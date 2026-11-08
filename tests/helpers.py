"""Helpers shared by several test files."""

from pathlib import Path

import numpy as np
import pytest
from pedalboard.io import ReadableAudioFile

from poser.core.audio import SAMPLE_RATE
from poser.plugin_check import PluginSpec, load_config

REPO_DIR = Path(__file__).resolve().parents[1]
SCAFFOLD_DIR = REPO_DIR / "src" / "poser" / "scaffold"


def real_specs() -> list[PluginSpec]:
    """Return the plugins listed in the repository's local plugin list."""
    config = REPO_DIR / "plugins.toml"
    if not config.is_file():
        pytest.fail(
            "real-plugin tests read plugins.toml at the repository root; "
            "copy src/poser/scaffold/plugins.toml there and edit its paths"
        )
    return load_config(config)


# Runs in a subprocess, so plugin code stays out of the test process. Serum's oscillator A octave
# at raw value 0.1 is -3 octaves, which moves middle C from about 262 Hz to about 33 Hz. The state
# is captured as `save-sound` captures it after the window closes.
CAPTURE_STATE = """
import sys
from pedalboard import load_plugin
from poser.sounds import capture_state, encode_state
plugin = load_plugin(sys.argv[1])
plugin.parameters["a_octave_oct"].raw_value = 0.1
open(sys.argv[2], "w").write(encode_state(capture_state(plugin)))
"""


def strongest_hz(stem: str) -> float:
    """Return the strongest frequency above 20 Hz between 0.2 and 1.2 seconds."""
    with ReadableAudioFile(stem) as audio_file:  # ty: ignore[no-matching-overload]
        audio = audio_file.read(audio_file.frames)
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
