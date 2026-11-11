"""One uninterrupted song-store lock lifetime, shared by use cases."""

from collections.abc import Generator
from contextlib import contextmanager

from poser.core.names import SongId
from poser.core.ports import RenderStore, SongStore


@contextmanager
def song_session(renders: RenderStore, song_id: SongId) -> Generator[SongStore]:
    """Hold the song lock until all operations, including publication, have finished."""
    store = renders.song(song_id)
    with store.lock():
        yield store
