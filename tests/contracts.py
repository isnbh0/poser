"""Contract checks that every adapter of a port must pass."""

from pathlib import Path

import numpy as np
import pytest

from poser.core.audio import NUM_CHANNELS, Audio
from poser.core.ports import AudioFileError, AudioFiles, RecordWriter, SongFormat
from poser.core.song import Song, SongError


def check_song_format_contract(fmt: object, sample_song: Song, tmp_path: Path) -> None:
    """Check a SongFormat: its suffixes, its failures, and, if it writes songs, a round trip."""
    assert isinstance(fmt, SongFormat)
    assert fmt.suffixes
    for suffix in fmt.suffixes:
        assert suffix == suffix.lower()
    unreadable = tmp_path / f"unreadable{fmt.suffixes[0]}"
    unreadable.write_text("this is not a song: [\n")
    with pytest.raises(SongError):
        fmt.load(unreadable)
    with pytest.raises(SongError):
        fmt.load(tmp_path / f"missing{fmt.suffixes[0]}")
    if isinstance(fmt, RecordWriter):
        written = tmp_path / f"written{fmt.suffix}"
        written.write_text(fmt.dump(sample_song))
        assert fmt.load(written) == sample_song


def check_audio_files_contract(files: object, tmp_path: Path) -> None:
    """Check an AudioFiles adapter: round trips, temporary files, and read failures."""
    assert isinstance(files, AudioFiles)
    frames = 1000
    ramp = np.linspace(-1.5, 1.5, NUM_CHANNELS * frames, dtype=np.float32)
    audio = Audio(ramp.reshape(NUM_CHANNELS, frames))

    directory = tmp_path / "stems"
    directory.mkdir()
    path = directory / f"track{files.suffix}"
    files.write(path, audio)
    assert sorted(directory.iterdir()) == [path]
    read = files.read(path, frames)
    assert np.array_equal(read.samples, audio.samples)
    assert float(np.max(np.abs(read.samples))) > 1.0

    files.write(path, audio)  # Replacing an existing file also leaves nothing else behind.
    assert sorted(directory.iterdir()) == [path]

    absent = tmp_path / "absent"
    with pytest.raises(OSError):  # noqa: PT011 -- any OSError is the contract.
        files.write(absent / f"track{files.suffix}", audio)
    assert not absent.exists()

    with pytest.raises(AudioFileError):
        files.read(directory / f"missing{files.suffix}", frames)
    with pytest.raises(AudioFileError):
        files.read(path, frames + 1)
