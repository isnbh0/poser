"""Tests for the song formats: YAML song files and Python song scripts."""

import os
import stat
from pathlib import Path
from typing import Self

import numpy as np
import pytest
import yaml
from contracts import check_record_writer_contract, check_song_format_contract
from helpers import SCAFFOLD_DIR
from test_song import cc_track_song, gain_song, helper_song, valid_song

from poser.adapters import song_formats
from poser.adapters.song_formats import (
    SONG_FORMATS,
    ScriptSongFormat,
    YamlSongFormat,
    write_yaml_song,
)
from poser.core.loading import load_song
from poser.core.song import Note, Song, SongError, Track


def test_valid_song_loads_with_expected_values(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text(yaml.safe_dump(valid_song()))

    song = load_song(path, SONG_FORMATS)

    assert song.bpm == 120
    assert song.tracks == (
        Track("lead", "serum", (Note(beat=0, pitch=60, length=1, velocity=100),)),
    )


def test_demo_song_loads_with_two_bare_instrument_tracks_and_eight_notes() -> None:
    song = load_song(SCAFFOLD_DIR / "demo.yaml", SONG_FORMATS)

    assert len(song.tracks) == 2
    assert all(track.sound == "surge" for track in song.tracks)
    assert sum(len(track.notes) for track in song.tracks) >= 8


def test_missing_file_is_a_song_error(tmp_path: Path) -> None:
    with pytest.raises(SongError, match="cannot read"):
        load_song(tmp_path / "missing.yaml", SONG_FORMATS)


def test_malformed_yaml_is_a_song_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("bpm: [unclosed")
    with pytest.raises(SongError, match="cannot read"):
        load_song(path, SONG_FORMATS)


def test_written_song_loads_back_equal(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    song = helper_song()

    write_yaml_song(song, path)

    assert load_song(path, SONG_FORMATS) == song
    assert any(note.beat == 2 / 3 for note in load_song(path, SONG_FORMATS).tracks[0].notes)
    assert [p.name for p in tmp_path.iterdir()] == ["song.yaml"]


def test_written_song_keeps_the_format_key_order(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_yaml_song(helper_song(), path)

    data = yaml.safe_load(path.read_text())

    assert list(data) == ["bpm", "tracks"]
    assert all(list(track) == ["name", "sound", "notes"] for track in data["tracks"])
    assert list(data["tracks"][0]["notes"][0]) == ["beat", "pitch", "length", "velocity"]


def test_failed_replace_keeps_the_old_file_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.yaml"
    path.write_text("old contents\n")

    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(SongError, match="disk full"):
        write_yaml_song(helper_song(), path)

    assert path.read_text() == "old contents\n"
    assert [p.name for p in tmp_path.iterdir()] == ["song.yaml"]


def test_failed_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "song.yaml"
    real_fdopen = song_formats.os.fdopen

    class FailingFile:
        def __init__(self, fd: int, mode: str) -> None:
            self.handle = real_fdopen(fd, mode)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self.handle.close()
            raise OSError("flush failed")

        def write(self, text: str) -> int:
            return self.handle.write(text)

    monkeypatch.setattr(song_formats.os, "fdopen", FailingFile)
    with pytest.raises(SongError, match="flush failed"):
        write_yaml_song(helper_song(), path)

    assert list(tmp_path.iterdir()) == []


def test_written_song_keeps_the_existing_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text("old\n")
    path.chmod(0o640)

    write_yaml_song(helper_song(), path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_new_song_file_gets_umask_permissions(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        write_yaml_song(helper_song(), tmp_path / "song.yaml")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "song.yaml").stat().st_mode) == 0o644


def test_writing_through_a_symlink_keeps_the_link(tmp_path: Path) -> None:
    real = tmp_path / "real.yaml"
    real.write_text("old\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)

    write_yaml_song(helper_song(), link)

    assert link.is_symlink()
    assert load_song(real, SONG_FORMATS) == helper_song()


def test_writing_into_a_missing_directory_is_a_song_error(tmp_path: Path) -> None:
    with pytest.raises(SongError, match="cannot write"):
        write_yaml_song(helper_song(), tmp_path / "missing" / "song.yaml")


def test_numpy_gain_writes_as_a_plain_float(tmp_path: Path) -> None:
    note = (Note(beat=0, pitch=60, length=1, velocity=100),)
    song = Song(bpm=120, tracks=(Track("lead", "serum", note, gain=np.float64(-6)),))
    path = tmp_path / "song.yaml"

    write_yaml_song(song, path)

    assert load_song(path, SONG_FORMATS).tracks[0].gain == -6.0


def test_numbers_write_in_one_spelling(tmp_path: Path) -> None:
    def write(beat: float, length: float, bpm: float) -> str:
        note = (Note(beat=beat, pitch=60, length=length, velocity=100),)
        path = tmp_path / "song.yaml"
        write_yaml_song(Song(bpm=bpm, tracks=(Track("lead", "serum", note),)), path)
        return path.read_text()

    assert write(0, 1, 120) == write(0.0, np.float64(1.0), 120.0)
    assert write(np.float64(0.5), 1, 120) == write(0.5, 1, 120)
    assert "beat: 0.5" in write(np.float64(0.5), 1, 120)


class Pitch(int):
    pass


def test_unrepresentable_value_is_a_song_error(tmp_path: Path) -> None:
    note = (Note(beat=0.5, pitch=Pitch(60), length=1, velocity=100),)
    song = Song(bpm=120, tracks=(Track("lead", "serum", note),))
    with pytest.raises(SongError, match="cannot write"):
        write_yaml_song(song, tmp_path / "song.yaml")
    assert list(tmp_path.iterdir()) == []


def test_gain_from_yaml_nan_and_inf_is_rejected(tmp_path: Path) -> None:
    for text in (".nan", ".inf"):
        path = tmp_path / "song.yaml"
        path.write_text(
            yaml.safe_dump(valid_song()).replace(
                "  sound: serum", f"  sound: serum\n  gain: {text}"
            )
        )
        with pytest.raises(SongError, match="gain"):
            load_song(path, SONG_FORMATS)


def test_song_with_gains_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_yaml_song(gain_song(), path)

    assert load_song(path, SONG_FORMATS) == gain_song()
    assert "gain" not in yaml.safe_load(path.read_text())["tracks"][1]


def test_song_with_controller_changes_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_yaml_song(cc_track_song(), path)

    assert load_song(path, SONG_FORMATS) == cc_track_song()


SCRIPT_HEADER = """from poser import ControlChange, Note, Song, Track
"""


def write_script(tmp_path: Path, body: str, name: str = "tune.py") -> Path:
    path = tmp_path / name
    path.write_text(SCRIPT_HEADER + body)
    return path


def test_script_song_loads_equal_to_the_song_it_builds(tmp_path: Path) -> None:
    path = write_script(
        tmp_path,
        """
def build():
    note = Note(0, 60, 1, 100)
    return Song(120, (Track("lead", "serum", (note,), gain=-3),))
""",
    )

    song = load_song(path, SONG_FORMATS)

    assert song == Song(120, (Track("lead", "serum", (Note(0, 60, 1, 100),), gain=-3.0),))


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("x = 1\n", "must define a function build() that returns a Song"),
        ("build = 3\n", "must define a function build() that returns a Song"),
        ("raise ImportError('no module')\n", "cannot run"),
        ("def build():\n    raise ValueError('boom')\n", "build() raised ValueError: boom"),
        ("def build():\n    return {'bpm': 120, 'tracks': []}\n", "must return a Song, got dict"),
    ],
)
def test_broken_script_song_is_one_song_error_naming_the_file(
    tmp_path: Path, body: str, message: str
) -> None:
    path = write_script(tmp_path, body)

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert len(error.value.problems) == 1
    assert str(path) in error.value.problems[0]
    assert message in error.value.problems[0]


def test_unknown_song_suffix_is_a_song_error(tmp_path: Path) -> None:
    path = tmp_path / "tune.json"
    path.write_text("{}")

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert len(error.value.problems) == 1
    assert str(path) in error.value.problems[0]
    assert ".yaml, .yml, or .py" in error.value.problems[0]


def test_script_song_is_validated_like_yaml(tmp_path: Path) -> None:
    pitch_path = write_script(
        tmp_path,
        """
def build():
    return Song(120, (Track("lead", "serum", (Note(0, 200, 1, 100),)),))
""",
        "pitch.py",
    )
    ramp_path = write_script(
        tmp_path,
        """
def build():
    notes = (Note(0, 60, 1, 100),)
    ramp = (ControlChange(0, 1, 0, "linear"),)
    return Song(120, (Track("lead", "serum", notes, cc=ramp),))
""",
        "ramp.py",
    )

    with pytest.raises(SongError) as pitch_error:
        load_song(pitch_path, SONG_FORMATS)
    with pytest.raises(SongError) as ramp_error:
        load_song(ramp_path, SONG_FORMATS)

    assert pitch_error.value.problems == [
        f"{pitch_path}: Note.pitch: must be an integer from 0 to 127, got 200"
    ]
    assert ramp_error.value.problems == [
        f"{ramp_path}: Track.cc[0].shape: linear needs a later change to control 1"
    ]


def test_script_and_yaml_with_one_stem_both_load(tmp_path: Path) -> None:
    write_script(
        tmp_path,
        "def build():\n    return Song(90, (Track('lead', 'serum', (Note(0, 60, 1, 100),)),))\n",
        "x.py",
    )
    (tmp_path / "x.yaml").write_text(yaml.safe_dump(valid_song()))

    assert load_song(tmp_path / "x.py", SONG_FORMATS).bpm == 90
    assert load_song(tmp_path / "x.yaml", SONG_FORMATS).bpm == 120


@pytest.mark.parametrize("where", ["import", "build"])
def test_script_song_that_exits_is_a_song_error(tmp_path: Path, where: str) -> None:
    exits = "import sys\nsys.exit(2)\n"
    body = exits if where == "import" else "def build():\n    import sys\n    sys.exit(2)\n"
    path = write_script(tmp_path, body)

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert "SystemExit" in error.value.problems[0]


@pytest.mark.parametrize(
    ("build", "problem"),
    [
        ("Song(120, None)", "Song.tracks:"),
        ('Song(120, (Track("lead", "serum", None),))', "Track.notes:"),
        (
            'Song(120, (Track("lead", "serum", ((0, 60, 1, 100),)),))',
            "Track.notes[0]:",
        ),
    ],
)
def test_misshapen_script_song_reports_the_path(tmp_path: Path, build: str, problem: str) -> None:
    path = write_script(tmp_path, f"def build():\n    return {build}\n")

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert any(p.startswith(f"{path}: {problem}") for p in error.value.problems), (
        error.value.problems
    )


def test_both_formats_meet_the_song_format_contract(tmp_path: Path) -> None:
    for fmt in SONG_FORMATS:
        directory = tmp_path / type(fmt).__name__
        directory.mkdir()
        check_song_format_contract(fmt, helper_song(), directory)


def test_yaml_format_meets_the_record_writer_contract() -> None:
    for song in (helper_song(), gain_song(), cc_track_song()):
        check_record_writer_contract(YamlSongFormat(), song)


def test_format_suffixes() -> None:
    assert YamlSongFormat().suffixes == (".yaml", ".yml")
    assert YamlSongFormat().suffix == ".yaml"
    assert ScriptSongFormat().suffixes == (".py",)


def test_yml_suffix_loads_as_yaml(tmp_path: Path) -> None:
    path = tmp_path / "song.yml"
    path.write_text(yaml.safe_dump(valid_song()))
    assert load_song(path, SONG_FORMATS).bpm == 120


def test_text_file_is_not_a_song(tmp_path: Path) -> None:
    path = tmp_path / "x.txt"
    path.write_text("bpm: 120\n")

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert error.value.problems == [f"cannot read {path}: a song is a .yaml, .yml, or .py file"]


def test_two_formats_claiming_one_suffix_raise(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text(yaml.safe_dump(valid_song()))

    with pytest.raises(ValueError, match=r"more than one song format claims the suffix \.yaml"):
        load_song(path, (YamlSongFormat(), YamlSongFormat()))


def test_yaml_format_loads_its_own_dump(tmp_path: Path) -> None:
    fmt = YamlSongFormat()
    for song in (helper_song(), gain_song(), cc_track_song()):
        path = tmp_path / f"song{fmt.suffix}"
        path.write_text(fmt.dump(song))
        assert fmt.load(path) == song


def test_yaml_data_with_engine_is_refused(tmp_path: Path) -> None:
    data = valid_song()
    tracks = data["tracks"]
    assert isinstance(tracks, list)
    tracks[0] = {**tracks[0], "engine": "pedalboard"}
    path = tmp_path / "song.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert error.value.problems == ["tracks[0]: unknown key 'engine'"]
