"""Every song in `songs/` loads, whether YAML or a script."""

from pathlib import Path

import pytest
from helpers import REPO_DIR

from poser.song import SCRIPT_SUFFIX, YAML_SUFFIXES, load_song

SONGS = sorted(
    path
    for path in (REPO_DIR / "songs").iterdir()
    if path.suffix in (*YAML_SUFFIXES, SCRIPT_SUFFIX)
)


def test_songs_directory_has_songs() -> None:
    assert SONGS


@pytest.mark.parametrize("path", SONGS, ids=[path.name for path in SONGS])
def test_song_loads(path: Path) -> None:
    assert load_song(path).tracks
