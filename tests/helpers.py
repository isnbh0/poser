"""Helpers shared by several test files."""

import numpy as np
from pedalboard.io import ReadableAudioFile

from vsti_composer.plugin_check import SAMPLE_RATE

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
