"""The scsynth adapter's real-engine tests: they need SuperCollider installed.

Every test locates the engine from `SCSYNTH_PATH` or the standard app-bundle path and skips
cleanly without one, so `make check` on a machine without SuperCollider is unchanged. They are
marked `real_scsynth` and run under `make test-real`.
"""

import os
import shutil
import time
from pathlib import Path

import numpy as np
import pytest
from poser_scsynth.instrument import ScsynthInstrument, create
from poser_scsynth.synthdefs import VOICE_BYTES

from poser.cli.check import EXIT_ALL_PASS
from poser.cli.check import main as check_main
from poser.compose import Composition
from poser.core.audio import SAMPLE_RATE, Audio, read_raw_audio
from poser.core.names import Identifier, SongId
from poser.core.performance import Control, NoteOff, NoteOn, Performance
from poser.core.ports import AdapterContext, RenderRequest, ResolvedSound
from poser.core.rendering import TrackResult
from poser.core.song import Note, Song, Track

pytestmark = pytest.mark.real_scsynth

BUNDLE_CANDIDATES = (
    "/Applications/SuperCollider.app/Contents/Resources/scsynth",
    "/usr/bin/scsynth",
    "/usr/local/bin/scsynth",
)


def real_scsynth() -> Path:
    """Return the engine's path from SCSYNTH_PATH, an app bundle, or PATH, or skip."""
    env = os.environ.get("SCSYNTH_PATH")
    candidates = [Path(env)] if env else []
    candidates += [Path(path) for path in BUNDLE_CANDIDATES]
    if found := shutil.which("scsynth"):
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip("no scsynth on this machine")


@pytest.fixture
def instrument(tmp_path: Path) -> ScsynthInstrument:
    context = AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
    return create(Identifier("sc"), {"scsynth": str(real_scsynth())}, context)


def two_note_song() -> Song:
    return Song(
        120,
        (
            Track(
                "lead",
                "sc",
                (
                    Note(0, 60, 0.5, 100),
                    Note(0.75, 64, 0.75, 90),
                ),
            ),
        ),
    )


def render_song(instrument: ScsynthInstrument, tmp_path: Path, song: Song, name: str) -> Audio:
    from poser.core.performance import perform
    from poser.core.sounds import SoundRef

    resolved = instrument.resolve(SoundRef(Identifier("sc"), None))
    assert isinstance(resolved, ResolvedSound), resolved
    performance = perform(song, song.tracks[0])
    path = tmp_path / name
    assert instrument.render(RenderRequest(resolved, performance, path), 120.0) is None
    audio = read_raw_audio(path, performance.samples)
    assert isinstance(audio, Audio), audio
    return audio


def test_real_render_is_exact_finite_and_deterministic(
    instrument: ScsynthInstrument, tmp_path: Path
) -> None:
    song = two_note_song()
    first = render_song(instrument, tmp_path, song, "first.npy")
    render_song(instrument, tmp_path, song, "second.npy")
    assert first.samples.shape[1] == first.frames
    assert np.isfinite(first.samples).all()
    assert (tmp_path / "first.npy").read_bytes() == (tmp_path / "second.npy").read_bytes()


def test_real_render_peaks_audibly(instrument: ScsynthInstrument, tmp_path: Path) -> None:
    audio = render_song(instrument, tmp_path, two_note_song(), "loud.npy")
    assert float(np.abs(audio.samples).max()) > 0.05, "the demo voice rendered silence"


def test_real_voice_has_the_requested_pitch_and_releases(
    instrument: ScsynthInstrument, tmp_path: Path
) -> None:
    song = Song(120, (Track("lead", "sc", (Note(0, 69, 2, 100),)),))
    audio = render_song(instrument, tmp_path, song, "pitch.npy")
    sustain = audio.samples[0, SAMPLE_RATE // 4 : SAMPLE_RATE * 3 // 4]
    frequencies = np.fft.rfftfreq(sustain.size, 1 / SAMPLE_RATE)
    spectrum = np.abs(np.fft.rfft(sustain))
    assert frequencies[np.argmax(spectrum)] == pytest.approx(440, abs=2)
    assert np.array_equal(audio.samples[0], audio.samples[1])
    # Note-off is at 1 s; Linen's 0.15 s release must finish before this window.
    assert np.max(np.abs(audio.samples[:, SAMPLE_RATE * 3 // 2 :])) == 0


def test_real_polyphony_renders_inside_the_timeout(
    instrument: ScsynthInstrument, tmp_path: Path
) -> None:
    """64 overlapping voices, 128 events. The probe measures; only completion is asserted."""
    onsets = [
        Note(onset, 48 + (index % 24), 2.0, 70)
        for index, onset in enumerate(np.linspace(0.0, 1.0, 64))
    ]
    song = Song(120, (Track("massive", "sc", tuple(onsets)),))
    from poser.core.performance import perform
    from poser.core.sounds import SoundRef

    resolved = instrument.resolve(SoundRef(Identifier("sc"), None))
    assert isinstance(resolved, ResolvedSound)
    performance = perform(song, song.tracks[0])
    path = tmp_path / "poly.npy"
    started = time.monotonic()
    failure = instrument.render(RenderRequest(resolved, performance, path), 120.0)
    elapsed = time.monotonic() - started
    print(f"polyphony probe: 64 overlapping voices rendered in {elapsed:.2f} s")
    assert failure is None, failure
    audio = read_raw_audio(path, performance.samples)
    assert isinstance(audio, Audio)
    assert np.isfinite(audio.samples).all()


def test_real_controller_reaches_the_voice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sound whose meta maps CC 1 to the amp parameter: the same song with and without a
    controller change must render different audio — the stub cannot vouch for this."""
    source = tmp_path / "cc"
    source.mkdir()
    (source / "voice.scsyndef").write_bytes(VOICE_BYTES)
    (source / "voice.json").write_text('{"cc": {"1": "amp"}}')
    context = AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
    instrument = create(
        Identifier("sc"), {"scsynth": str(real_scsynth()), "sources": {"cc": str(source)}}, context
    )
    from poser.core.sounds import SoundRef

    resolved = instrument.resolve(SoundRef(Identifier("sc"), "cc/voice"))
    assert isinstance(resolved, ResolvedSound), resolved

    plain = Performance(SAMPLE_RATE, (NoteOn(0, 60, 100), NoteOff(SAMPLE_RATE // 2, 60)))
    steered = Performance(
        SAMPLE_RATE,
        (NoteOn(0, 60, 100), Control(SAMPLE_RATE // 4, 1, 30), NoteOff(SAMPLE_RATE // 2, 60)),
    )
    plain_path, steered_path = tmp_path / "plain.npy", tmp_path / "steered.npy"
    assert instrument.render(RenderRequest(resolved, plain, plain_path), 120.0) is None
    assert instrument.render(RenderRequest(resolved, steered, steered_path), 120.0) is None
    plain_audio = read_raw_audio(plain_path, plain.samples)
    steered_audio = read_raw_audio(steered_path, steered.samples)
    assert isinstance(plain_audio, Audio)
    assert isinstance(steered_audio, Audio)
    difference = float(np.abs(steered_audio.samples - plain_audio.samples).max())
    print(f"controller probe: max-abs difference with CC 1 mapped to amp: {difference:.4f}")
    assert difference > 0.01, "the controller change did not change the audio"


def test_real_initial_controls_and_polyphonic_broadcast(tmp_path: Path) -> None:
    from poser.core.sounds import SoundRef

    source = tmp_path / "source"
    source.mkdir()
    (source / "voice.scsyndef").write_bytes(VOICE_BYTES)
    (source / "voice.json").write_text('{"cc": {"1": "amp"}}')
    context = AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
    instrument = create(
        Identifier("sc"),
        {"scsynth": str(real_scsynth()), "sources": {"demo": str(source)}},
        context,
    )
    resolved = instrument.resolve(SoundRef(Identifier("sc"), "demo/voice"))
    assert isinstance(resolved, ResolvedSound)
    notes = (NoteOn(0, 60, 100), NoteOn(0, 64, 100))
    offs = (NoteOff(24000, 60), NoteOff(24000, 64))
    plain = Performance(SAMPLE_RATE, (*notes, *offs))
    controlled = Performance(SAMPLE_RATE, (Control(0, 1, 0), *notes, Control(12000, 1, 60), *offs))
    paths = [tmp_path / "plain.npy", tmp_path / "controlled.npy"]
    for performance, path in zip((plain, controlled), paths, strict=True):
        assert instrument.render(RenderRequest(resolved, performance, path), 120) is None
    first = read_raw_audio(paths[0], SAMPLE_RATE)
    second = read_raw_audio(paths[1], SAMPLE_RATE)
    assert isinstance(first, Audio)
    assert isinstance(second, Audio)
    assert np.max(np.abs(second.samples[:, :11000])) == 0
    # Both oscillators started together: group-wide amp=60/127 must scale their sum by 60/100.
    assert np.allclose(
        second.samples[:, 14000:20000],
        first.samples[:, 14000:20000] * 0.6,
        rtol=1e-5,
        atol=1e-6,  # Scale-before-sum and sum-before-scale differ at float32 precision.
    )


def test_real_unknown_ugen_is_load_failed_not_silent_success(
    instrument: ScsynthInstrument, tmp_path: Path
) -> None:
    from dataclasses import replace

    from poser_scsynth.formats import SoundRecipe
    from poser_scsynth.synthdefs import additive_voice, emit

    from poser.core.jsonvalue import Json
    from poser.core.ports import RenderFailure
    from poser.core.sounds import SoundRef
    from poser.core.statuses import RenderFailureStatus

    voice = additive_voice()
    missing = replace(voice.ugens[0], name="PoserUnavailableUGen")
    recipe = SoundRecipe(emit(replace(voice, ugens=(missing, *voice.ugens[1:]))), Json.of({}))
    sound = ResolvedSound(SoundRef(Identifier("sc")), "missing-ugen", recipe.payload)
    performance = Performance(SAMPLE_RATE, (NoteOn(0, 60, 100), NoteOff(24000, 60)))
    result = instrument.render(RenderRequest(sound, performance, tmp_path / "bad.npy"), 120)
    assert isinstance(result, RenderFailure)
    assert result.status is RenderFailureStatus.LOAD_FAILED
    assert "PoserUnavailableUGen" in result.detail


def test_check_instruments_accepts_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "poser.toml"
    config.write_text(
        "[instruments.sc]\n"
        'adapter = "poser_scsynth.instrument:create"\n'
        f'scsynth = "{real_scsynth()}"\n'
    )
    monkeypatch.chdir(tmp_path)
    assert check_main(["--config", str(config), "--json"]) == EXIT_ALL_PASS


# The two-engine law: one song, one scsynth leaf and one plugin leaf, mixed as one.


REPO_CONFIG = Path(__file__).resolve().parents[1] / "poser.toml"


def two_engine_composition(tmp_path: Path) -> tuple[Composition, Path]:
    """Load the repository's poser.toml with a scsynth instrument added, whose demo source is a
    tmp folder this function creates and returns. Skips without the repo config, whose plugin
    side is machine configuration."""
    if not REPO_CONFIG.is_file():
        pytest.skip("no poser.toml at the repository root; the plugin side is machine config")
    source = tmp_path / "demo"
    source.mkdir()
    (source / "voice.scsyndef").write_bytes(VOICE_BYTES)
    (source / "voice.json").write_text("{}")
    config = tmp_path / "poser.toml"
    config.write_text(
        REPO_CONFIG.read_text(encoding="utf-8") + "\n[instruments.sc]\n"
        'adapter = "poser_scsynth.instrument:create"\n'
        f'scsynth = "{real_scsynth()}"\n'
        "\n[instruments.sc.sources]\n"
        f'demo = "{source}"\n'
    )
    from poser.compose import load

    return load(config, tmp_path / "sounds", tmp_path / "renders"), source


def two_engine_song(plugin_instrument: str) -> Song:
    return Song(
        100,
        (
            Track("synth", "sc:demo/voice", (Note(0, 69, 2, 100), Note(2, 76, 2, 90)), gain=-3.0),
            Track("strings", plugin_instrument, (Note(0, 57, 4, 90),), gain=-6.0),
        ),
    )


def render(composition: Composition, song: Song, song_id: str, *, force: bool) -> list[TrackResult]:
    from poser.cli.common import RENDER_TIMEOUT_S
    from poser.core.rendering import render_song

    return render_song(
        song,
        composition.render_store(),
        SongId(song_id),
        composition.instruments(),
        composition.audio_files(),
        effects=composition.effects(),
        timeout_s=RENDER_TIMEOUT_S,
        force=force,
    )


def pedalboard_instrument(composition: Composition) -> str:
    names = [
        name
        for name, instrument in composition.instruments().items()
        if str(instrument.adapter_id) == "pedalboard"
    ]
    if not names:
        pytest.skip("the repository poser.toml names no pedalboard instrument")
    return str(min(names))


def test_two_engine_timeline_and_cache(tmp_path: Path) -> None:
    """Both adapters answer the same song with exact stems and independent caches: re-rendering
    one engine's sound leaves the other engine's stem alone."""
    from poser.core.statuses import StemStatus

    composition, source = two_engine_composition(tmp_path)
    song = two_engine_song(pedalboard_instrument(composition))
    frames = stem_samples_of(song)

    first = render(composition, song, "songs/two-engines.py", force=True)
    by_track = {str(result.track): result for result in first}
    assert set(by_track) == {"synth", "strings"}
    for result in first:
        assert result.status in (StemStatus.RENDERED, StemStatus.SILENT), result
        assert result.cached is False
    stems = {}
    for name, result in by_track.items():
        assert result.stem is not None
        stems[name] = composition.audio_files().read(result.stem, frames)
    for audio in stems.values():
        assert isinstance(audio, Audio)
        assert np.isfinite(audio.samples).all()

    second = render(composition, song, "songs/two-engines.py", force=False)
    assert all(result.cached for result in second)

    # Touching only the scsynth sound's meta re-renders only the scsynth node.
    (source / "voice.json").write_text('{"cc": {"1": "amp"}}')
    third = render(composition, song, "songs/two-engines.py", force=False)
    by_track = {str(result.track): result for result in third}
    assert by_track["synth"].cached is False
    assert by_track["strings"].cached is True


def test_two_engine_mix_sums_the_leaves(tmp_path: Path) -> None:
    """The mix the analysis keeps equals the leaves summed at their gains, re-derived here from
    the stems — the law the shared timeline exists for."""
    from poser.core.analysis import analyze_song
    from poser.core.names import SongId
    from poser.core.statuses import StemStatus

    composition, _ = two_engine_composition(tmp_path)
    song = two_engine_song(pedalboard_instrument(composition))
    song_id = SongId("songs/two-engines.py")
    frames = stem_samples_of(song)
    results = render(composition, song, "songs/two-engines.py", force=True)
    for result in results:
        assert result.status in (StemStatus.RENDERED, StemStatus.SILENT), result

    analyze_song(
        song,
        "two-engines",
        composition.render_store(),
        song_id,
        composition.instruments(),
        composition.audio_files(),
        composition.record_writer(),
        composition.analysis_tools(),
        effects=composition.effects(),
    )
    mix = composition.audio_files().read(
        composition.render_store().song(song_id).mix_path(), frames
    )
    assert isinstance(mix, Audio)
    gains = {str(track.name): track.gain for track in song.tracks}
    by_track = {str(result.track): result for result in results}
    stems = {}
    for name, gain in gains.items():
        stem = by_track[name].stem
        assert stem is not None
        samples = composition.audio_files().read(stem, frames).samples.astype(np.float64)
        stems[name] = samples * 10 ** (gain / 20)
    expected = np.zeros_like(mix.samples, dtype=np.float64)
    for samples in stems.values():
        expected += samples
    assert np.allclose(mix.samples, expected, rtol=1e-5, atol=1e-6)


def stem_samples_of(song: Song) -> int:
    from poser.core.audio import stem_samples

    return stem_samples(song)
