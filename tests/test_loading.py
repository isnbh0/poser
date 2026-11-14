"""Loading a song file into one graph model."""

import inspect
from pathlib import Path

import pytest
import yaml

from poser.adapters.song_formats import ScriptSongFormat, YamlSongFormat
from poser.core.loading import LoadedSong, load_song
from poser.core.marks import Mark, Marks
from poser.core.song import Song, SongError

FORMATS = (YamlSongFormat(), ScriptSongFormat())
NOTE = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}


def write_yaml(path: Path, effects: list[str]) -> Path:
    track = {"name": "lead", "sound": "synth", "notes": [NOTE], "effects": effects}
    data = {"bpm": 120, "tracks": [track], "marks": {"parts": [{"beat": 0, "end": 1, "text": "a"}]}}
    path.write_text(yaml.safe_dump(data))
    return path


def write_graph_script(path: Path) -> Path:
    path.write_text(
        "from poser import Graph, Node, Note, Wire\n\n\n"
        "def build() -> Graph:\n"
        "    lead = Node('lead', 'track', sound='synth', notes=(Note(0, 60, 1, 100),))\n"
        "    room = Node('room', 'effect', effect='reverb')\n"
        "    return Graph(120, (lead, room), (Wire('lead', 'room', 'audio'),))\n"
    )
    return path


def test_a_yaml_song_loads_with_its_lowered_graph_and_marks(tmp_path: Path) -> None:
    loaded = load_song(write_yaml(tmp_path / "song.yaml", ["drive"]), FORMATS)

    assert isinstance(loaded.source, Song)
    assert loaded.graph == loaded.source.graph()
    assert loaded.graph.marks == loaded.source.marks == Marks({"parts": [Mark(0, 1, "a")]})


def test_a_script_graph_is_its_own_graph(tmp_path: Path) -> None:
    loaded = load_song(write_graph_script(tmp_path / "song.py"), FORMATS)

    assert loaded.graph is loaded.source


def test_check_effects_names_a_songs_track(tmp_path: Path) -> None:
    loaded = load_song(write_yaml(tmp_path / "song.yaml", ["drive"]), FORMATS)
    loaded.check_effects(["drive"])

    with pytest.raises(SongError, match=r"tracks\[0\]: no effect recipe 'drive'"):
        loaded.check_effects([])


def test_check_effects_names_a_graphs_node(tmp_path: Path) -> None:
    loaded = load_song(write_graph_script(tmp_path / "song.py"), FORMATS)
    loaded.check_effects(["reverb"])

    with pytest.raises(SongError, match=r"nodes\[1\]: no effect recipe 'reverb'"):
        loaded.check_effects(["drive"])


def test_the_graph_is_not_a_constructor_parameter() -> None:
    assert list(inspect.signature(LoadedSong).parameters) == ["source"]
