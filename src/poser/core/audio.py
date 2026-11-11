"""Audio: the stem format, the song's timeline in samples, and stem-format audio values."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast, final

import numpy as np
from numpy.lib.format import read_array_header_1_0, read_array_header_2_0, read_magic

from poser.core.graph import Graph
from poser.core.rules import _last_beat
from poser.core.song import Song

SAMPLE_RATE = 48000
NUM_CHANNELS = 2
SILENCE_DBFS = -60.0
FLOOR_DBFS = -120.0  # Reported for digital silence, because JSON has no -inf.
TAIL_S = 2.0
STEM_BIT_DEPTH = 32  # Float WAV, so levels above full scale survive until mixing.


def to_dbfs(peak: float) -> float:
    floor = 10.0 ** (FLOOR_DBFS / 20.0)
    return FLOOR_DBFS if peak <= floor else 20.0 * float(np.log10(peak))


def measure_peak_dbfs(samples: np.ndarray) -> float:
    return to_dbfs(float(np.abs(samples[np.isfinite(samples)]).max(initial=0.0)))


def song_duration_s(song: Song | Graph) -> float:
    """Return the length shared by every stem: the song's last beat plus the tail. Pedalboard
    ignores messages after the end, so changes must fall within.
    """
    members = song.tracks if isinstance(song, Song) else song.nodes
    return song.seconds(_last_beat(members)) + TAIL_S


def graph_duration_s(graph: Graph) -> float:
    """Return the length shared by every stem of a graph: its last beat plus the tail."""
    return song_duration_s(graph)


def expected_samples(duration_s: float) -> int:
    """Return the sample count Pedalboard renders, which truncates rather than rounds."""
    return int(duration_s * SAMPLE_RATE)


def stem_samples(song: Song | Graph) -> int:
    """Return the frame count of every stem of the song."""
    return expected_samples(song_duration_s(song))


def sample_at(song: Song | Graph, beat: float) -> int:
    """Return the sample a beat falls on."""
    return round(song.seconds(beat) * SAMPLE_RATE)


class AudioFault(StrEnum):
    MISSING = "missing"
    UNREADABLE = "unreadable"
    SHAPE = "shape"
    DTYPE = "dtype"
    LENGTH = "length"
    NON_FINITE = "non_finite"


@dataclass(frozen=True)
class AudioProblem:
    """Why some data is not stem-format audio."""

    fault: AudioFault
    message: str


def audio_problem(data: object, frames: int | None = None) -> AudioProblem | None:
    """Return the first rule of stem-format audio the data breaks, or None when it is audio."""
    if not isinstance(data, np.ndarray):
        return AudioProblem(
            AudioFault.SHAPE, f"audio must be a numpy array, got {type(data).__name__}"
        )
    if data.ndim != 2 or data.shape[0] != NUM_CHANNELS or data.shape[1] < 1:
        return AudioProblem(
            AudioFault.SHAPE, f"audio shape {data.shape}, expected ({NUM_CHANNELS}, frames)"
        )
    if data.dtype != np.float32:
        return AudioProblem(AudioFault.DTYPE, f"audio dtype {data.dtype}, expected float32")
    if frames is not None and data.shape[1] != frames:
        return AudioProblem(AudioFault.LENGTH, f"{data.shape[1]} samples, expected {frames}")
    if not np.isfinite(data).all():
        return AudioProblem(AudioFault.NON_FINITE, "audio contains NaN or infinity")
    return None


@final
class Audio:
    """Stem-format audio. Immutable: it owns a read-only copy of the samples."""

    __slots__ = ("_data",)

    def __init__(self, data: np.ndarray) -> None:
        problem = audio_problem(data)
        if problem is not None:
            raise ValueError(problem.message)
        owned = np.array(data, dtype=np.float32, order="C", copy=True)
        owned.flags.writeable = False
        self._data = owned

    @property
    def samples(self) -> np.ndarray:
        """A read-only view of the samples, shaped `(NUM_CHANNELS, frames)`."""
        return self._data.view()

    @property
    def frames(self) -> int:
        return int(self._data.shape[1])


def make_audio(data: object, frames: int) -> Audio | AudioProblem:
    """Return the data as audio of `frames` frames, or the first rule it breaks."""
    problem = audio_problem(data, frames)
    if problem is not None:
        return problem
    return Audio(cast("np.ndarray", data))  # audio_problem checked the type.


def write_raw_audio(path: Path, audio: np.ndarray) -> None:
    """Write an array as raw audio, the file an instrument writes for the core."""
    with path.open("wb") as output:
        np.save(output, np.ascontiguousarray(audio), allow_pickle=False)


_RAW_DTYPE = np.dtype("<f4")


def read_raw_audio(path: Path, frames: int) -> Audio | AudioProblem:
    """Return the raw audio at `path` as audio of `frames` frames, or why it is not.

    The header is checked before any data is read, and no more data is read than it allows.
    """
    count = NUM_CHANNELS * frames
    try:
        with path.open("rb") as raw:
            version = read_magic(raw)
            if version == (1, 0):
                shape, fortran_order, dtype = read_array_header_1_0(raw)
            elif version == (2, 0):
                shape, fortran_order, dtype = read_array_header_2_0(raw)
            else:
                return AudioProblem(
                    AudioFault.UNREADABLE, f"cannot read audio: unsupported version {version}"
                )
            if len(shape) != 2 or shape[0] != NUM_CHANNELS or shape[1] < 1:
                return AudioProblem(
                    AudioFault.SHAPE, f"audio shape {shape}, expected ({NUM_CHANNELS}, frames)"
                )
            if dtype != _RAW_DTYPE:
                return AudioProblem(
                    AudioFault.DTYPE, f"audio dtype {dtype}, expected little-endian float32"
                )
            if shape[1] != frames:
                return AudioProblem(AudioFault.LENGTH, f"{shape[1]} samples, expected {frames}")
            if fortran_order:
                return AudioProblem(
                    AudioFault.UNREADABLE, "cannot read audio: Fortran-ordered array"
                )
            array = np.fromfile(raw, dtype=_RAW_DTYPE, count=count)
    except FileNotFoundError:
        return AudioProblem(AudioFault.MISSING, "no audio file")
    except (OSError, ValueError, EOFError) as error:
        return AudioProblem(AudioFault.UNREADABLE, f"cannot read audio: {error}")
    if array.size < count:
        return AudioProblem(
            AudioFault.UNREADABLE, f"cannot read audio: file holds {array.size} of {count} values"
        )
    return make_audio(array.reshape(NUM_CHANNELS, frames), frames)
