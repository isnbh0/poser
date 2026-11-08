"""Proves a saved sound's state reaches a real render, by pitch. Deselected by default."""

import subprocess
import sys
from pathlib import Path

import pytest
from helpers import CAPTURE_STATE, real_specs, strongest_hz

from poser import render
from poser.song import Note, Song, Track
from poser.sounds import SavedSound, write_sound


@pytest.mark.real_plugins
def test_saved_sound_state_reaches_the_render(tmp_path: Path) -> None:
    specs = real_specs()
    serum = next(spec for spec in specs if spec.name == "Serum")
    state_file = tmp_path / "state.b64"
    subprocess.run(
        [sys.executable, "-c", CAPTURE_STATE, serum.path, str(state_file)], check=True, timeout=120
    )
    sounds_dir = tmp_path / "sounds"
    write_sound(
        sounds_dir, SavedSound("low", "Serum", "three octaves down", state_file.read_text())
    )

    def note_song(sound: str) -> Song:
        return Song(bpm=60, tracks=(Track("lead", "pedalboard", sound, (Note(0, 60, 1.5, 100),)),))

    out = tmp_path / "renders"
    [default] = render.render_song(
        note_song("Serum"), out / "default", specs, sounds_dir=sounds_dir
    )
    [saved] = render.render_song(note_song("low"), out / "saved", specs, sounds_dir=sounds_dir)

    assert default.status is render.TrackStatus.RENDERED, default.detail
    assert saved.status is render.TrackStatus.RENDERED, saved.detail
    assert default.stem is not None
    assert saved.stem is not None
    assert strongest_hz(default.stem) == pytest.approx(262, abs=5)
    assert strongest_hz(saved.stem) == pytest.approx(33, abs=3)
