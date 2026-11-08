"""The spectrogram visualizer: a PNG of each track's and the mix's spectrum over time.

It relies on the port's precondition that audio is at least `TAIL_S` long.
"""

from collections.abc import Mapping
from pathlib import Path

import numpy as np
from matplotlib import mlab
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from poser.adapters.loading import refuse_options
from poser.core.audio import SAMPLE_RATE, Audio
from poser.core.jsonvalue import JsonValue
from poser.core.names import Suffix
from poser.core.ports import AdapterContext

SPECGRAM_NFFT = 2048
SPECGRAM_NOVERLAP = 1536
POWER_FLOOR = 1e-12  # -120 dB, which keeps the spectrogram's logarithm finite for silence.
SPECTROGRAM_MIN_HZ, SPECTROGRAM_MAX_HZ = 20.0, 20000.0

GUIDE = """\
## Spectrograms

Each analyzed track and the mix get a spectrogram, a PNG image that you can open and view. Time
in seconds runs left to right, frequency runs up on a logarithmic axis from 20 Hz to 20 kHz, and
color shows power in dB, brighter for louder. Look for energy in the wrong band, notes that ring
past their end, and parts that mask each other.
"""


def spectrogram_figure(audio: np.ndarray, title: str) -> Figure:
    """Draw a spectrogram on a logarithmic frequency axis from 20 Hz to 20 kHz, with a dB scale."""
    figure = Figure(figsize=(10, 4))
    FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    # This follows `Axes.specgram`, which takes the logarithm of the power directly, so silent
    # bins would give -inf and a warning. The floor goes on the power for that reason.
    power, freqs, times = (
        np.asarray(result, dtype=np.float64)
        for result in mlab.specgram(
            audio.sum(axis=0, dtype=np.float64),
            NFFT=SPECGRAM_NFFT,
            Fs=SAMPLE_RATE,
            noverlap=SPECGRAM_NOVERLAP,
        )
    )
    # A logarithmic axis cannot show the 0 Hz bin, and nothing below 20 Hz is audible.
    keep = freqs >= SPECTROGRAM_MIN_HZ
    mesh = axes.pcolormesh(
        times, freqs[keep], 10.0 * np.log10(power[keep] + POWER_FLOOR), shading="nearest"
    )
    axes.set_yscale("log")
    axes.set_ylim(SPECTROGRAM_MIN_HZ, SPECTROGRAM_MAX_HZ)
    figure.colorbar(mesh, ax=axes, label="dB")
    axes.set_xlabel("Time (s)")
    axes.set_ylabel("Frequency (Hz)")
    axes.set_title(title)
    return figure


def write_spectrogram(audio: np.ndarray, path: Path, title: str) -> None:
    spectrogram_figure(audio, title).savefig(path, format="png")


class SpectrogramVisualizer:
    """Draws spectrograms as PNG images."""

    @property
    def suffix(self) -> Suffix:
        return Suffix(".png")

    @property
    def guide(self) -> str:
        return GUIDE

    def draw(self, audio: Audio, path: Path, title: str) -> None:
        write_spectrogram(audio.samples, path, title)


def create(options: Mapping[str, JsonValue], _context: AdapterContext) -> SpectrogramVisualizer:
    """The factory for `spectrogram`. Takes no options."""
    refuse_options(options)
    return SpectrogramVisualizer()
