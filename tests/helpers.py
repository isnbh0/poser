"""Helpers shared by several test files."""

import json
import re
import stat
import struct
import sys
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest
from pedalboard.io import ReadableAudioFile

from poser.adapters.pedalboard import worker as pedalboard_worker
from poser.adapters.pedalboard.formats import PRESET_FORMATS
from poser.adapters.pedalboard.instrument import PedalboardInstrument
from poser.adapters.pedalboard.state import decode_state
from poser.cli.common import RENDER_TIMEOUT_S
from poser.compose import Composition, load
from poser.core.analysis import analyze_song
from poser.core.audio import SAMPLE_RATE, SILENCE_DBFS, measure_peak_dbfs
from poser.core.graph import Graph
from poser.core.jsonvalue import JsonValue
from poser.core.loading import LoadedSong
from poser.core.names import SongId
from poser.core.rendering import TrackResult, render_song
from poser.core.report import Report
from poser.core.song import Song

if TYPE_CHECKING:
    from pedalboard import AudioUnitPlugin, VST3Plugin

REPO_DIR = Path(__file__).resolve().parents[1]
SCAFFOLD_DIR = Path(str(files("poser").joinpath("scaffold")))
SURGE_DIR = Path(str(files("poser.reference_setups.surge")))
REAL_CONFIG = REPO_DIR / "poser.toml"
# `chmod(0)` leaves a folder readable on Windows, where access control lists decide.
needs_posix_permissions = pytest.mark.skipif(
    sys.platform == "win32", reason="chmod cannot make a folder unreadable on Windows"
)


def assert_mode(path: Path, mode: int) -> None:
    """Assert that `path` has the permission bits `mode`. On Windows the only mode is the read-only
    attribute, so there it asserts the mode Windows reports for `mode`: `0o444` when the owner
    cannot write, `0o666` otherwise."""
    expected = mode
    if sys.platform == "win32":
        expected = 0o666 if mode & stat.S_IWUSR else 0o444
    assert stat.S_IMODE(path.stat().st_mode) == expected


def set_instrument_guide(config: Path, instrument: str, note: str) -> None:
    """Replace the one-line `guide` of `[instruments.<instrument>]` in `config` with `note`."""
    text = config.read_text(encoding="utf-8")
    header = f"[instruments.{instrument}]\n"
    start = text.index(header) + len(header)
    end = text.find("\n[", start)
    end = len(text) if end == -1 else end
    line = f"guide = {json.dumps(note)}"
    table, count = re.subn(r"^guide = .*$", lambda _: line, text[start:end], flags=re.MULTILINE)
    assert count == 1, f"no single-line guide in [instruments.{instrument}]"
    config.write_text(text[:start] + table + text[end:], encoding="utf-8", newline="\n")
    assert (
        tomllib.loads(config.read_text(encoding="utf-8"))["instruments"][instrument]["guide"]
        == note
    )


def real_composition(tmp_path: Path) -> Composition:
    """Load the repository's local `poser.toml`, keeping sounds and results under `tmp_path`."""
    if not REAL_CONFIG.is_file():
        pytest.fail(
            "real-plugin tests read poser.toml at the repository root; "
            "copy src/poser/reference_setups/surge/poser.toml there and edit its paths"
        )
    return load(REAL_CONFIG, tmp_path / "sounds", tmp_path / "renders")


def loaded_song(song: LoadedSong | Song | Graph) -> LoadedSong:
    """Return the song as `load_song` would, so tests can pass a song built in code."""
    return song if isinstance(song, LoadedSong) else LoadedSong(song)


def real_render(tmp_path: Path, song: LoadedSong | Song | Graph, song_id: str) -> list[TrackResult]:
    """Render a song through the real composition, as `render-song` does."""
    composition = real_composition(tmp_path)
    return render_song(
        loaded_song(song),
        composition.render_store(),
        SongId(song_id),
        composition.instruments(),
        composition.audio_files(),
        effects=composition.effects(),
        timeout_s=RENDER_TIMEOUT_S,
        force=False,
    )


def real_analyze(tmp_path: Path, song: LoadedSong | Song | Graph, song_id: str) -> Report:
    """Analyze a rendered song through the real composition, as `analyze-song` does."""
    composition = real_composition(tmp_path)
    return analyze_song(
        loaded_song(song),
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


@dataclass(frozen=True)
class HarmonicMetrics:
    """Health-checked steady-window measurements, with channel powers summed, not waveforms."""

    rms_dbfs: float
    fundamental_hz: float
    fundamental_fraction: float
    h2_db: float


def harmonic_metrics(audio: np.ndarray) -> HarmonicMetrics:
    """Measure 0.2-1.2 seconds of held MIDI 69, refusing unhealthy audio before class checks.

    Hann-window each channel separately; integrate power within ±5 Hz of the strongest >20 Hz
    peak and twice that peak. Floor the dimensionless P2/P1 ratio at 1e-15 (-150 dB), so a
    numerically absent second harmonic remains finite without changing the separated classes.
    """
    start, end = int(0.2 * SAMPLE_RATE), int(1.2 * SAMPLE_RATE)
    assert audio.ndim == 2
    assert audio.shape[0] == 2
    assert audio.shape[1] >= end
    assert np.isfinite(audio).all(), "harmonic audio contains non-finite samples"
    assert measure_peak_dbfs(audio) > SILENCE_DBFS, "harmonic audio is inaudible"
    window = audio[:, start:end].astype(np.float64)
    rms_dbfs = 20 * float(np.log10(max(float(np.sqrt(np.mean(window**2))), 1e-15)))
    assert rms_dbfs > -60, f"harmonic window RMS {rms_dbfs:.2f} dBFS is too quiet"
    power = np.sum(np.abs(np.fft.rfft(window * np.hanning(end - start), axis=1)) ** 2, axis=0)
    frequencies = np.fft.rfftfreq(end - start, 1 / SAMPLE_RATE)
    non_dc = frequencies > 20
    fundamental = float(frequencies[non_dc][np.argmax(power[non_dc])])
    assert abs(fundamental - 440) <= 5, f"harmonic fundamental {fundamental:.2f} Hz is not A4"
    p1 = float(power[np.abs(frequencies - fundamental) <= 5].sum())
    p2 = float(power[np.abs(frequencies - 2 * fundamental) <= 5].sum())
    fraction = p1 / float(power[non_dc].sum())
    assert fraction > 0.1, f"harmonic fundamental power fraction {fraction:.4f} is too small"
    h2_db = 10 * float(np.log10(max(p2 / p1, 1e-15)))
    return HarmonicMetrics(rms_dbfs, fundamental, fraction, h2_db)


def assert_sine_saw_pair(sine: HarmonicMetrics, saw: HarmonicMetrics) -> None:
    """Require the selected presets' distinct harmonic classes, not waveform inequality."""
    assert sine.h2_db < -30, f"harmonic class: expected sine H2 < -30 dB, got {sine.h2_db:.2f}"
    assert saw.h2_db > -15, f"harmonic class: expected saw H2 > -15 dB, got {saw.h2_db:.2f}"


def render_without_restoring_state(job: JsonValue) -> JsonValue:
    """Child-only counterfactual: omit raw_state assignment, retaining the original render job.

    Import this target via inherited PYTHONPATH. Keeping nonempty state in render_job preserves
    its ordinary post-state no-MIDI preparation even though load ignores restoration.
    """
    assert isinstance(job, dict)
    encoded = job.get("state")
    assert isinstance(encoded, str), "control requires nonempty job state"
    assert encoded, "control requires nonempty job state"
    expected = decode_state(encoded)
    assert expected, "control requires nonempty decoded state"
    original_load = pedalboard_worker.load
    calls = 0

    def ignore_state(
        path: str, plugin_name: str | None, state: bytes | None
    ) -> "VST3Plugin | AudioUnitPlugin":
        nonlocal calls
        calls += 1
        assert state == expected, "control load did not receive the original nonempty state"
        return original_load(path, plugin_name, None)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pedalboard_worker, "load", ignore_state)
        result = pedalboard_worker.render_job(job)
    assert calls == 1, f"control load wrapper ran {calls} times, expected exactly once"
    return result


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


Version = tuple[int, int, int, int]


def write_pe(
    path: Path,
    *,
    file_version: Version = (1, 3, 4, 0),
    product_version: Version = (0, 0, 0, 0),
    pe32: bool = False,
    signature: int = 0xFEEF04BD,
    resource_type: int = 16,
    resource_directory: bool = True,
) -> Path:
    """Write a minimal PE image whose version resource holds the given fixed versions, and return
    its path.

    The image has one section, `.rsrc`, at RVA 0x1000 and file offset 0x200. Its resource tree
    leads from `resource_type` through ID 1 and language 0x409 to a VS_VERSIONINFO, whose
    VS_FIXEDFILEINFO starts with `signature`. Without `resource_directory`, the optional header
    names no resource table.
    """

    def dwords(version: Version) -> tuple[int, int]:
        a, b, c, d = version
        return a << 16 | b, c << 16 | d

    fixed = struct.pack(
        "<13I", signature, 0x10000, *dwords(file_version), *dwords(product_version), *[0] * 7
    )
    key = "VS_VERSION_INFO\0".encode("utf-16-le")
    info = struct.pack("<HHH", 6 + len(key) + 2 + len(fixed), len(fixed), 0) + key + b"\0\0" + fixed

    rva, subdirectory = 0x1000, 0x80000000

    def table(entry_id: int, offset: int) -> bytes:
        return struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", entry_id, offset)

    resources = (
        table(resource_type, subdirectory | 0x18)
        + table(1, subdirectory | 0x30)
        + table(0x409, 0x48)
        + struct.pack("<IIII", rva + 0x58, len(info), 0, 0)
        + info
    )

    count_offset, directories_offset = (92, 96) if pe32 else (108, 112)
    optional = bytearray(directories_offset + 16 * 8)
    struct.pack_into("<H", optional, 0, 0x10B if pe32 else 0x20B)
    struct.pack_into("<I", optional, count_offset, 16)
    if resource_directory:
        struct.pack_into("<II", optional, directories_offset + 2 * 8, rva, len(resources))
    machine = 0x14C if pe32 else 0x8664
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", machine, 1, 0, 0, 0, len(optional), 0x2022)
    section = b".rsrc\0\0\0" + struct.pack(
        "<IIIIIIHHI", len(resources), rva, len(resources), 0x200, 0, 0, 0, 0, 0x40000040
    )
    dos = bytearray(0x40)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, len(dos))
    headers = bytes(dos) + coff + bytes(optional) + section
    assert len(headers) <= 0x200
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(headers.ljust(0x200, b"\0") + resources)
    return path
