"""Tests of the core audio module: the song's length in samples, peaks, and raw audio files."""

import io
import zipfile
from pathlib import Path

import numpy as np
import pytest
from numpy.lib.format import write_array_header_1_0

from poser.core.audio import (
    FLOOR_DBFS,
    NUM_CHANNELS,
    SAMPLE_RATE,
    TAIL_S,
    Audio,
    AudioFault,
    AudioProblem,
    measure_peak_dbfs,
    read_raw_audio,
    stem_samples,
    write_raw_audio,
)
from poser.core.song import ControlChange, Note, Song, Track, song_last_beat

FRAMES = 1000


@pytest.fixture(autouse=True)
def _forbid_np_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """`read_raw_audio` must never call `np.load`, which trusts the file's header."""

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("np.load must not be called")

    monkeypatch.setattr(np, "load", fail)


def song_of(bpm: float, notes: tuple[Note, ...], cc: tuple[ControlChange, ...] = ()) -> Song:
    return Song(bpm=bpm, tracks=(Track(name="a", sound="Serum", notes=notes, cc=cc),))


@pytest.mark.parametrize(
    "song",
    [
        song_of(120, (Note(0, 60, 1, 100),)),
        song_of(100, (Note(0, 60, 1, 100), Note(7.5, 62, 0.25, 90))),
        song_of(
            87.3,
            (Note(1, 60, 2, 100),),
            (ControlChange(0, 1, 0, "linear"), ControlChange(9, 1, 127)),
        ),
    ],
)
def test_stem_samples_cover_the_last_beat_and_the_tail(song: Song) -> None:
    expected = int((song.seconds(song_last_beat(song.graph())) + TAIL_S) * SAMPLE_RATE)
    assert stem_samples(song.graph()) == expected


def test_peak_of_half_scale_is_minus_six_dbfs() -> None:
    samples = np.full((NUM_CHANNELS, 100), -0.5, dtype=np.float32)
    assert measure_peak_dbfs(samples) == pytest.approx(-6.02, abs=0.01)


def test_peak_of_silence_is_the_floor() -> None:
    assert measure_peak_dbfs(np.zeros((NUM_CHANNELS, 100), dtype=np.float32)) == FLOOR_DBFS


def test_peak_ignores_non_finite_samples() -> None:
    samples = np.full((NUM_CHANNELS, 100), 0.5, dtype=np.float32)
    samples[0, 0] = np.nan
    samples[1, 1] = np.inf
    assert measure_peak_dbfs(samples) == pytest.approx(-6.02, abs=0.01)


def ramp(frames: int = FRAMES) -> np.ndarray:
    values = np.linspace(-1.5, 1.5, NUM_CHANNELS * frames, dtype=np.float32)
    return values.reshape(NUM_CHANNELS, frames)


def problem(path: Path, frames: int = FRAMES) -> AudioProblem:
    result = read_raw_audio(path, frames)
    assert isinstance(result, AudioProblem), result
    return result


def test_raw_audio_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "audio.npy"
    write_raw_audio(path, ramp())
    audio = read_raw_audio(path, FRAMES)
    assert isinstance(audio, Audio)
    assert np.array_equal(audio.samples, ramp())


def test_missing_raw_audio_is_missing(tmp_path: Path) -> None:
    assert problem(tmp_path / "absent.npy").fault is AudioFault.MISSING


def test_malformed_raw_audio_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "audio.npy"
    path.write_bytes(b"not an array at all")
    assert problem(path).fault is AudioFault.UNREADABLE


def test_npz_archive_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "audio.npy"
    buffer = io.BytesIO()
    np.savez(buffer, audio=ramp())
    path.write_bytes(buffer.getvalue())
    assert zipfile.is_zipfile(path)
    assert problem(path).fault is AudioFault.UNREADABLE


def test_pickled_object_array_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "audio.npy"
    objects = np.empty((NUM_CHANNELS, FRAMES), dtype=object)
    objects[...] = 0.0
    np.save(path, objects, allow_pickle=True)
    assert problem(path).fault is AudioFault.DTYPE


def test_truncated_header_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "audio.npy"
    write_raw_audio(path, ramp())
    path.write_bytes(path.read_bytes()[:20])
    assert problem(path).fault is AudioFault.UNREADABLE


def test_huge_header_is_refused_before_reading_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audio.npy"
    header = {"descr": "<f4", "fortran_order": False, "shape": (NUM_CHANNELS, 10**12)}
    with path.open("wb") as output:
        write_array_header_1_0(output, header)  # Writes the magic too.

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("no data may be read")

    monkeypatch.setattr(np, "fromfile", fail)
    result = problem(path)
    assert result.fault is AudioFault.LENGTH
    assert result.message == f"{10**12} samples, expected {FRAMES}"


@pytest.mark.parametrize(
    ("array", "fault"),
    [
        (np.zeros((1, FRAMES), dtype=np.float32), AudioFault.SHAPE),
        (np.zeros(NUM_CHANNELS * FRAMES, dtype=np.float32), AudioFault.SHAPE),
        (np.zeros((NUM_CHANNELS, FRAMES + 1), dtype=np.float32), AudioFault.LENGTH),
        (np.zeros((NUM_CHANNELS, FRAMES), dtype=np.float64), AudioFault.DTYPE),
        (np.zeros((NUM_CHANNELS, FRAMES), dtype=">f4"), AudioFault.DTYPE),
    ],
)
def test_wrong_raw_audio_is_refused(tmp_path: Path, array: np.ndarray, fault: AudioFault) -> None:
    path = tmp_path / "audio.npy"
    np.save(path, array, allow_pickle=False)
    assert problem(path).fault is fault


def test_fortran_ordered_raw_audio_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "audio.npy"
    np.save(path, np.asfortranarray(ramp()), allow_pickle=False)
    result = problem(path)
    assert result.fault is AudioFault.UNREADABLE
    assert "Fortran" in result.message


def test_short_payload_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "audio.npy"
    write_raw_audio(path, ramp())
    path.write_bytes(path.read_bytes()[:-40])  # Ten float32 values short.
    count = NUM_CHANNELS * FRAMES
    result = problem(path)
    assert result.fault is AudioFault.UNREADABLE
    assert result.message == f"cannot read audio: file holds {count - 10} of {count} values"


def test_nan_in_raw_audio_is_non_finite(tmp_path: Path) -> None:
    path = tmp_path / "audio.npy"
    data = ramp()
    data[1, 5] = np.nan
    write_raw_audio(path, data)
    assert problem(path).fault is AudioFault.NON_FINITE
