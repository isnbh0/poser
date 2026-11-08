"""The Pedalboard instrument on the plugins installed on this machine. Deselected by default."""

import base64
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest
from contracts import check_instrument_contract
from helpers import (
    CAPTURE_STATE,
    REAL_FORMATS,
    fxp_bytes,
    real_composition,
    real_pedalboard_instruments,
    strongest_hz,
)

from poser.adapters.pedalboard.config import from_options
from poser.adapters.pedalboard.formats import SavedFormat, SavedSound
from poser.adapters.pedalboard.instrument import PedalboardInstrument
from poser.adapters.pedalboard.state import component_state
from poser.core.audio import Audio, read_raw_audio
from poser.core.names import Identifier
from poser.core.performance import NoteOff, NoteOn, Performance, perform
from poser.core.ports import RenderRequest, ResolvedSound
from poser.core.song import Note, Song, Track
from poser.core.sounds import SoundRef

pytestmark = pytest.mark.real_plugins

SHORT = Performance(27, (NoteOn(0, 60, 100), NoteOff(20, 60)))


def render(
    instrument: PedalboardInstrument, ref: SoundRef, performance: Performance, path: Path
) -> Audio:
    sound = instrument.resolve(ref)
    assert isinstance(sound, ResolvedSound), sound
    failure = instrument.render(RenderRequest(sound, performance, path), 120)
    assert failure is None, failure
    audio = read_raw_audio(path, performance.samples)
    assert isinstance(audio, Audio), audio
    return audio


def test_each_instrument_meets_the_instrument_contract(tmp_path: Path) -> None:
    for name, instrument in real_pedalboard_instruments(tmp_path).items():
        (tmp_path / name).mkdir()
        check_instrument_contract(instrument, SoundRef(Identifier(name)), tmp_path / name)


def test_a_render_has_exactly_the_performance_s_frames(tmp_path: Path) -> None:
    presets = 0
    for name, instrument in real_pedalboard_instruments(tmp_path).items():
        refs = [entry.ref for entry in instrument.sounds().entries][:2]
        presets += len(refs) - 1
        for index, ref in enumerate(refs):
            audio = render(instrument, ref, SHORT, tmp_path / f"{name}-{index}.npy")
            assert audio.frames == SHORT.samples
    assert presets, "no instrument lists a preset"


def captured_state(instrument_path: Path, tmp_path: Path) -> bytes:
    state_file = tmp_path / "state.b64"
    subprocess.run(
        [sys.executable, "-c", CAPTURE_STATE, str(instrument_path), str(state_file)],
        check=True,
        timeout=120,
    )
    return base64.b64decode(state_file.read_text())


def test_a_captured_state_reaches_the_render(tmp_path: Path) -> None:
    sounds_dir = tmp_path / "sounds"
    composition = real_composition(tmp_path)
    (spec,) = [item for item in composition.config.instruments if item.name == "serum"]
    config = from_options(spec.name, spec.spec.options, composition.context.root)
    state = captured_state(config.path, tmp_path)

    # As save-sound stores it.
    (sounds_dir / config.name).mkdir(parents=True)
    saved = SavedFormat()
    sound = SavedSound("three octaves down", state)
    saved.write_sound(sounds_dir / config.name / f"low{saved.suffix}", sound)
    # As an installed preset holds it.
    presets = tmp_path / "presets"
    (presets / "Test").mkdir(parents=True)
    (presets / "Test" / "low.fxp").write_bytes(fxp_bytes(component_state(state), name="low"))
    serum = PedalboardInstrument(
        dataclasses.replace(config, sources={Identifier("test"): presets}), sounds_dir, REAL_FORMATS
    )

    song = Song(bpm=60, tracks=(Track("lead", "serum", (Note(0, 60, 1.5, 100),)),))
    performance = perform(song, song.tracks[0])
    default = render(serum, SoundRef(config.name), performance, tmp_path / "default.npy")
    low = render(serum, SoundRef(config.name, "saved/low"), performance, tmp_path / "saved.npy")
    preset_ref = SoundRef(config.name, "test/Test/low")
    preset = render(serum, preset_ref, performance, tmp_path / "preset.npy")

    assert strongest_hz(default.samples) == pytest.approx(262, abs=5)
    assert strongest_hz(low.samples) == pytest.approx(33, abs=3)
    assert strongest_hz(preset.samples) == pytest.approx(33, abs=3)
