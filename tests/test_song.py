"""Tests for the song format. No plugins or rendering."""

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Self, cast

import numpy as np
import pytest
import yaml
from helpers import SCAFFOLD_DIR

from poser import song as song_module
from poser.music import chord, notes, pitch, scale, swing
from poser.song import (
    ControlChange,
    Note,
    NoteData,
    Song,
    SongData,
    SongError,
    Track,
    load_song,
    parse_song,
    song_to_data,
    write_song,
)


def valid_note() -> dict[str, object]:
    return {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}


def valid_track(name: str = "lead") -> dict[str, object]:
    return {"name": name, "engine": "pedalboard", "sound": "Serum", "notes": [valid_note()]}


def valid_song() -> dict[str, object]:
    return {"bpm": 120, "tracks": [valid_track()]}


def test_valid_song_loads_with_expected_values(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text(yaml.safe_dump(valid_song()))

    song = load_song(path)

    assert song.bpm == 120
    assert song.tracks == (
        Track("lead", "pedalboard", "Serum", (Note(beat=0, pitch=60, length=1, velocity=100),)),
    )


def test_demo_song_loads_with_two_serum_preset_tracks_and_eight_notes() -> None:
    song = load_song(SCAFFOLD_DIR / "demo.yaml")

    assert len(song.tracks) == 2
    assert all(track.sound.startswith("Serum:") for track in song.tracks)
    assert sum(len(track.notes) for track in song.tracks) >= 8


def test_seconds_uses_bpm() -> None:
    assert parse_song({**valid_song(), "bpm": 90}).seconds(3) == pytest.approx(2.0)


def first_item(container: dict[str, object], key: str) -> dict[str, object]:
    """Return the first mapping in a list held by song data under construction."""
    items = container[key]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    return first


def first_track(song: dict[str, object]) -> dict[str, object]:
    return first_item(song, "tracks")


def first_note(song: dict[str, object]) -> dict[str, object]:
    return first_item(first_track(song), "notes")


def set_bpm(song: dict[str, object], value: object) -> None:
    song["bpm"] = value


def set_track(song: dict[str, object], key: str, value: object) -> None:
    first_track(song)[key] = value


def set_note(song: dict[str, object], key: str, value: object) -> None:
    first_note(song)[key] = value


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda s: set_bpm(s, 0), "bpm"),
        (lambda s: set_bpm(s, -120), "bpm"),
        (lambda s: set_bpm(s, "fast"), "bpm"),
        (lambda s: set_bpm(s, True), "bpm"),
        (lambda s: set_bpm(s, float("inf")), "bpm"),
        (lambda s: s.update(tracks=[]), "at least one track"),
        (lambda s: s.update(tracks="lead"), "at least one track"),
        (lambda s: set_track(s, "notes", []), "at least one note"),
        (lambda s: s.update(tracks=[valid_track(), valid_track()]), "duplicate track name 'lead'"),
        (lambda s: s.update(tracks=[valid_track(), valid_track("LEAD")]), "duplicate track name"),
        (lambda s: set_track(s, "name", "has space"), "tracks[0].name"),
        (lambda s: set_track(s, "name", "../escape"), "tracks[0].name"),
        (lambda s: set_track(s, "name", "line\n"), "tracks[0].name"),
        (lambda s: set_track(s, "name", 7), "tracks[0].name"),
        (lambda s: set_track(s, "engine", "reaper"), "unknown engine 'reaper'"),
        (lambda s: set_track(s, "sound", ""), "tracks[0].sound"),
        (lambda s: set_note(s, "pitch", -1), "notes[0].pitch"),
        (lambda s: set_note(s, "pitch", 128), "notes[0].pitch"),
        (lambda s: set_note(s, "pitch", 60.5), "notes[0].pitch"),
        (lambda s: set_note(s, "velocity", 0), "notes[0].velocity"),
        (lambda s: set_note(s, "velocity", 128), "notes[0].velocity"),
        (lambda s: set_note(s, "beat", -1), "notes[0].beat"),
        (lambda s: set_note(s, "beat", float("nan")), "notes[0].beat"),
        (lambda s: set_note(s, "length", 0), "notes[0].length"),
        (lambda s: set_note(s, "length", -0.5), "notes[0].length"),
        (lambda s: s.update(tempo=120), "unknown key 'tempo'"),
        (lambda s: set_track(s, "pan", 1), "tracks[0]: unknown key 'pan'"),
        (lambda s: set_note(s, "vel", 1), "notes[0]: unknown key 'vel'"),
        (lambda s: s.pop("bpm"), "missing key 'bpm'"),
        (lambda s: first_track(s).pop("sound"), "tracks[0]: missing key 'sound'"),
        (lambda s: first_note(s).pop("pitch"), "notes[0]: missing key 'pitch'"),
    ],
)
def test_invalid_song_names_the_problem(
    change: Callable[[dict[str, object]], object], expected: str
) -> None:
    song = valid_song()
    change(song)

    with pytest.raises(SongError) as raised:
        parse_song(song)

    assert expected in str(raised.value)


@pytest.mark.parametrize("data", [None, [], "song", 3])
def test_song_that_is_not_a_mapping_is_rejected(data: object) -> None:
    with pytest.raises(SongError, match="must be a mapping"):
        parse_song(data)


def test_each_problem_gets_its_own_message() -> None:
    song = valid_song()
    set_bpm(song, 0)
    set_note(song, "pitch", 200)
    set_note(song, "velocity", 0)

    with pytest.raises(SongError) as raised:
        parse_song(song)

    assert len(raised.value.problems) == 3


def test_missing_file_is_a_song_error(tmp_path: Path) -> None:
    with pytest.raises(SongError, match="cannot read"):
        load_song(tmp_path / "missing.yaml")


def test_malformed_yaml_is_a_song_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("bpm: [unclosed")
    with pytest.raises(SongError, match="cannot read"):
        load_song(path)


def test_song_module_stays_engine_neutral() -> None:
    source = Path(song_module.__file__).read_text()
    assert "pedalboard import" not in source
    assert "import mido" not in source


def helper_song() -> Song:
    progression = chord(pitch("A3"), "min") + chord(pitch("F3"), "maj", 1)
    lead = swing(notes(scale(pitch("A4"), "minor"), 0, 0.5, 90, step=0.5), 2 / 3)
    return Song(
        bpm=96.5,
        tracks=(
            Track("lead", "pedalboard", "Serum:Leads/LD A Bit of Luck [SD]", lead),
            Track("pad", "pedalboard", "Serum", notes(progression, 0, 4, 70)),
        ),
    )


def test_written_song_loads_back_equal(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    song = helper_song()

    write_song(song, path)

    assert load_song(path) == song
    assert any(note.beat == 2 / 3 for note in load_song(path).tracks[0].notes)
    assert [p.name for p in tmp_path.iterdir()] == ["song.yaml"]


def test_written_song_keeps_the_format_key_order(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_song(helper_song(), path)

    data = yaml.safe_load(path.read_text())

    assert list(data) == ["bpm", "tracks"]
    assert all(list(track) == ["name", "engine", "sound", "notes"] for track in data["tracks"])
    assert list(data["tracks"][0]["notes"][0]) == ["beat", "pitch", "length", "velocity"]


def test_song_to_data_round_trips_through_parse_song() -> None:
    song = helper_song()
    assert parse_song(song_to_data(song)) == song


def test_failed_replace_keeps_the_old_file_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.yaml"
    path.write_text("old contents\n")

    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(song_module.os, "replace", fail)
    with pytest.raises(SongError, match="disk full"):
        write_song(helper_song(), path)

    assert path.read_text() == "old contents\n"
    assert [p.name for p in tmp_path.iterdir()] == ["song.yaml"]


def test_failed_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "song.yaml"
    real_fdopen = song_module.os.fdopen

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

    monkeypatch.setattr(song_module.os, "fdopen", FailingFile)
    with pytest.raises(SongError, match="flush failed"):
        write_song(helper_song(), path)

    assert list(tmp_path.iterdir()) == []


def test_written_song_keeps_the_existing_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text("old\n")
    path.chmod(0o640)

    write_song(helper_song(), path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_new_song_file_gets_umask_permissions(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        write_song(helper_song(), tmp_path / "song.yaml")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "song.yaml").stat().st_mode) == 0o644


def test_writing_through_a_symlink_keeps_the_link(tmp_path: Path) -> None:
    real = tmp_path / "real.yaml"
    real.write_text("old\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)

    write_song(helper_song(), link)

    assert link.is_symlink()
    assert load_song(real) == helper_song()


def test_writing_into_a_missing_directory_is_a_song_error(tmp_path: Path) -> None:
    with pytest.raises(SongError, match="cannot write"):
        write_song(helper_song(), tmp_path / "missing" / "song.yaml")


def test_track_without_gain_has_gain_zero() -> None:
    assert parse_song(valid_song()).tracks[0].gain == 0.0


@pytest.mark.parametrize("gain", [-60, 12, -6.5, 3])
def test_gain_in_range_parses(gain: float) -> None:
    song = valid_song()
    set_track(song, "gain", gain)
    assert parse_song(song).tracks[0].gain == gain


@pytest.mark.parametrize("gain", [-60.5, 12.5, True, "loud", float("nan"), float("inf")])
def test_bad_gain_is_reported_with_other_problems(gain: object) -> None:
    song = valid_song()
    set_track(song, "gain", gain)
    set_bpm(song, 0)

    with pytest.raises(SongError) as caught:
        parse_song(song)

    assert any(problem.startswith("tracks[0].gain: ") for problem in caught.value.problems)
    assert any(problem.startswith("bpm: ") for problem in caught.value.problems)


def test_bad_gain_message_names_the_range_and_value() -> None:
    song = valid_song()
    set_track(song, "gain", "loud")
    with pytest.raises(SongError) as caught:
        parse_song(song)
    assert caught.value.problems == [
        "tracks[0].gain: must be a number of decibels from -60 to 12, got 'loud'"
    ]


def test_integer_too_large_for_a_float_is_a_song_error() -> None:
    song = valid_song()
    set_track(song, "gain", 10**400)
    set_bpm(song, 10**400)
    with pytest.raises(SongError) as caught:
        parse_song(song)
    assert [problem.split(":")[0] for problem in caught.value.problems] == ["bpm", "tracks[0].gain"]


def test_integer_gain_is_stored_as_a_float() -> None:
    song = valid_song()
    set_track(song, "gain", -6)
    gain = parse_song(song).tracks[0].gain
    assert gain == -6.0
    assert type(gain) is float


def test_numpy_gain_writes_as_a_plain_float(tmp_path: Path) -> None:
    note = (Note(beat=0, pitch=60, length=1, velocity=100),)
    song = Song(bpm=120, tracks=(Track("lead", "pedalboard", "Serum", note, gain=np.float64(-6)),))
    path = tmp_path / "song.yaml"

    write_song(song, path)

    assert load_song(path).tracks[0].gain == -6.0


def test_numbers_write_in_one_spelling(tmp_path: Path) -> None:
    def write(beat: float, length: float, bpm: float) -> str:
        note = (Note(beat=beat, pitch=60, length=length, velocity=100),)
        path = tmp_path / "song.yaml"
        write_song(Song(bpm=bpm, tracks=(Track("lead", "pedalboard", "Serum", note),)), path)
        return path.read_text()

    assert write(0, 1, 120) == write(0.0, np.float64(1.0), 120.0)
    assert write(np.float64(0.5), 1, 120) == write(0.5, 1, 120)
    assert "beat: 0.5" in write(np.float64(0.5), 1, 120)


class Pitch(int):
    pass


def test_unrepresentable_value_is_a_song_error(tmp_path: Path) -> None:
    note = (Note(beat=0.5, pitch=Pitch(60), length=1, velocity=100),)
    song = Song(bpm=120, tracks=(Track("lead", "pedalboard", "Serum", note),))
    with pytest.raises(SongError, match="cannot write"):
        write_song(song, tmp_path / "song.yaml")
    assert list(tmp_path.iterdir()) == []


def test_gain_from_yaml_nan_and_inf_is_rejected(tmp_path: Path) -> None:
    for text in (".nan", ".inf"):
        path = tmp_path / "song.yaml"
        path.write_text(
            yaml.safe_dump(valid_song()).replace("  name: lead", f"  gain: {text}\n  name: lead")
        )
        with pytest.raises(SongError, match="gain"):
            load_song(path)


def gain_song() -> Song:
    note = (Note(beat=0, pitch=60, length=1, velocity=100),)
    return Song(
        bpm=120,
        tracks=(
            Track("lead", "pedalboard", "Serum", note, gain=-6),
            Track("bass", "pedalboard", "Serum", note),
        ),
    )


def test_song_to_data_writes_gain_between_sound_and_notes_only_when_set() -> None:
    lead, bass = song_to_data(gain_song())["tracks"]
    assert list(lead) == ["name", "engine", "sound", "gain", "notes"]
    assert lead["gain"] == -6
    assert list(bass) == ["name", "engine", "sound", "notes"]


def test_song_to_data_is_the_song_format() -> None:
    note: NoteData = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}
    expected: SongData = {
        "bpm": 120,
        "tracks": [
            {
                "name": "lead",
                "engine": "pedalboard",
                "sound": "Serum",
                "gain": -6.0,
                "notes": [note],
            },
            {"name": "bass", "engine": "pedalboard", "sound": "Serum", "notes": [note]},
        ],
    }

    data = song_to_data(gain_song())

    assert data == expected
    assert list(data) == ["bpm", "tracks"]
    assert [list(track) for track in data["tracks"]] == [
        ["name", "engine", "sound", "gain", "notes"],
        ["name", "engine", "sound", "notes"],
    ]
    assert [list(note) for track in data["tracks"] for note in track["notes"]] == [
        ["beat", "pitch", "length", "velocity"]
    ] * 2


def test_song_with_gains_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_song(gain_song(), path)

    assert load_song(path) == gain_song()
    assert "gain" not in yaml.safe_load(path.read_text())["tracks"][1]


# Controller changes


def cc_song(*changes: object) -> dict[str, object]:
    song = valid_song()
    first_track(song)["cc"] = list(changes)
    return song


def cc_problems(*changes: object) -> list[str]:
    song = cc_song(*changes)
    set_bpm(song, -1)  # Another problem, so controller problems are reported alongside it.
    with pytest.raises(SongError) as error:
        parse_song(song)
    assert "bpm: must be a positive number, got -1" in error.value.problems
    return [p for p in error.value.problems if not p.startswith("bpm")]


def test_controller_changes_parse_in_listed_order_with_step_by_default() -> None:
    song = parse_song(
        cc_song(
            {"beat": 0, "control": 1, "value": 0, "shape": "linear"},
            {"beat": 2, "control": 74, "value": 90},
            {"beat": 2, "control": 1, "value": 127},
        )
    )

    assert song.tracks[0].cc == (
        ControlChange(0, 1, 0, "linear"),
        ControlChange(2, 74, 90, "step"),
        ControlChange(2, 1, 127, "step"),
    )


def test_empty_cc_equals_no_cc() -> None:
    assert parse_song(cc_song()).tracks[0].cc == ()
    assert parse_song(cc_song()) == parse_song(valid_song())


def good_cc(**changes: object) -> dict[str, object]:
    return {"beat": 0, "control": 1, "value": 0, **changes}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (["x"], "tracks[0].cc[0]: must be a mapping, got 'x'"),
        ([good_cc(depth=1)], "tracks[0].cc[0]: unknown key 'depth'"),
        ([{"beat": 0, "control": 1}], "tracks[0].cc[0]: missing key 'value'"),
        ([good_cc(beat=-1)], "tracks[0].cc[0].beat: must be a number that is not negative, got -1"),
        (
            [good_cc(beat="1")],
            "tracks[0].cc[0].beat: must be a number that is not negative, got '1'",
        ),
        (
            [good_cc(beat=4), good_cc(beat=2)],
            "tracks[0].cc[1].beat: must not be earlier than the previous change at beat 4, got 2",
        ),
        (
            [good_cc(control=-1)],
            "tracks[0].cc[0].control: must be an integer from 0 to 119, got -1",
        ),
        (
            [good_cc(control=120)],
            "tracks[0].cc[0].control: must be an integer from 0 to 119, got 120",
        ),
        (
            [good_cc(control=1.0)],
            "tracks[0].cc[0].control: must be an integer from 0 to 119, got 1.0",
        ),
        (
            [good_cc(control=True)],
            "tracks[0].cc[0].control: must be an integer from 0 to 119, got True",
        ),
        ([good_cc(value=-1)], "tracks[0].cc[0].value: must be an integer from 0 to 127, got -1"),
        ([good_cc(value=128)], "tracks[0].cc[0].value: must be an integer from 0 to 127, got 128"),
        ([good_cc(value=1.0)], "tracks[0].cc[0].value: must be an integer from 0 to 127, got 1.0"),
        ([good_cc(shape="curve")], "tracks[0].cc[0].shape: must be step or linear, got 'curve'"),
        (
            [good_cc(shape="linear"), good_cc(beat=1, control=74)],
            "tracks[0].cc[0].shape: linear needs a later change to control 1",
        ),
        (
            [good_cc(shape="linear"), good_cc(value=9)],
            "tracks[0].cc[0].shape: linear needs a later change to control 1",
        ),
    ],
)
def test_bad_controller_change_is_reported_with_other_problems(
    changes: list[object], message: str
) -> None:
    assert cc_problems(*changes) == [message]


def test_cc_that_is_not_a_list_is_reported_with_other_problems() -> None:
    song = valid_song()
    first_track(song)["cc"] = "x"
    set_bpm(song, -1)
    with pytest.raises(SongError) as error:
        parse_song(song)
    assert error.value.problems == [
        "bpm: must be a positive number, got -1",
        "tracks[0].cc: must be a list of controller changes, got 'x'",
    ]


def test_bad_ramp_target_reports_only_its_own_problem() -> None:
    problems = cc_problems(good_cc(shape="linear"), good_cc(beat=1, value=200))

    assert problems == ["tracks[0].cc[1].value: must be an integer from 0 to 127, got 200"]


def cc_track_song() -> Song:
    note = Note(0, 60, 1, 100)
    changes = (ControlChange(0, 1, 0, "linear"), ControlChange(2, 1, 127))
    return Song(
        120,
        (
            Track("lead", "pedalboard", "Serum", (note,), gain=-6.0, cc=changes),
            Track("bass", "pedalboard", "Serum", (note,)),
        ),
    )


def test_song_to_data_writes_cc_after_notes_and_shape_only_for_linear() -> None:
    data = song_to_data(cc_track_song())

    lead, bass = data["tracks"]
    assert list(lead) == ["name", "engine", "sound", "gain", "notes", "cc"]
    assert list(bass) == ["name", "engine", "sound", "notes"]
    assert lead.get("cc") == [
        {"beat": 0, "control": 1, "value": 0, "shape": "linear"},
        {"beat": 2, "control": 1, "value": 127},
    ]


def test_song_with_controller_changes_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_song(cc_track_song(), path)

    assert load_song(path) == cc_track_song()


SCRIPT_HEADER = """from poser.song import ControlChange, Note, Song, Track
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
    return Song(120, (Track("lead", "pedalboard", "Serum", (note,), gain=-3),))
""",
    )

    song = load_song(path)

    assert song == Song(
        120, (Track("lead", "pedalboard", "Serum", (Note(0, 60, 1, 100),), gain=-3.0),)
    )


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
        load_song(path)

    assert len(error.value.problems) == 1
    assert str(path) in error.value.problems[0]
    assert message in error.value.problems[0]


def test_unknown_song_suffix_is_a_song_error(tmp_path: Path) -> None:
    path = tmp_path / "tune.json"
    path.write_text("{}")

    with pytest.raises(SongError) as error:
        load_song(path)

    assert len(error.value.problems) == 1
    assert str(path) in error.value.problems[0]
    assert ".yaml, .yml, or .py" in error.value.problems[0]


def test_script_song_is_validated_like_yaml(tmp_path: Path) -> None:
    pitch_path = write_script(
        tmp_path,
        """
def build():
    return Song(120, (Track("lead", "pedalboard", "Serum", (Note(0, 200, 1, 100),)),))
""",
        "pitch.py",
    )
    ramp_path = write_script(
        tmp_path,
        """
def build():
    notes = (Note(0, 60, 1, 100),)
    ramp = (ControlChange(0, 1, 0, "linear"),)
    return Song(120, (Track("lead", "pedalboard", "Serum", notes, cc=ramp),))
""",
        "ramp.py",
    )

    with pytest.raises(SongError) as pitch_error:
        load_song(pitch_path)
    with pytest.raises(SongError) as ramp_error:
        load_song(ramp_path)

    assert pitch_error.value.problems == [
        f"{pitch_path}: Note.pitch: must be an integer from 0 to 127, got 200"
    ]
    assert ramp_error.value.problems == [
        f"{ramp_path}: Track.cc[0].shape: linear needs a later change to control 1"
    ]


def test_script_and_yaml_with_one_stem_both_load(tmp_path: Path) -> None:
    write_script(
        tmp_path,
        "def build():\n    return Song(90, (Track('lead', 'pedalboard', 'Serum', (Note(0, 60, 1, 100),)),))\n",
        "x.py",
    )
    (tmp_path / "x.yaml").write_text(yaml.safe_dump(valid_song()))

    assert load_song(tmp_path / "x.py").bpm == 90
    assert load_song(tmp_path / "x.yaml").bpm == 120


@pytest.mark.parametrize("where", ["import", "build"])
def test_script_song_that_exits_is_a_song_error(tmp_path: Path, where: str) -> None:
    exits = "import sys\nsys.exit(2)\n"
    body = exits if where == "import" else "def build():\n    import sys\n    sys.exit(2)\n"
    path = write_script(tmp_path, body)

    with pytest.raises(SongError) as error:
        load_song(path)

    assert "SystemExit" in error.value.problems[0]


@pytest.mark.parametrize(
    ("build", "problem"),
    [
        ("Song(120, None)", "Song.tracks:"),
        ('Song(120, (Track("lead", "pedalboard", "Serum", None),))', "Track.notes:"),
        (
            'Song(120, (Track("lead", "pedalboard", "Serum", ((0, 60, 1, 100),)),))',
            "Track.notes[0]:",
        ),
    ],
)
def test_misshapen_script_song_reports_the_path(tmp_path: Path, build: str, problem: str) -> None:
    path = write_script(tmp_path, f"def build():\n    return {build}\n")

    with pytest.raises(SongError) as error:
        load_song(path)

    assert any(p.startswith(f"{path}: {problem}") for p in error.value.problems), (
        error.value.problems
    )


def construct[T](cls: type[T], **values: object) -> T:
    """Call a song class with values of any type, as a script without a type checker may."""
    return cast("Callable[..., T]", cls)(**values)


def make_track(**changes: object) -> Track:
    values: dict[str, object] = {
        "name": "lead",
        "engine": "pedalboard",
        "sound": "Serum",
        "notes": (Note(0, 60, 1, 100),),
    }
    return construct(Track, **(values | changes))


NOTE: dict[str, object] = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}
CHANGE: dict[str, object] = {"beat": 0, "control": 1, "value": 0}


def make_note(**changes: object) -> Note:
    return construct(Note, **(NOTE | changes))


def make_change(**changes: object) -> ControlChange:
    return construct(ControlChange, **(CHANGE | changes))


def make_song(bpm: object, tracks: object) -> Song:
    return construct(Song, bpm=bpm, tracks=tracks)


@pytest.mark.parametrize(
    ("build", "location"),
    [
        (lambda: make_note(beat=-1), "Note.beat:"),
        (lambda: make_note(beat="0"), "Note.beat:"),
        (lambda: make_note(pitch=128), "Note.pitch:"),
        (lambda: make_note(pitch=60.0), "Note.pitch:"),
        (lambda: make_note(length=0), "Note.length:"),
        (lambda: make_note(velocity=0), "Note.velocity:"),
        (lambda: make_note(velocity=True), "Note.velocity:"),
        (lambda: make_change(beat=-0.5), "ControlChange.beat:"),
        (lambda: make_change(control=120), "ControlChange.control:"),
        (lambda: make_change(value=128), "ControlChange.value:"),
        (lambda: make_change(shape="curve"), "ControlChange.shape:"),
        (lambda: make_track(name="../victim"), "Track.name:"),
        (lambda: make_track(name=""), "Track.name:"),
        (lambda: make_track(engine="other"), "Track.engine:"),
        (lambda: make_track(sound=""), "Track.sound:"),
        (lambda: make_track(gain=13), "Track.gain:"),
        (lambda: make_track(gain="0"), "Track.gain:"),
        (lambda: make_track(notes=()), "Track.notes:"),
        (lambda: make_track(notes=None), "Track.notes:"),
        (lambda: make_track(notes=((0, 60, 1, 100),)), "Track.notes[0]:"),
        (lambda: make_track(cc=None), "Track.cc:"),
        (lambda: make_track(cc=({"beat": 0},)), "Track.cc[0]:"),
        (
            lambda: make_track(cc=(ControlChange(2, 1, 0), ControlChange(1, 1, 0))),
            "Track.cc[1].beat:",
        ),
        (lambda: make_track(cc=(ControlChange(0, 1, 0, "linear"),)), "Track.cc[0].shape:"),
        (lambda: make_song(0, (make_track(),)), "Song.bpm:"),
        (lambda: make_song(120, ()), "Song.tracks:"),
        (lambda: make_song(120, None), "Song.tracks:"),
        (lambda: make_song(120, ("lead",)), "Song.tracks[0]:"),
        (lambda: make_song(120, (make_track(), make_track())), "Song.tracks:"),
    ],
)
def test_invalid_object_raises_with_its_class_location(
    build: Callable[[], object], location: str
) -> None:
    with pytest.raises(SongError) as error:
        build()

    assert len(error.value.problems) == 1, error.value.problems
    assert error.value.problems[0].startswith(location), error.value.problems


def test_note_reports_every_problem_at_once() -> None:
    with pytest.raises(SongError) as error:
        Note(beat=-1, pitch=200, length=0, velocity=0)

    assert [p.split(":")[0] for p in error.value.problems] == [
        "Note.beat",
        "Note.pitch",
        "Note.length",
        "Note.velocity",
    ]


def test_track_name_cannot_leave_the_render_directory() -> None:
    with pytest.raises(SongError) as error:
        Track(name="../victim", engine="pedalboard", sound="x", notes=(Note(0, 60, 1, 100),))

    assert error.value.problems == [
        "Track.name: must contain only letters, digits, '_', and '-', got '../victim'"
    ]


def test_song_without_tracks_is_a_song_error() -> None:
    with pytest.raises(SongError) as error:
        Song(bpm=120, tracks=())

    assert error.value.problems == ["Song.tracks: must list at least one track"]


def test_track_names_differing_in_case_are_duplicates() -> None:
    with pytest.raises(SongError) as error:
        Song(120, (make_track(name="lead"), make_track(name="Lead")))

    assert error.value.problems == ["Song.tracks: duplicate track name 'Lead'"]


def test_linear_change_without_a_later_target_is_a_song_error() -> None:
    with pytest.raises(SongError) as error:
        make_track(cc=(ControlChange(0, 1, 0, "linear"),))

    assert error.value.problems == ["Track.cc[0].shape: linear needs a later change to control 1"]


def test_lists_are_stored_as_tuples_and_gain_as_a_float() -> None:
    note = Note(0, 60, 1, 100)
    from_lists = make_track(notes=[note], cc=[])
    from_tuples = make_track(notes=(note,), cc=())

    assert from_lists == from_tuples
    assert isinstance(from_lists.notes, tuple)
    assert isinstance(from_lists.cc, tuple)
    gain = make_track(gain=-3).gain
    assert gain == -3.0
    assert isinstance(gain, float)
    assert isinstance(make_song(120, [from_lists]).tracks, tuple)


def test_generator_of_notes_is_a_song_error() -> None:
    with pytest.raises(SongError) as error:
        make_track(notes=(n for n in (Note(0, 60, 1, 100),)))

    assert [p.split(":")[0] for p in error.value.problems] == ["Track.notes"]
