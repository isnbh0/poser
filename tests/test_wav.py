"""Tests of the WAV adapter, which reads and writes stem-format audio."""

import os
import stat
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from contracts import check_audio_files_contract
from pedalboard.io import WriteableAudioFile

from poser.adapters.wav import WavAudioFiles
from poser.core.audio import SAMPLE_RATE
from poser.core.ports import AudioFileError

FRAMES = 4800


def write_wav(path: Path, audio: np.ndarray, rate: int = SAMPLE_RATE) -> Path:
    with WriteableAudioFile(  # ty: ignore[no-matching-overload]
        str(path), samplerate=rate, num_channels=audio.shape[0], bit_depth=32
    ) as output:
        output.write(audio.astype(np.float32))
    return path


def constant(value: float, frames: int = FRAMES, channels: int = 2) -> np.ndarray:
    return np.full((channels, frames), value, dtype=np.float32)


def test_wav_audio_files_meets_the_audio_files_contract(tmp_path: Path) -> None:
    check_audio_files_contract(WavAudioFiles(), tmp_path)


def test_read_returns_the_written_samples(tmp_path: Path) -> None:
    audio = constant(0.25)
    audio[1, 10] = -0.5
    path = write_wav(tmp_path / "stem.wav", audio)

    read = WavAudioFiles().read(path, FRAMES)

    assert np.array_equal(read.samples, audio)


def truncate(path: Path) -> Path:
    path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])
    return path


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (
            lambda path: write_wav(path, constant(0.1), rate=44100),
            "sample rate 44100 Hz, expected 48000",
        ),
        (lambda path: write_wav(path, constant(0.1, 2 * FRAMES, 1)), "1 channels, expected 2"),
        (lambda path: write_wav(path, constant(0.1, channels=1)), "shorter than its audio data"),
        (lambda path: truncate(write_wav(path, constant(0.1))), "shorter than its audio data"),
        (
            lambda path: write_wav(path, constant(0.1, FRAMES + 100)),
            f"{FRAMES + 100} samples, expected {FRAMES}",
        ),
        (
            lambda path: write_wav(path, constant(0.1, FRAMES - 100)),
            "shorter than its audio data",
        ),
        (
            lambda path: path.write_bytes(b"not a wav file") and path,
            "shorter than its audio data",
        ),
        (
            lambda path: path.write_bytes(b"not a wav file" * FRAMES) and path,
            "cannot read stem:",
        ),
        (lambda path: path, "cannot read stem:"),
        (
            lambda path: truncate(write_wav(path, constant(0.1), rate=44100)),
            "shorter than its audio data",
        ),
    ],
    ids=[
        "44100_hz",
        "mono",
        "mono_and_short",
        "truncated",
        "more_frames",
        "fewer_frames",
        "short_non_wav",
        "non_wav",
        "missing",
        "truncated_at_44100_hz",
    ],
)
def test_read_refuses_a_file_that_is_not_a_stem(
    tmp_path: Path, make: Callable[[Path], object], message: str
) -> None:
    path = tmp_path / "stem.wav"
    make(path)

    with pytest.raises(AudioFileError, match=message):
        WavAudioFiles().read(path, FRAMES)


def test_read_refuses_a_file_holding_a_nan(tmp_path: Path) -> None:
    audio = constant(0.1)
    audio[:, 1000:2000] = np.nan
    path = write_wav(tmp_path / "stem.wav", audio)

    with pytest.raises(AudioFileError, match="NaN or infinity"):
        WavAudioFiles().read(path, FRAMES)


def test_write_leaves_no_temporary_file_when_writing_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    audio = WavAudioFiles().read(write_wav(tmp_path / "source.wav", constant(0.1)), FRAMES)
    (tmp_path / "out").mkdir()

    with pytest.raises(OSError, match="disk full"):
        WavAudioFiles().write(tmp_path / "out" / "stem.wav", audio)
    assert list((tmp_path / "out").iterdir()) == []


def test_write_gives_the_file_the_umask_based_mode(tmp_path: Path) -> None:
    audio = WavAudioFiles().read(write_wav(tmp_path / "source.wav", constant(0.1)), FRAMES)
    umask = os.umask(0o022)
    try:
        WavAudioFiles().write(tmp_path / "stem.wav", audio)
    finally:
        os.umask(umask)
    assert stat.S_IMODE((tmp_path / "stem.wav").stat().st_mode) == 0o644
