"""Every one of the scaffold's songs loads, whether YAML or a script."""

from pathlib import Path

import pytest
from helpers import SCAFFOLD_DIR

from poser.adapters.song_formats import SONG_FORMATS
from poser.core.loading import load_song
from poser.core.song import Song

SUFFIXES = {suffix for fmt in SONG_FORMATS for suffix in fmt.suffixes}
SONGS = sorted(path for path in SCAFFOLD_DIR.iterdir() if path.suffix in SUFFIXES)


def test_scaffold_has_songs() -> None:
    assert SONGS


@pytest.mark.parametrize("path", SONGS, ids=[path.name for path in SONGS])
def test_song_loads(path: Path) -> None:
    song = load_song(path, SONG_FORMATS)
    assert isinstance(song, Song)
    assert song.tracks
