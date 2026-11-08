"""The standard measurer: loudness per ITU-R BS.1770, and mean power in each band.

It relies on the port's precondition that audio is at least `TAIL_S` long.
"""

import math

import numpy as np
import pyloudnorm
from scipy import signal

from poser.core.audio import SAMPLE_RATE, Audio
from poser.core.report import BANDS, Levels

POWER_FLOOR = 1e-12  # -120 dB, which keeps the logarithm finite for silence.
WELCH_NPERSEG = 8192


def integrated_lufs(audio: np.ndarray) -> float | None:
    """Return loudness per ITU-R BS.1770, or None for silence, because JSON has no infinity."""
    # Pyloudnorm takes samples-first arrays, and stems are channels-first.
    loudness = pyloudnorm.Meter(SAMPLE_RATE).integrated_loudness(audio.T.astype(np.float64))
    return float(loudness) if math.isfinite(loudness) else None


def bands_db(audio: np.ndarray) -> dict[str, float]:
    """Return each band's mean power spectral density in dB, for comparing bands and versions.

    The values are relative, not calibrated to a loudness scale.
    """
    mono = audio.mean(axis=0, dtype=np.float64)
    freqs, density = signal.welch(mono, fs=SAMPLE_RATE, nperseg=min(WELCH_NPERSEG, mono.size))
    levels = {}
    for name, (low, high) in BANDS.items():
        inside = density[(freqs >= low) & (freqs < high)]
        power = float(inside.mean()) if inside.size else 0.0
        levels[name] = round(10.0 * math.log10(max(power, POWER_FLOOR)), 2)
    return levels


class StandardMeasurer:
    """Measures integrated loudness with pyloudnorm and band levels with Welch's method."""

    def measure(self, audio: Audio) -> Levels:
        samples = audio.samples
        return Levels(integrated_lufs(samples), bands_db(samples))
