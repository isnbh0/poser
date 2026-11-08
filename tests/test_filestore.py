"""Tests for the render store on the filesystem."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from contracts import check_render_store_contract

from poser.adapters.filestore import FileRenderStore, FileSongStore
from poser.core.cache import Stamp, StampProblem
from poser.core.names import Identifier, SongId, Suffix, TrackName
from poser.core.ports import SongBusyError, SongStore
from poser.core.statuses import StemStatus

WAV, PNG, YAML = Suffix(".wav"), Suffix(".png"), Suffix(".yaml")
LEAD, BASS = TrackName("lead"), TrackName("bass")
STAMP = Stamp("a" * 64, "b" * 64, StemStatus.RENDERED, -6.0)


def spoil_stamp(store: SongStore, track: TrackName) -> None:
    assert isinstance(store, FileSongStore)
    path = store.stem_path(track).parent.parent / "stamps" / f"{track}.json"
    assert path.is_file()
    path.write_text("{garbage")


def renders(tmp_path: Path) -> FileRenderStore:
    return FileRenderStore(tmp_path / "renders", WAV)


def files_under(directory: Path) -> set[str]:
    return {
        path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
    }


def run(store: FileSongStore) -> None:
    """Take and release the song's lock, as every run does."""
    with store.lock():
        pass


def test_file_render_store_meets_the_render_store_contract(tmp_path: Path) -> None:
    check_render_store_contract(lambda root: FileRenderStore(root, WAV), tmp_path, spoil_stamp)


def test_file_render_store_keeps_the_documented_layout(tmp_path: Path) -> None:
    store = renders(tmp_path)
    song = store.song(SongId("songs/demo.yaml"))
    with song.lock():
        for track in (LEAD, BASS):
            song.stem_path(track).write_bytes(b"stem")
            song.write_stamp(track, STAMP)
            song.image_path(track, PNG).write_bytes(b"image")
        song.image_path(None, PNG).write_bytes(b"mix image")
        song.mix_path().write_bytes(b"mix")
        song.report_path().write_text("{}")
        record = song.start_record("song text", YAML)
        for track in (LEAD, BASS, None):
            song.keep_image(record, track, PNG)
        song.finish_record(record, "report")
    preview = store.preview_path(Identifier("serum"), Identifier("pad"))
    preview.write_bytes(b"preview")

    assert record.run == 1
    assert record.path == tmp_path / "renders" / "songs" / "demo.yaml" / "history" / "1"
    assert files_under(tmp_path / "renders") == {
        ".songs.lock",
        "songs/demo.yaml/.lock",
        "songs/demo.yaml/stems/lead.wav",
        "songs/demo.yaml/stems/bass.wav",
        "songs/demo.yaml/stamps/lead.json",
        "songs/demo.yaml/stamps/bass.json",
        "songs/demo.yaml/latest/report.json",
        "songs/demo.yaml/latest/mix.wav",
        "songs/demo.yaml/latest/images/lead.png",
        "songs/demo.yaml/latest/images/bass.png",
        "songs/demo.yaml/latest/images/[mix].png",
        "songs/demo.yaml/history/1/song.yaml",
        "songs/demo.yaml/history/1/report.json",
        "songs/demo.yaml/history/1/images/lead.png",
        "songs/demo.yaml/history/1/images/bass.png",
        "songs/demo.yaml/history/1/images/[mix].png",
        ".sound-previews/serum/pad.wav",
    }
    history = tmp_path / "renders" / "songs" / "demo.yaml" / "history" / "1"
    assert (history / "song.yaml").read_text() == "song text"
    assert (history / "report.json").read_text() == "report"
    assert (history / "images" / "[mix].png").read_bytes() == b"mix image"


def test_song_refuses_the_store_s_own_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"starting with '\.'"):
        renders(tmp_path).song(SongId(".x/y.yaml"))


def test_song_refuses_a_directory_inside_another_song_s(tmp_path: Path) -> None:
    store = renders(tmp_path)
    run(store.song(SongId("songs/a.yaml")))
    with pytest.raises(ValueError, match=r"inside the directory of the song 'songs/a\.yaml'"):
        store.song(SongId("songs/a.yaml/latest/b.py"))


def test_song_refuses_a_directory_holding_another_song_s(tmp_path: Path) -> None:
    store = renders(tmp_path)
    run(store.song(SongId("songs/a.yaml/latest/b.py")))
    with pytest.raises(ValueError, match="would hold other songs' directories"):
        store.song(SongId("songs/a.yaml"))


def test_lock_refuses_a_song_nested_after_its_handle_was_taken(tmp_path: Path) -> None:
    store = renders(tmp_path)
    outer, inner = store.song(SongId("songs/a.yaml")), store.song(SongId("songs/a.yaml/x/b.py"))
    run(outer)
    with pytest.raises(ValueError, match=r"inside the directory of the song 'songs/a\.yaml'"):
        run(inner)
    assert not (tmp_path / "renders" / "songs" / "a.yaml" / "x").exists()

    store = renders(tmp_path / "reversed")
    outer, inner = store.song(SongId("songs/a.yaml")), store.song(SongId("songs/a.yaml/x/b.py"))
    run(inner)
    with pytest.raises(ValueError, match="would hold other songs' directories"):
        run(outer)
    assert not (tmp_path / "reversed" / "renders" / "songs" / "a.yaml" / ".lock").exists()


def test_lock_accepts_a_song_holding_its_own_files(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    song.stem_path(LEAD).write_bytes(b"stem")
    song.write_stamp(LEAD, STAMP)
    run(song)
    run(song)


def test_song_accepts_an_empty_directory_or_its_own(tmp_path: Path) -> None:
    store = renders(tmp_path)
    (tmp_path / "renders" / "songs" / "empty.yaml").mkdir(parents=True)
    run(store.song(SongId("songs/empty.yaml")))
    song = store.song(SongId("songs/a.yaml"))
    run(song)
    song.stem_path(LEAD).write_bytes(b"stem")
    run(store.song(SongId("songs/a.yaml")))


def test_sibling_songs_keep_their_files(tmp_path: Path) -> None:
    store = renders(tmp_path)
    a, b = store.song(SongId("songs/a.yaml")), store.song(SongId("songs/b.yaml"))
    for song in (a, b):
        with song.lock():
            song.stem_path(LEAD).write_bytes(b"stem")
            song.write_stamp(LEAD, STAMP)
            song.report_path().write_text("{}")
    before = files_under(tmp_path / "renders" / "songs" / "b.yaml")
    a.clear_latest()
    a.discard(LEAD)
    assert files_under(tmp_path / "renders" / "songs" / "b.yaml") == before
    assert b.read_stamp(LEAD) == STAMP
    assert files_under(tmp_path / "renders" / "songs" / "a.yaml") == {".lock"}


def test_stamps_round_trip_and_notice_a_changed_stem(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    stem = song.stem_path(LEAD)
    stem.write_bytes(b"stem")
    song.write_stamp(LEAD, STAMP)
    assert song.read_stamp(LEAD) == STAMP

    info = stem.stat()
    os.utime(stem, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))
    assert song.read_stamp(LEAD) is StampProblem.CHANGED

    song.write_stamp(LEAD, STAMP)
    stem.write_bytes(b"a longer stem")
    os.utime(stem, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))
    assert song.read_stamp(LEAD) is StampProblem.CHANGED

    song.write_stamp(LEAD, STAMP)
    stem.unlink()
    assert song.read_stamp(LEAD) is StampProblem.CHANGED


@pytest.mark.parametrize(
    "text",
    [
        "",
        "[]",
        '{"track_key": "a"}',
        (
            '{"track_key": 1, "sound_key": "b", "status": "rendered", "peak_dbfs": 0, '
            '"stem_size": 4, "stem_mtime_ns": 1}'
        ),
        (
            '{"track_key": "a", "sound_key": "b", "status": "lost", "peak_dbfs": 0, '
            '"stem_size": 4, "stem_mtime_ns": 1}'
        ),
        (
            '{"track_key": "a", "sound_key": "b", "status": "rendered", "peak_dbfs": 0, '
            '"stem_size": true, "stem_mtime_ns": 1}'
        ),
    ],
    ids=["empty", "list", "keys", "track key", "status", "size"],
)
def test_an_unusable_stamp_reads_as_missing(tmp_path: Path, text: str) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    song.stem_path(LEAD).write_bytes(b"stem")
    song.write_stamp(LEAD, STAMP)
    (tmp_path / "renders" / "songs" / "a.yaml" / "stamps" / "lead.json").write_text(text)
    assert song.read_stamp(LEAD) is StampProblem.MISSING


def test_reading_a_stamp_creates_nothing(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    assert song.read_stamp(LEAD) is StampProblem.MISSING
    assert not (tmp_path / "renders").exists()

    song.stem_path(LEAD).write_bytes(b"stem")
    song.write_stamp(LEAD, STAMP)
    stems = tmp_path / "renders" / "songs" / "a.yaml" / "stems"
    (stems / "lead.wav").unlink()
    stems.rmdir()
    assert song.read_stamp(LEAD) is StampProblem.CHANGED
    assert not stems.exists()


def test_records_number_after_the_highest_record(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    history = tmp_path / "renders" / "songs" / "a.yaml" / "history"
    (history / "7").mkdir(parents=True)
    (history / "notes").mkdir()
    assert song.start_record("song", YAML).run == 8


LOCK_HOLDER = """
import sys, time
from pathlib import Path
from poser.adapters.filestore import FileRenderStore
from poser.core.names import SongId, Suffix

song = FileRenderStore(Path(sys.argv[1]), Suffix(".wav")).song(SongId(sys.argv[2]))
with song.lock():
    print("locked", flush=True)
    time.sleep(60)
"""


def test_a_second_process_finds_the_song_busy(tmp_path: Path) -> None:
    store = renders(tmp_path)
    with subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(tmp_path / "renders"), "songs/a.yaml"],
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline() == "locked\n"
            message = "another render-song or analyze-song is running on a.yaml"
            with pytest.raises(SongBusyError, match=message):
                run(store.song(SongId("songs/a.yaml")))
            run(store.song(SongId("songs/b.yaml")))
        finally:
            holder.kill()
            holder.wait()
    run(store.song(SongId("songs/a.yaml")))


def test_guide_names_each_kept_directory(tmp_path: Path) -> None:
    guide = renders(tmp_path).guide
    assert guide.startswith("## ")
    for directory in ("stems/", "latest/", "history/"):
        assert directory in guide
    assert "<track>.wav" in guide
