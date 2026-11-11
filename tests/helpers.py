"""Helpers shared by several test files."""

from pathlib import Path

import numpy as np
import pytest
from pedalboard.io import ReadableAudioFile

from poser.adapters.pedalboard.formats import PRESET_FORMATS
from poser.adapters.pedalboard.instrument import PedalboardInstrument
from poser.cli.common import RENDER_TIMEOUT_S
from poser.compose import Composition, load
from poser.core.analysis import analyze_song
from poser.core.audio import SAMPLE_RATE
from poser.core.graph import Graph
from poser.core.names import SongId
from poser.core.rendering import TrackResult, render_song
from poser.core.report import Report
from poser.core.song import Song

REPO_DIR = Path(__file__).resolve().parents[1]
SCAFFOLD_DIR = REPO_DIR / "src" / "poser" / "scaffold"
REAL_CONFIG = REPO_DIR / "poser.toml"


def real_composition(tmp_path: Path) -> Composition:
    """Load the repository's local `poser.toml`, keeping sounds and results under `tmp_path`."""
    if not REAL_CONFIG.is_file():
        pytest.fail(
            "real-plugin tests read poser.toml at the repository root; "
            "copy src/poser/scaffold/poser.toml there and edit its paths"
        )
    return load(REAL_CONFIG, tmp_path / "sounds", tmp_path / "renders")


def real_render(tmp_path: Path, song: Song | Graph, song_id: str) -> list[TrackResult]:
    """Render a song through the real composition, as `render-song` does."""
    composition = real_composition(tmp_path)
    return render_song(
        song,
        composition.render_store(),
        SongId(song_id),
        composition.instruments(),
        composition.audio_files(),
        effects=composition.effects(),
        timeout_s=RENDER_TIMEOUT_S,
        force=False,
    )


def real_analyze(tmp_path: Path, song: Song | Graph, song_id: str) -> Report:
    """Analyze a rendered song through the real composition, as `analyze-song` does."""
    composition = real_composition(tmp_path)
    return analyze_song(
        song,
        Path(song_id).stem,
        composition.render_store(),
        SongId(song_id),
        composition.instruments(),
        composition.audio_files(),
        composition.record_writer(),
        composition.analysis_tools(),
        effects=composition.effects(),
    )


REAL_FORMATS = {fmt.suffix: fmt for fmt in PRESET_FORMATS.values()}


def real_pedalboard_instruments(tmp_path: Path) -> dict[str, PedalboardInstrument]:
    """Return each Pedalboard instrument in the repository's local `poser.toml`, by name."""
    instruments = real_composition(tmp_path).instruments()
    return {
        name: instrument
        for name, instrument in instruments.items()
        if isinstance(instrument, PedalboardInstrument)
    }


# Runs in a subprocess, so plugin code stays out of the test process. Serum's oscillator A octave
# at raw value 0.1 is -3 octaves, which moves middle C from about 262 Hz to about 33 Hz. The state
# is captured as `save-sound` captures it after the window closes.
CAPTURE_STATE = """
import sys
from pedalboard import load_plugin
from poser.adapters.pedalboard.state import encode_state
from poser.adapters.pedalboard.worker import capture_state
plugin = load_plugin(sys.argv[1])
plugin.parameters["a_octave_oct"].raw_value = 0.1
open(sys.argv[2], "w").write(encode_state(capture_state(plugin)))
"""


def strongest_hz(audio: np.ndarray) -> float:
    """Return the strongest frequency above 20 Hz between 0.2 and 1.2 seconds."""
    mono = audio.mean(axis=0)[int(0.2 * SAMPLE_RATE) : int(1.2 * SAMPLE_RATE)]
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    freqs = np.fft.rfftfreq(len(mono), 1 / SAMPLE_RATE)
    band = freqs > 20
    return float(freqs[band][np.argmax(spectrum[band])])


def fxp_bytes(chunk: bytes, name: str = "test", kind: bytes = b"FPCh") -> bytes:
    """Build an opaque-chunk `.fxp` program for Serum around `chunk`."""
    body = (
        kind
        + (1).to_bytes(4, "big")
        + b"XfsX"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
        + name.encode("ascii")[:28].ljust(28, b"\x00")
        + len(chunk).to_bytes(4, "big")
        + chunk
    )
    return b"CcnK" + len(body).to_bytes(4, "big") + body


def read_stem(stem: str) -> np.ndarray:
    with ReadableAudioFile(stem) as audio_file:  # ty: ignore[no-matching-overload]
        return audio_file.read(audio_file.frames)


def _bands_db(segment: np.ndarray, bands: int) -> np.ndarray:
    magnitude = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
    return 20 * np.log10(
        np.array([band.mean() for band in np.array_split(magnitude, bands)]) + 1e-9
    )


def band_spectrum_db(audio: np.ndarray, start_s: float, end_s: float) -> np.ndarray:
    """Return 64 equal-width spectrum bands in dB for the channel mean between two times."""
    mono = audio.mean(axis=0)[int(start_s * SAMPLE_RATE) : int(end_s * SAMPLE_RATE)]
    return _bands_db(mono, 64)


FRAME, HOP = 1024, 240


def change_time_s(reference: np.ndarray, changed: np.ndarray) -> float:
    """Return about when two renders start to differ, from frame spectra of 32 bands.

    The per-frame mean difference in dB is split in two where the summed variance of the sides is
    least, among frames starting from 0.9 to 1.7 seconds within frames from 0.6 to 2.0 seconds.
    The result is the end of the first frame after the split, the latest a change there can start.
    """
    a, b = reference.mean(axis=0), changed.mean(axis=0)
    starts = np.arange(0, len(a) - FRAME, HOP)
    diff = np.array(
        [
            np.abs(_bands_db(a[s : s + FRAME], 32) - _bands_db(b[s : s + FRAME], 32)).mean()
            for s in starts
        ]
    )
    times = starts / SAMPLE_RATE
    region = (times > 0.6) & (times < 2.0)
    diff, times = diff[region], times[region]
    candidates = [i for i in range(1, len(diff)) if 0.9 < times[i] < 1.7]
    split = min(
        candidates,
        key=lambda i: diff[:i].var() * i + diff[i:].var() * (len(diff) - i),
    )
    return float(times[split] + FRAME / SAMPLE_RATE)
