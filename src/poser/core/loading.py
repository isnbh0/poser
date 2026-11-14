"""Loading: reading a song file through the song format that claims its suffix, into the one
graph every later step reads."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from poser.core.graph import Graph
from poser.core.ports import SongFormat
from poser.core.song import Song, SongError


def check_effect_recipes(source: Song | Graph, effects: Sequence[str]) -> None:
    """Raise SongError naming every effect recipe the source uses that `effects` does not list.

    A problem points into the file the author wrote: `tracks[i]` for a Song, whose chains name
    recipes per track, and `nodes[i]` for a Graph, whose author names every node.
    """
    known = set(effects)
    if isinstance(source, Song):
        problems = [
            f"tracks[{index}]: no effect recipe {recipe!r} in poser.toml"
            for index, track in enumerate(source.tracks)
            for recipe in track.effects
            if recipe not in known
        ]
    else:
        problems = [
            f"nodes[{index}]: no effect recipe {node.effect!r} in poser.toml"
            for index, node in enumerate(source.nodes)
            if node.kind == "effect" and node.effect not in known
        ]
    if problems:
        raise SongError(problems)


@dataclass(frozen=True)
class LoadedSong:
    """A loaded song: the source its file holds, and the graph derived from it.

    Every step after loading reads `graph`. Only the analysis record reads `source`, so a song is
    recorded in the form its author wrote. Raises SongError when a Song's effect instances
    collide with its authored names.
    """

    source: Song | Graph
    graph: Graph = field(init=False)

    def __post_init__(self) -> None:
        graph = self.source if isinstance(self.source, Graph) else self.source.graph()
        object.__setattr__(self, "graph", graph)

    def check_effects(self, effects: Sequence[str]) -> None:
        """Raise SongError naming every effect recipe the song uses that `effects` does not list."""
        check_effect_recipes(self.source, effects)


def _listed(suffixes: Sequence[str]) -> str:
    """Return suffixes as an English list, such as `.yaml, .yml, or .py`."""
    if len(suffixes) <= 2:
        return " or ".join(suffixes)
    return f"{', '.join(suffixes[:-1])}, or {suffixes[-1]}"


def load_song(path: Path, formats: Sequence[SongFormat]) -> LoadedSong:
    """Load the song at `path` with the format whose suffixes contain its suffix exactly.

    A script's `build()` may return a Song or a Graph; either becomes one LoadedSong. Raises
    ValueError when two formats claim one suffix, and SongError when no format claims the path's
    suffix or the format cannot read the song.
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
    return LoadedSong(song_format.load(path))
