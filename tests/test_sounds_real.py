"""Proves a saved sound's state reaches a real render, by pitch. Deselected by default."""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from pedalboard.io import ReadableAudioFile

from vsti_composer import render
from vsti_composer.plugin_check import DEFAULT_CONFIG, SAMPLE_RATE, load_config
from vsti_composer.song import Note, Song, Track
from vsti_composer.sounds import SavedSound, write_sound

# Runs in a subprocess, so plugin code stays out of the test process. Serum's oscillator A octave
# at raw value 0.1 is -3 octaves, which moves middle C from about 262 Hz to about 33 Hz. The state
# is captured as `save-sound` captures it after the window closes.
CAPTURE_STATE = """
import sys
from pedalboard import load_plugin
from vsti_composer.sounds import capture_state, encode_state
plugin = load_plugin(sys.argv[1])
plugin.parameters["a_octave_oct"].raw_value = 0.1
open(sys.argv[2], "w").write(encode_state(capture_state(plugin)))
"""


def strongest_hz(stem: str) -> float:
    with ReadableAudioFile(stem) as audio_file:  # ty: ignore[no-matching-overload]
        audio = audio_file.read(audio_file.frames)
    mono = audio.mean(axis=0)[int(0.2 * SAMPLE_RATE) : int(1.2 * SAMPLE_RATE)]
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    freqs = np.fft.rfftfreq(len(mono), 1 / SAMPLE_RATE)
    band = freqs > 20
    return float(freqs[band][np.argmax(spectrum[band])])


@pytest.mark.real_plugins
def test_saved_sound_state_reaches_the_render(tmp_path: Path) -> None:
    specs = load_config(DEFAULT_CONFIG)
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
    [default] = render.render_song(note_song("Serum"), "default", specs, out, sounds_dir=sounds_dir)
    [saved] = render.render_song(note_song("low"), "saved", specs, out, sounds_dir=sounds_dir)

    assert default.status is render.TrackStatus.RENDERED, default.detail
    assert saved.status is render.TrackStatus.RENDERED, saved.detail
    assert default.stem is not None
    assert saved.stem is not None
    assert strongest_hz(default.stem) == pytest.approx(262, abs=5)
    assert strongest_hz(saved.stem) == pytest.approx(33, abs=3)
