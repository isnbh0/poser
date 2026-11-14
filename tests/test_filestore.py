"""Tests for the render store on the filesystem."""

import subprocess
import sys
from pathlib import Path

import pytest
from contracts import check_render_store_contract

from poser.adapters.filestore import FileRenderStore, FileSongStore
from poser.core.names import Identifier, SongId, Suffix
from poser.core.ports import SongBusyError, StemMeta
from poser.core.statuses import StemStatus

WAV, PNG, YAML = Suffix(".wav"), Suffix(".png"), Suffix(".yaml")
KEY_A, KEY_B, KEY_C = "a" * 64, "b" * 64, "c" * 64


class WriteInterruptedError(Exception):
    """Stands in for a failure while a unit or record is written."""


def publish(song: FileSongStore, key: str, *stems: bytes, replace: bool = False) -> None:
    """Publish a unit holding `stems`, each rendered at -6 dBFS."""
    with song.write_unit(key, replace=replace) as draft:
        for index, data in enumerate(stems):
            draft.stem_path(index).write_bytes(data)
            draft.describe(index, StemStatus.RENDERED, -6.0)


def interrupt_unit(song: FileSongStore, key: str) -> None:
    with song.write_unit(key) as draft:
        draft.stem_path(0).write_bytes(b"partial")
        draft.describe(0, StemStatus.RENDERED, -6.0)
        raise WriteInterruptedError


def interrupt_record(song: FileSongStore) -> None:
    with song.write_record("song", YAML, latest=True) as record:
        record.path("report.json").write_text("partial")
        raise WriteInterruptedError


def describe_second_stem_only(song: FileSongStore) -> None:
    with song.write_unit(KEY_A) as draft:
        draft.stem_path(1).write_bytes(b"stem")
        draft.describe(1, StemStatus.RENDERED, -6.0)


def drafts(directory: Path) -> list[Path]:
    return [path for path in directory.rglob("*") if path.name.startswith((".tmp-", "latest.tmp-"))]


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
    check_render_store_contract(lambda root: FileRenderStore(root, WAV), tmp_path)


def test_file_render_store_keeps_the_documented_layout(tmp_path: Path) -> None:
    store = renders(tmp_path)
    song = store.song(SongId("songs/demo.yaml"))
    with song.lock():
        publish(song, KEY_A, b"lead", b"bass")
        with song.write_record("song text", YAML, latest=True) as record:
            for relative in ("images/node-0.png", "images/mix.png", "audio/mix.wav"):
                record.path(relative).write_bytes(b"file")
            record.path("report.json").write_text("report")
    preview = store.preview_path(Identifier("serum"), Identifier("pad"))
    preview.write_bytes(b"preview")

    song_dir = tmp_path / "renders" / "songs" / "demo.yaml"
    assert record.run == 1
    assert song.record_path(1) == song_dir / "history" / "1"
    assert files_under(tmp_path / "renders") == {
        ".songs.lock",
        "songs/demo.yaml/.lock",
        f"songs/demo.yaml/objects/{KEY_A}/meta.json",
        f"songs/demo.yaml/objects/{KEY_A}/0.wav",
        f"songs/demo.yaml/objects/{KEY_A}/1.wav",
        "songs/demo.yaml/history/1/song.yaml",
        "songs/demo.yaml/history/1/report.json",
        "songs/demo.yaml/history/1/audio/mix.wav",
        "songs/demo.yaml/history/1/images/node-0.png",
        "songs/demo.yaml/history/1/images/mix.png",
        ".sound-previews/serum/pad.wav",
    }
    assert (song_dir / "latest").is_symlink()
    assert (song_dir / "latest").readlink() == Path("history/1")
    assert (song_dir / "latest" / "report.json").read_text() == "report"
    assert (song_dir / "objects" / KEY_A / "meta.json").read_text() == (
        '{"stems": [{"status": "rendered", "peak_dbfs": -6.0}, '
        '{"status": "rendered", "peak_dbfs": -6.0}]}\n'
    )
    assert store.protected_roots() == ((tmp_path / "renders").resolve(),)


def test_a_unit_is_found_only_once_published(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    assert song.find(KEY_A) is None
    with song.write_unit(KEY_A) as draft:
        draft.stem_path(0).write_bytes(b"stem")
        draft.describe(0, StemStatus.SILENT, -90.0)
        assert song.find(KEY_A) is None
    unit = song.find(KEY_A)
    assert unit is not None
    assert unit.stems() == (StemMeta(StemStatus.SILENT, -90.0),)
    assert unit.stem_path(0).read_bytes() == b"stem"


def test_an_exception_leaves_no_unit_and_no_draft(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    with pytest.raises(WriteInterruptedError):
        interrupt_unit(song, KEY_A)
    assert song.find(KEY_A) is None
    assert not drafts(tmp_path)
    assert not (tmp_path / "renders" / "songs" / "a.yaml" / "objects" / KEY_A).exists()


def test_a_unit_that_does_not_describe_every_stem_is_refused(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    with pytest.raises(ValueError, match="from 0 up"):
        describe_second_stem_only(song)
    with pytest.raises(ValueError, match="no stem"), song.write_unit(KEY_A) as draft:
        draft.describe(0, StemStatus.RENDERED, -6.0)
    assert song.find(KEY_A) is None
    assert not drafts(tmp_path)


@pytest.mark.parametrize("damage", ["no meta", "bad meta", "extra stem", "missing stem"], ids=str)
def test_a_damaged_unit_is_not_found(tmp_path: Path, damage: str) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    publish(song, KEY_A, b"one", b"two")
    unit = tmp_path / "renders" / "songs" / "a.yaml" / "objects" / KEY_A
    match damage:
        case "no meta":
            (unit / "meta.json").unlink()
        case "bad meta":
            (unit / "meta.json").write_text('{"stems": [{"status": "lost", "peak_dbfs": 0}]}')
        case "extra stem":
            (unit / "2.wav").write_bytes(b"three")
        case _:
            (unit / "1.wav").unlink()
    assert song.find(KEY_A) is None
    # A render publishes over the damaged unit.
    publish(song, KEY_A, b"fresh")
    found = song.find(KEY_A)
    assert found is not None
    assert found.stem_path(0).read_bytes() == b"fresh"


def test_find_refuses_a_key_that_is_not_a_digest(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    with pytest.raises(ValueError, match="64 lowercase hex"):
        song.find("../../escape")


def test_drafts_are_never_found_and_sweep_deletes_them(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    song_dir = tmp_path / "renders" / "songs" / "a.yaml"
    unit_draft = song_dir / "objects" / ".tmp-left"
    unit_draft.mkdir(parents=True)
    (unit_draft / "meta.json").write_text('{"stems": [{"status": "rendered", "peak_dbfs": 0}]}')
    (unit_draft / "0.wav").write_bytes(b"stem")
    record_draft = song_dir / "history" / ".tmp-left"
    record_draft.mkdir(parents=True)
    (record_draft / "song.yaml").write_text("song")
    (song_dir / "latest.tmp-left").symlink_to("history/.tmp-left")
    with song.lock():
        with song.write_record("song", YAML, latest=False) as record:
            assert record.run == 1
        song.sweep()
    assert not drafts(tmp_path)
    assert song.record_path(1).is_dir()


def test_replace_swaps_an_existing_unit(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    publish(song, KEY_A, b"old", b"old")
    publish(song, KEY_A, b"ignored")
    kept = song.find(KEY_A)
    assert kept is not None
    assert len(kept.stems()) == 2
    publish(song, KEY_A, b"new", replace=True)
    replaced = song.find(KEY_A)
    assert replaced is not None
    assert len(replaced.stems()) == 1
    assert replaced.stem_path(0).read_bytes() == b"new"
    assert not drafts(tmp_path)


def test_keep_units_deletes_exactly_the_units_not_given(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    for key in (KEY_A, KEY_B, KEY_C):
        publish(song, key, key.encode())
    song.keep_units([KEY_B])
    assert [song.find(key) is not None for key in (KEY_A, KEY_B, KEY_C)] == [False, True, False]
    assert sorted(p.name for p in (tmp_path / "renders/songs/a.yaml/objects").iterdir()) == [KEY_B]


def test_latest_follows_the_newest_complete_record(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    latest = tmp_path / "renders" / "songs" / "a.yaml" / "latest"
    with song.write_record("first", YAML, latest=True) as record:
        record.path("report.json").write_text("1")
    assert latest.readlink() == Path("history/1")
    with song.write_record("second", YAML, latest=True) as record:
        record.path("report.json").write_text("2")
    assert latest.readlink() == Path("history/2")
    assert (latest / "report.json").read_text() == "2"
    with song.write_record("third", YAML, latest=False):
        pass
    assert not latest.exists()
    assert not latest.is_symlink()
    assert (song.record_path(2) / "report.json").read_text() == "2"
    assert not drafts(tmp_path)


@pytest.mark.parametrize("complete", [False, True])
def test_latest_is_cleared_before_a_record_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    """A crash between publishing a record and pointing `latest` must not leave `latest`
    naming an older record."""
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    latest = tmp_path / "renders" / "songs" / "a.yaml" / "latest"
    with song.write_record("first", YAML, latest=True):
        pass
    rename = Path.rename

    def crash_on_publish(self: Path, target: Path) -> Path:
        if Path(target) == song.record_path(2):
            assert not latest.is_symlink()
            raise WriteInterruptedError
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", crash_on_publish)
    with pytest.raises(WriteInterruptedError), song.write_record("second", YAML, latest=complete):
        pass
    assert not latest.is_symlink()
    assert song.record_path(1).is_dir()


def test_an_exception_leaves_no_record_and_latest_as_it_was(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    latest = tmp_path / "renders" / "songs" / "a.yaml" / "latest"
    with song.write_record("first", YAML, latest=True):
        pass
    with pytest.raises(WriteInterruptedError):
        interrupt_record(song)
    assert not song.record_path(2).exists()
    assert latest.readlink() == Path("history/1")
    assert not drafts(tmp_path)
    with song.write_record("second", YAML, latest=True) as record:
        assert record.run == 2


def test_clear_latest_removes_only_the_link(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    song_dir = tmp_path / "renders" / "songs" / "a.yaml"
    with song.write_record("first", YAML, latest=True):
        pass
    song.clear_latest()
    assert not (song_dir / "latest").is_symlink()
    assert (song.record_path(1) / "song.yaml").read_text() == "first"
    song.clear_latest()


def test_a_latest_directory_from_an_older_layout_is_replaced(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    old = tmp_path / "renders" / "songs" / "a.yaml" / "latest"
    old.mkdir(parents=True)
    (old / "mix.wav").write_bytes(b"old mix")
    with song.write_record("first", YAML, latest=True):
        pass
    assert old.readlink() == Path("history/1")


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
    publish(song, KEY_A, b"stem")
    run(song)
    run(song)


def test_song_accepts_an_empty_directory_or_its_own(tmp_path: Path) -> None:
    store = renders(tmp_path)
    (tmp_path / "renders" / "songs" / "empty.yaml").mkdir(parents=True)
    run(store.song(SongId("songs/empty.yaml")))
    song = store.song(SongId("songs/a.yaml"))
    run(song)
    publish(song, KEY_A, b"stem")
    run(store.song(SongId("songs/a.yaml")))


def test_sibling_songs_keep_their_files(tmp_path: Path) -> None:
    store = renders(tmp_path)
    a, b = store.song(SongId("songs/a.yaml")), store.song(SongId("songs/b.yaml"))
    for song in (a, b):
        with song.lock():
            publish(song, KEY_A, b"stem")
            with song.write_record("song", YAML, latest=True):
                pass
    before = files_under(tmp_path / "renders" / "songs" / "b.yaml")
    a.clear_latest()
    a.keep_units([])
    assert files_under(tmp_path / "renders" / "songs" / "b.yaml") == before
    assert b.find(KEY_A) is not None
    assert files_under(tmp_path / "renders" / "songs" / "a.yaml") == {
        ".lock",
        "history/1/song.yaml",
    }


def test_finding_a_unit_creates_nothing(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    assert song.find(KEY_A) is None
    song.sweep()
    song.keep_units([])
    song.clear_latest()
    assert not (tmp_path / "renders").exists()


def test_records_number_after_the_highest_record(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    history = tmp_path / "renders" / "songs" / "a.yaml" / "history"
    (history / "7").mkdir(parents=True)
    (history / "notes").mkdir()
    with song.write_record("song", YAML, latest=False) as record:
        assert record.run == 8


def test_a_record_path_cannot_leave_the_record(tmp_path: Path) -> None:
    song = renders(tmp_path).song(SongId("songs/a.yaml"))
    with song.write_record("song", YAML, latest=False) as record:
        for relative in ("../escape", "/abs", ""):
            with pytest.raises(ValueError, match="inside the record"):
                record.path(relative)


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
    for directory in ("objects/", "`latest`", "history/"):
        assert directory in guide
    assert "latest/audio/mix.wav" in guide
