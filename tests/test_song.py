"""Tests for the song format. No plugins or rendering."""

import os
import stat
from pathlib import Path
from typing import Any, Self

import numpy as np
import pytest
import yaml

from vsti_composer import song as song_module
from vsti_composer.music import chord, notes, pitch, scale, swing
from vsti_composer.plugin_check import PROJECT_DIR
from vsti_composer.song import (
    Note,
    Song,
    SongError,
    Track,
    load_song,
    parse_song,
    song_to_data,
    write_song,
)


def valid_note() -> dict[str, Any]:
    return {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}


def valid_track(name: str = "lead") -> dict[str, Any]:
    return {"name": name, "engine": "pedalboard", "sound": "Serum", "notes": [valid_note()]}


def valid_song() -> dict[str, Any]:
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
    song = load_song(PROJECT_DIR / "songs" / "demo.yaml")

    assert len(song.tracks) == 2
    assert all(track.sound.startswith("Serum:") for track in song.tracks)
    assert sum(len(track.notes) for track in song.tracks) >= 8


def test_seconds_uses_bpm() -> None:
    assert parse_song({**valid_song(), "bpm": 90}).seconds(3) == pytest.approx(2.0)


def set_bpm(song: dict[str, Any], value: Any) -> None:
    song["bpm"] = value


def set_track(song: dict[str, Any], key: str, value: Any) -> None:
    song["tracks"][0][key] = value


def set_note(song: dict[str, Any], key: str, value: Any) -> None:
    song["tracks"][0]["notes"][0][key] = value


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
        (lambda s: s["tracks"][0].pop("sound"), "tracks[0]: missing key 'sound'"),
        (lambda s: s["tracks"][0]["notes"][0].pop("pitch"), "notes[0]: missing key 'pitch'"),
    ],
)
def test_invalid_song_names_the_problem(change: Any, expected: str) -> None:
    song = valid_song()
    change(song)

    with pytest.raises(SongError) as raised:
        parse_song(song)

    assert expected in str(raised.value)


@pytest.mark.parametrize("data", [None, [], "song", 3])
def test_song_that_is_not_a_mapping_is_rejected(data: Any) -> None:
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
    source = (PROJECT_DIR / "src" / "vsti_composer" / "song.py").read_text()
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


def test_writing_an_invalid_song_raises_and_keeps_the_old_file(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text("old contents\n")
    bad = Song(bpm=120, tracks=(Track("lead", "pedalboard", "Serum", (Note(0, 60, 1, 0),)),))

    with pytest.raises(SongError, match="velocity"):
        write_song(bad, path)

    assert path.read_text() == "old contents\n"
    assert [p.name for p in tmp_path.iterdir()] == ["song.yaml"]


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
def test_bad_gain_is_reported_with_other_problems(gain: Any) -> None:
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


def test_unrepresentable_value_is_a_song_error(tmp_path: Path) -> None:
    note = (Note(beat=np.float64(0.5), pitch=60, length=1, velocity=100),)
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


def test_song_with_gains_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_song(gain_song(), path)

    assert load_song(path) == gain_song()
    assert "gain" not in yaml.safe_load(path.read_text())["tracks"][1]
