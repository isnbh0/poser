"""Ports: the protocols through which the core reaches song files and audio files."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from poser.core.audio import Audio
from poser.core.names import Suffix
from poser.core.song import Song


@runtime_checkable
class SongFormat(Protocol):
    """Reads one kind of song file into a Song."""

    @property
    def suffixes(self) -> tuple[Suffix, ...]:
        """The file suffixes this format reads, such as `(".yaml", ".yml")`."""
        ...

    def load(self, path: Path) -> Song:
        """Read the song at `path`, raising SongError for any failure."""
        ...


@runtime_checkable
class RecordWriter(Protocol):
    """Writes a song as text for an analysis record."""

    @property
    def suffix(self) -> Suffix:
        """The record's song file is `song<suffix>`."""
        ...

    def dump(self, song: Song) -> str:
        """Return the song as text, raising SongError if it cannot be written."""
        ...


class AudioFileError(Exception):
    """A file cannot be read as stem-format audio."""


@runtime_checkable
class AudioFiles(Protocol):
    """Reads and writes audio in the stem format: SAMPLE_RATE, NUM_CHANNELS, 32-bit float."""

    @property
    def suffix(self) -> Suffix:
        """Stems are named `<track><suffix>`."""
        ...

    def read(self, path: Path, frames: int) -> Audio:
        """Read stem-format audio of `frames` frames.

        Raises AudioFileError when the file is missing, unreadable, at another sample rate or
        channel count, holds a frame count other than `frames`, or holds non-finite samples.
        """
        ...

    def write(self, path: Path, audio: Audio) -> None:
        """Write audio in the stem format.

        Writes through a temporary file with a unique name (tempfile.mkstemp) in the same
        directory, then replaces `path`, so a reader never sees a partial file and two writers
        never share a temporary file. Removes the temporary file and raises OSError on failure.
        """
        ...
