"""Loading: reading a song file through the song format that claims its suffix."""

from collections.abc import Sequence
from pathlib import Path

from poser.core.ports import SongFormat
from poser.core.song import Song, SongError


def _listed(suffixes: Sequence[str]) -> str:
    """Return suffixes as an English list, such as `.yaml, .yml, or .py`."""
    if len(suffixes) <= 2:
        return " or ".join(suffixes)
    return f"{', '.join(suffixes[:-1])}, or {suffixes[-1]}"


def load_song(path: Path, formats: Sequence[SongFormat]) -> Song:
    """Load the song at `path` with the format whose suffixes contain its suffix exactly.

    Raises ValueError when two formats claim one suffix, and SongError when no format claims the
    path's suffix or the format cannot read the song.
    """
    claimed: dict[str, SongFormat] = {}
    for song_format in formats:
        for suffix in song_format.suffixes:
            if suffix in claimed:
                raise ValueError(f"more than one song format claims the suffix {suffix}")
            claimed[suffix] = song_format
    song_format = claimed.get(path.suffix)
    if song_format is None:
        raise SongError([f"cannot read {path}: a song is a {_listed(list(claimed))} file"])
    return song_format.load(path)
