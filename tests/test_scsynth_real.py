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
from poser.core.audio import SAMPLE_RATE, Audio, read_raw_audio
from poser.core.names import Identifier
from poser.core.performance import Control, NoteOff, NoteOn, Performance
from poser.core.ports import AdapterContext, RenderRequest, ResolvedSound
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
