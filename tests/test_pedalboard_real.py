"""The Pedalboard instrument on the plugins installed on this machine. Deselected by default."""

import base64
import dataclasses
import os
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import numpy as np
import pytest
from contracts import check_instrument_contract
from helpers import (
    CAPTURE_STATE,
    NON_ASCII_DIR,
    REAL_FORMATS,
    HarmonicMetrics,
    assert_sine_saw_pair,
    fxp_bytes,
    harmonic_metrics,
    real_composition,
    real_pedalboard_instruments,
    render_without_restoring_state,
    strongest_hz,
)

from poser.adapters.filesystem import native
from poser.adapters.pedalboard import instrument as instrument_module
from poser.adapters.pedalboard.config import from_options
from poser.adapters.pedalboard.formats import SavedFormat, SavedSound
from poser.adapters.pedalboard.instrument import (
    PedalboardInstrument,
    payload_state,
    plugin_version,
)
from poser.adapters.pedalboard.plugin_files import windows_binary
from poser.adapters.pedalboard.state import component_state
from poser.core.audio import SAMPLE_RATE, SILENCE_DBFS, Audio, measure_peak_dbfs, read_raw_audio
from poser.core.names import Identifier
from poser.core.performance import NoteOff, NoteOn, Performance, perform
from poser.core.ports import EffectRenderRequest, RenderRequest, ResolvedEffect, ResolvedSound
from poser.core.rendering import late_start_s
from poser.core.song import Note, Song, Track
from poser.core.sounds import SoundRef
from poser.reference_setups.surge.showcase import build as showcase

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


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows loads a VST3 from its x64 binary")
def test_a_windows_plugin_loads_from_its_binary_as_from_its_bundle(tmp_path: Path) -> None:
    composition = real_composition(tmp_path)
    pedalboard = {
        name
        for name, instrument in composition.instruments().items()
        if isinstance(instrument, PedalboardInstrument)
    }
    # Long enough for a plugin's latency to pass, unlike SHORT.
    performance = Performance(SAMPLE_RATE // 2, (NoteOn(0, 60, 100), NoteOff(SAMPLE_RATE // 4, 60)))
    bundles = 0
    for item in composition.config.instruments:
        if item.name not in pedalboard:
            continue
        config = from_options(item.name, item.spec.options, composition.context.root)
        if not config.path.is_dir():
            continue
        bundles += 1
        binary = windows_binary(config.path)
        version = plugin_version(config.path)
        assert version is not None, f"{config.path} has no readable version"
        assert plugin_version(binary) == version
        for index, path in enumerate((config.path, binary)):
            instrument = PedalboardInstrument(
                dataclasses.replace(config, path=path), tmp_path / "sounds", REAL_FORMATS, native()
            )
            ref = SoundRef(Identifier(item.name))
            audio = render(instrument, ref, performance, tmp_path / f"{item.name}-{index}.npy")
            assert audio.frames == performance.samples
            assert measure_peak_dbfs(audio.samples) > SILENCE_DBFS, path
    assert bundles, "no Pedalboard instrument names a bundle directory"


def test_a_plugin_loads_from_a_non_ascii_path(tmp_path: Path) -> None:
    """A copy of the `surge` bundle in a folder named in Hangul and a Latin accent loads and
    sounds. Only Surge is copied: a licensed or Audio Unit plugin may refuse to load from a copy."""
    composition = real_composition(tmp_path)
    items = [item for item in composition.config.instruments if item.name == "surge"]
    if not items:
        pytest.skip("the root poser.toml defines no 'surge' instrument")
    (item,) = items
    config = from_options(item.name, item.spec.options, composition.context.root)
    if not (config.path.is_dir() and config.path.suffix == ".vst3"):
        pytest.skip(f"the 'surge' instrument's path {config.path} is not a .vst3 bundle")
    # Windows loads the binary named after the bundle, so the copy keeps the bundle's name.
    copy = tmp_path / NON_ASCII_DIR / config.path.name
    shutil.copytree(config.path, copy, symlinks=True)
    instrument = PedalboardInstrument(
        dataclasses.replace(config, path=copy), tmp_path / "sounds", REAL_FORMATS, native()
    )
    # Long enough for a plugin's latency to pass, unlike SHORT.
    performance = Performance(SAMPLE_RATE // 2, (NoteOn(0, 60, 100), NoteOff(SAMPLE_RATE // 4, 60)))
    audio = render(instrument, SoundRef(Identifier("surge")), performance, tmp_path / "copy.npy")
    assert audio.frames == performance.samples
    assert measure_peak_dbfs(audio.samples) > SILENCE_DBFS


@pytest.mark.parametrize(
    "delay_samples", [0, SAMPLE_RATE // 2], ids=["first-note", "authored-silence"]
)
def test_surge_restored_preset_keeps_its_first_note(tmp_path: Path, delay_samples: int) -> None:
    instrument = real_pedalboard_instruments(tmp_path).get("surge")
    if instrument is None:
        pytest.skip("the root poser.toml has no Pedalboard Surge instrument named 'surge'")
    song = showcase()
    bass = next(track for track in song.tracks if track.name == "bass")
    full = perform(song.graph(), bass)
    prefix = Performance(
        4 * SAMPLE_RATE, tuple(event for event in full.events if event.sample < 4 * SAMPLE_RATE)
    )
    performance = Performance(
        prefix.samples + delay_samples,
        tuple(
            dataclasses.replace(event, sample=event.sample + delay_samples)
            for event in prefix.events
        ),
    )
    first = next(event for event in performance.events if isinstance(event, NoteOn))
    off = next(
        event
        for event in performance.events
        if isinstance(event, NoteOff) and event.pitch == first.pitch and event.sample > first.sample
    )
    sound = instrument.resolve(bass.sound_ref)
    assert isinstance(sound, ResolvedSound), sound
    path = tmp_path / "first-note.npy"
    # Exercise the actual adapter once: core retries or a diagnostic wrapper must not hide a miss.
    failure = instrument.render(RenderRequest(sound, performance, path, attempt=1), 120)
    assert failure is None, failure
    audio = read_raw_audio(path, performance.samples)
    assert isinstance(audio, Audio), audio
    assert audio.frames == performance.samples
    assert measure_peak_dbfs(audio.samples[:, first.sample : off.sample]) > SILENCE_DBFS
    assert late_start_s(audio.samples, performance) is None
    if delay_samples:
        assert measure_peak_dbfs(audio.samples[:, : first.sample]) < -100


def test_surge_factory_presets_take_effect_headlessly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instrument = real_pedalboard_instruments(tmp_path).get("surge")
    if instrument is None:
        pytest.skip("the root poser.toml has no Pedalboard Surge instrument named 'surge'")
    sine = instrument.resolve(SoundRef(Identifier("surge"), "presets/Templates/Init Sine"))
    saw = instrument.resolve(SoundRef(Identifier("surge"), "presets/Templates/Init Saw"))
    assert isinstance(sine, ResolvedSound), sine
    assert isinstance(saw, ResolvedSound), saw
    sine_state, saw_state = payload_state(sine.payload), payload_state(saw.payload)
    assert sine_state, "Init Sine must resolve to nonempty state"
    assert saw_state, "Init Saw must resolve to nonempty state"
    assert sine_state != saw_state
    assert sine.payload != saw.payload
    assert sine.identity != saw.identity

    composition = real_composition(tmp_path)
    spec = next(item for item in composition.config.instruments if item.name == "surge")
    config = from_options(spec.name, spec.spec.options, composition.context.root)
    versions = f"pedalboard {metadata.version('pedalboard')}, Surge {plugin_version(config.path)}"
    print(f"headless factory preset selection: {versions}")
    # A4 is held throughout the 0.2-1.2 s analysis window; every request is a fresh attempt 1.
    performance = Performance(
        2 * SAMPLE_RATE, (NoteOn(0, 69, 100), NoteOff(3 * SAMPLE_RATE // 2, 69))
    )

    def measure(sound: ResolvedSound, label: str) -> HarmonicMetrics:
        path = tmp_path / f"{label}.npy"
        failure = instrument.render(RenderRequest(sound, performance, path, attempt=1), 120)
        assert failure is None, f"{label}: {failure}"
        audio = read_raw_audio(path, performance.samples)
        assert isinstance(audio, Audio), f"{label}: {audio}"
        assert audio.frames == performance.samples, label
        assert np.isfinite(audio.samples).all(), label
        peak = measure_peak_dbfs(audio.samples)
        assert peak > SILENCE_DBFS, f"{label}: peak {peak:.2f} dBFS"
        metrics = harmonic_metrics(audio.samples)
        print(
            f"{label} {sound.ref}: frames={audio.frames}, peak={peak:.2f} dBFS, "
            f"window RMS={metrics.rms_dbfs:.2f} dBFS, f0={metrics.fundamental_hz:.2f} Hz, "
            f"P1/total={metrics.fundamental_fraction:.4f}, H2/P1={metrics.h2_db:.2f} dB"
        )
        return metrics

    first_sine = measure(sine, "sine-first")
    selected_saw = measure(saw, "saw")
    last_sine = measure(sine, "sine-again")
    assert_sine_saw_pair(first_sine, selected_saw)
    assert_sine_saw_pair(last_sine, selected_saw)

    # Route only these two requests to an importable CHILD target, never replace isolation.
    # The context restores both routing and the complete inherited PYTHONPATH on exit.
    pythonpath = str(Path(__file__).resolve().parent)
    inherited = os.environ.get("PYTHONPATH")
    if inherited is not None:
        pythonpath += os.pathsep + inherited
    with monkeypatch.context() as patch:
        patch.setenv("PYTHONPATH", pythonpath)
        patch.setattr(instrument_module, "render_job", render_without_restoring_state)
        ignored_sine = measure(sine, "ignored-sine")
        ignored_saw = measure(saw, "ignored-saw")
    # Both controls must render and pass ALL health checks before semantic rejection counts.
    with pytest.raises(AssertionError, match=r"^harmonic class:"):
        assert_sine_saw_pair(ignored_sine, ignored_saw)


def captured_state(instrument_path: Path, tmp_path: Path) -> bytes:
    state_file = tmp_path / "state.b64"
    subprocess.run(
        [sys.executable, "-c", CAPTURE_STATE, str(instrument_path), str(state_file)],
        check=True,
        timeout=120,
    )
    return base64.b64decode(state_file.read_text(encoding="utf-8"))


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
    saved.write_sound(native(), sounds_dir / config.name / f"low{saved.suffix}", sound)
    # As an installed preset holds it.
    presets = tmp_path / "presets"
    (presets / "Test").mkdir(parents=True)
    (presets / "Test" / "low.fxp").write_bytes(fxp_bytes(component_state(state), name="low"))
    serum = PedalboardInstrument(
        dataclasses.replace(config, sources={Identifier("test"): presets}),
        sounds_dir,
        REAL_FORMATS,
        native(),
    )

    song = Song(bpm=60, tracks=(Track("lead", "serum", (Note(0, 60, 1.5, 100),)),))
    performance = perform(song.graph(), song.tracks[0])
    default = render(serum, SoundRef(config.name), performance, tmp_path / "default.npy")
    low = render(serum, SoundRef(config.name, "saved/low"), performance, tmp_path / "saved.npy")
    preset_ref = SoundRef(config.name, "test/Test/low")
    preset = render(serum, preset_ref, performance, tmp_path / "preset.npy")

    assert strongest_hz(default.samples) == pytest.approx(262, abs=5)
    assert strongest_hz(low.samples) == pytest.approx(33, abs=3)
    assert strongest_hz(preset.samples) == pytest.approx(33, abs=3)


@pytest.mark.parametrize(
    "preset", ["presets/Bass/BA Ampology [CFA]", "presets/Leads/LD A Bit of Luck [SD]"]
)
def test_a_long_release_preset_is_silent_until_its_note(tmp_path: Path, preset: str) -> None:
    composition = real_composition(tmp_path)
    (spec,) = [item for item in composition.config.instruments if item.name == "serum"]
    config = from_options(spec.name, spec.spec.options, composition.context.root)
    serum = PedalboardInstrument(config, tmp_path / "sounds", REAL_FORMATS, native())
    note_on = NoteOn(SAMPLE_RATE // 2, 48, 100)
    performance = Performance(3 * SAMPLE_RATE // 2, (note_on, NoteOff(SAMPLE_RATE, 48)))
    first = note_on.sample

    audio = render(serum, SoundRef(config.name, preset), performance, tmp_path / "out.npy")

    versions = f"pedalboard {metadata.version('pedalboard')}, plugin {plugin_version(config.path)}"
    before = measure_peak_dbfs(audio.samples[:, :first])
    after = measure_peak_dbfs(audio.samples[:, first:])
    assert before < -100, f"{preset} sounds before its note at {before:.1f} dBFS ({versions})"
    assert after > SILENCE_DBFS, (
        f"{preset} is silent after its note at {after:.1f} dBFS ({versions})"
    )


def test_each_effect_renders_as_many_frames_as_it_is_given(tmp_path: Path) -> None:
    """A configured effect loads and returns exactly the frames it was given.

    Whether two renders agree is the plugin's, as for an instrument: Surge XT Effects differs in
    its last bits between renders on Windows. Poser's part, a fresh instance in a fresh worker
    given the same input each time, is checked with a native effect in `test_pedalboard.py`.
    """
    composition = real_composition(tmp_path)
    effects = composition.effects()
    if not effects:
        pytest.skip("the root poser.toml names no [effects] recipes")
    audio_in = Audio(np.full((2, SAMPLE_RATE), 0.5, dtype=np.float32))
    for name, effect in effects.items():
        resolved = effect.resolve(name)
        assert isinstance(resolved, ResolvedEffect), resolved
        output = tmp_path / f"{name}.npy"
        failure = effect.render(EffectRenderRequest(audio_in, resolved, output), 120)
        assert failure is None, f"{name}: {failure}"
        rendered = read_raw_audio(output, SAMPLE_RATE)
        assert isinstance(rendered, Audio), f"{name}: {rendered}"
