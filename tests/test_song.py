"""Tests for the song format. No plugins or rendering."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from vsti_composer.plugin_check import PROJECT_DIR
from vsti_composer.song import Note, SongError, Track, load_song, parse_song


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


def test_demo_song_loads_with_two_serum_tracks_and_eight_notes() -> None:
    song = load_song(PROJECT_DIR / "songs" / "demo.yaml")

    assert len(song.tracks) == 2
    assert {track.sound for track in song.tracks} == {"Serum"}
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
        (lambda s: set_track(s, "gain", 1), "tracks[0]: unknown key 'gain'"),
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
