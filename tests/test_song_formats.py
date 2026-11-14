"""Tests for the song formats: YAML song files and Python song scripts."""

import hashlib
import os
import stat
from pathlib import Path
from typing import Self

import numpy as np
import pytest
import yaml
from contracts import check_record_writer_contract, check_song_format_contract
from helpers import SURGE_DIR
from test_song import cc_track_song, gain_song, helper_song, two_layer_song, valid_song

from poser.adapters import song_formats
from poser.adapters.song_formats import (
    SONG_FORMATS,
    ScriptSongFormat,
    YamlSongFormat,
    _UniqueKeyLoader,
    write_yaml_song,
)
from poser.core.graph import DelayNode, EffectNode, Graph, TrackNode, Wire
from poser.core.loading import load_song
from poser.core.marks import Mark, Marks
from poser.core.song import Note, Song, SongError, Track


def loaded(path: Path) -> Song:
    """The song at `path`, which these fixtures always build as a Song, never a Graph."""
    song = load_song(path, SONG_FORMATS).source
    assert isinstance(song, Song)
    return song


def test_valid_song_loads_with_expected_values(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text(yaml.safe_dump(valid_song()))

    song = loaded(path)

    assert song.bpm == 120
    assert song.tracks == (
        Track("lead", "serum", (Note(beat=0, pitch=60, length=1, velocity=100),)),
    )


def test_demo_song_loads_with_two_bare_instrument_tracks_and_eight_notes() -> None:
    song = loaded(SURGE_DIR / "demo.yaml")

    assert len(song.tracks) == 2
    assert all(track.sound == "surge" for track in song.tracks)
    assert sum(len(track.notes) for track in song.tracks) >= 8


def test_missing_file_is_a_song_error(tmp_path: Path) -> None:
    with pytest.raises(SongError, match="cannot read"):
        load_song(tmp_path / "missing.yaml", SONG_FORMATS)


def test_malformed_yaml_is_a_song_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("bpm: [unclosed")
    with pytest.raises(SongError, match="cannot read"):
        load_song(path, SONG_FORMATS)


def test_written_song_loads_back_equal(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    song = helper_song()

    write_yaml_song(song, path)

    assert loaded(path) == song
    assert any(note.beat == 2 / 3 for note in loaded(path).tracks[0].notes)
    assert [p.name for p in tmp_path.iterdir()] == ["song.yaml"]


def test_written_song_keeps_the_format_key_order(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_yaml_song(helper_song(), path)

    data = yaml.safe_load(path.read_text())

    assert list(data) == ["bpm", "tracks"]
    assert all(list(track) == ["name", "sound", "notes"] for track in data["tracks"])
    assert list(data["tracks"][0]["notes"][0]) == ["beat", "pitch", "length", "velocity"]


def test_failed_replace_keeps_the_old_file_and_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "song.yaml"
    path.write_text("old contents\n")

    def fail(*args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(SongError, match="disk full"):
        write_yaml_song(helper_song(), path)

    assert path.read_text() == "old contents\n"
    assert [p.name for p in tmp_path.iterdir()] == ["song.yaml"]


def test_failed_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "song.yaml"
    real_fdopen = song_formats.os.fdopen

    class FailingFile:
        def __init__(self, fd: int, mode: str) -> None:
            self.handle = real_fdopen(fd, mode)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            self.handle.close()
            raise OSError("flush failed")

        def write(self, text: str) -> int:
            return self.handle.write(text)

    monkeypatch.setattr(song_formats.os, "fdopen", FailingFile)
    with pytest.raises(SongError, match="flush failed"):
        write_yaml_song(helper_song(), path)

    assert list(tmp_path.iterdir()) == []


def test_written_song_keeps_the_existing_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text("old\n")
    path.chmod(0o640)

    write_yaml_song(helper_song(), path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_new_song_file_gets_umask_permissions(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        write_yaml_song(helper_song(), tmp_path / "song.yaml")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "song.yaml").stat().st_mode) == 0o644


def test_writing_through_a_symlink_keeps_the_link(tmp_path: Path) -> None:
    real = tmp_path / "real.yaml"
    real.write_text("old\n")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)

    write_yaml_song(helper_song(), link)

    assert link.is_symlink()
    assert load_song(real, SONG_FORMATS).source == helper_song()


def test_writing_into_a_missing_directory_is_a_song_error(tmp_path: Path) -> None:
    with pytest.raises(SongError, match="cannot write"):
        write_yaml_song(helper_song(), tmp_path / "missing" / "song.yaml")


def test_numpy_gain_writes_as_a_plain_float(tmp_path: Path) -> None:
    note = (Note(beat=0, pitch=60, length=1, velocity=100),)
    song = Song(bpm=120, tracks=(Track("lead", "serum", note, gain=np.float64(-6)),))
    path = tmp_path / "song.yaml"

    write_yaml_song(song, path)

    assert loaded(path).tracks[0].gain == -6.0


def test_numbers_write_in_one_spelling(tmp_path: Path) -> None:
    def write(beat: float, length: float, bpm: float) -> str:
        note = (Note(beat=beat, pitch=60, length=length, velocity=100),)
        path = tmp_path / "song.yaml"
        write_yaml_song(Song(bpm=bpm, tracks=(Track("lead", "serum", note),)), path)
        return path.read_text()

    assert write(0, 1, 120) == write(0.0, np.float64(1.0), 120.0)
    assert write(np.float64(0.5), 1, 120) == write(0.5, 1, 120)
    assert "beat: 0.5" in write(np.float64(0.5), 1, 120)


class Velocity(int):
    pass


def test_unrepresentable_value_is_a_song_error(tmp_path: Path) -> None:
    note = (Note(beat=0.5, pitch=60, length=1, velocity=Velocity(100)),)
    song = Song(bpm=120, tracks=(Track("lead", "serum", note),))
    with pytest.raises(SongError, match="cannot write"):
        write_yaml_song(song, tmp_path / "song.yaml")
    assert list(tmp_path.iterdir()) == []


def test_gain_from_yaml_nan_and_inf_is_rejected(tmp_path: Path) -> None:
    for text in (".nan", ".inf"):
        path = tmp_path / "song.yaml"
        path.write_text(
            yaml.safe_dump(valid_song()).replace(
                "  sound: serum", f"  sound: serum\n  gain: {text}"
            )
        )
        with pytest.raises(SongError, match="gain"):
            load_song(path, SONG_FORMATS)


def test_song_with_gains_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_yaml_song(gain_song(), path)

    assert load_song(path, SONG_FORMATS).source == gain_song()
    assert "gain" not in yaml.safe_load(path.read_text())["tracks"][1]


def test_song_with_controller_changes_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    write_yaml_song(cc_track_song(), path)

    assert load_song(path, SONG_FORMATS).source == cc_track_song()


SCRIPT_HEADER = """from poser import ControlChange, Note, Song, Track
"""


def write_script(tmp_path: Path, body: str, name: str = "tune.py") -> Path:
    path = tmp_path / name
    path.write_text(SCRIPT_HEADER + body)
    return path


def test_script_song_loads_equal_to_the_song_it_builds(tmp_path: Path) -> None:
    path = write_script(
        tmp_path,
        """
def build():
    note = Note(0, 60, 1, 100)
    return Song(120, (Track("lead", "serum", (note,), gain=-3),))
""",
    )

    song = load_song(path, SONG_FORMATS).source

    assert song == Song(120, (Track("lead", "serum", (Note(0, 60, 1, 100),), gain=-3.0),))


def test_script_song_may_return_a_graph_the_author_wired(tmp_path: Path) -> None:
    """C14: the direct frontend. The author names every node; a track node stays input-less."""
    header = "from poser import DelayNode, EffectNode, Graph, Note, TrackNode, Wire\n"
    path = tmp_path / "tune.py"
    path.write_text(
        header + "def build():\n"
        '    lead = TrackNode("lead", sound="serum", notes=(Note(0, 60, 1, 100),))\n'
        '    drive = EffectNode("lead.drive", effect="crush")\n'
        '    return Graph(120, (lead, drive), (Wire("lead", "lead.drive", "audio"),))\n'
    )

    graph = load_song(path, SONG_FORMATS).source

    assert isinstance(graph, Graph)
    assert graph.bpm == 120
    assert [node.name for node in graph.nodes] == ["lead", "lead.drive"]
    assert [(w.from_name, w.to_name, w.kind) for w in graph.wires] == [
        ("lead", "lead.drive", "audio")
    ]


def test_script_song_may_return_a_feedback_graph(tmp_path: Path) -> None:
    """C14 with the declared regime: the mode arrives in the returned value, nowhere else."""
    path = write_script(
        tmp_path,
        """
from poser import DelayNode, EffectNode, Graph, Note, TrackNode, Wire


def build():
    note = (Note(0, 60, 1, 100),)
    lead = TrackNode("lead", sound="serum", notes=note)
    loop = EffectNode("lead.loop", effect="crush")
    late = DelayNode("lead.late", delay_samples=512)
    return Graph(
        120,
        (lead, loop, late),
        (Wire("lead", "lead.loop", "audio"), Wire("lead.loop", "lead.late", "audio"),
         Wire("lead.late", "lead.loop", "audio")),
        mode="feedback",
    )
""",
    )

    graph = load_song(path, SONG_FORMATS).source

    assert isinstance(graph, Graph)
    assert graph.mode == "feedback"
    assert [node.kind for node in graph.nodes] == ["track", "effect", "delay"]


def test_a_feedback_graph_whose_instantaneous_graph_is_cyclic_is_a_song_error(
    tmp_path: Path,
) -> None:
    path = write_script(
        tmp_path,
        """
from poser import EffectNode, Graph, Wire


def build():
    a = EffectNode("a", effect="crush")
    b = EffectNode("b", effect="crush")
    return Graph(120, (a, b), (Wire("a", "b", "audio"), Wire("b", "a", "audio")), mode="feedback")
""",
    )

    with pytest.raises(SongError) as raised:
        load_song(path, SONG_FORMATS)

    assert raised.value.problems == [f"{path}: Graph: instantaneous wires form a cycle a -> b -> a"]


def test_the_record_writes_and_keeps_a_graphs_mode(tmp_path: Path) -> None:
    """C17: the record's `mode` round-trips, so the two regimes never share a record text."""
    fmt = YamlSongFormat()
    note = (Note(0, 60, 1, 100),)
    lead = TrackNode("lead", sound="serum", notes=note)
    loop = EffectNode("lead.loop", effect="crush")
    late = DelayNode("lead.late", delay_samples=512)
    wires = (
        Wire("lead", "lead.loop", "audio"),
        Wire("lead.loop", "lead.late", "audio"),
        Wire("lead.late", "lead.loop", "audio"),
    )

    text = fmt.dump(Graph(120, (lead, loop, late), wires, "feedback"))

    data = yaml.safe_load(text)
    assert data["mode"] == "feedback"
    straight = fmt.dump(Graph(120, (lead, loop, late), wires[:2]))
    assert straight != text
    assert "mode: acyclic" in straight


def test_a_misshapen_graph_from_a_script_reports_the_path(tmp_path: Path) -> None:
    """The same constructor laws hold, with the script's path before each problem."""
    header = "from poser import DelayNode, EffectNode, Graph, Note, TrackNode, Wire\n"
    path = tmp_path / "tune.py"
    path.write_text(
        header + "def build():\n"
        '    lead = TrackNode("lead", sound="serum", notes=(Note(0, 60, 1, 100),))\n'
        '    return Graph(120, (lead,), (Wire("lead", "ghost", "audio"),))\n'
    )

    with pytest.raises(SongError) as raised:
        load_song(path, SONG_FORMATS)

    assert raised.value.problems == [f"{path}: Graph.wires[0]: names unknown node 'ghost'"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("x = 1\n", "must define a function build() that returns a Song or a Graph"),
        ("build = 3\n", "must define a function build() that returns a Song or a Graph"),
        ("raise ImportError('no module')\n", "cannot run"),
        (
            "from poser import NoSuchName\n",
            "if the song was written for an earlier poser, see https://",
        ),
        ("def build():\n    raise ValueError('boom')\n", "build() raised ValueError: boom"),
        (
            "def build():\n    return {'bpm': 120, 'tracks': []}\n",
            "must return a Song or a Graph, got dict",
        ),
        ("def build():\n    return 120\n", "must return a Song or a Graph, got int"),
    ],
)
def test_broken_script_song_is_one_song_error_naming_the_file(
    tmp_path: Path, body: str, message: str
) -> None:
    path = write_script(tmp_path, body)

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert len(error.value.problems) == 1
    assert str(path) in error.value.problems[0]
    assert message in error.value.problems[0]


def test_unknown_song_suffix_is_a_song_error(tmp_path: Path) -> None:
    path = tmp_path / "tune.json"
    path.write_text("{}")

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert len(error.value.problems) == 1
    assert str(path) in error.value.problems[0]
    assert ".yaml, .yml, or .py" in error.value.problems[0]


def test_script_song_is_validated_like_yaml(tmp_path: Path) -> None:
    pitch_path = write_script(
        tmp_path,
        """
def build():
    return Song(120, (Track("lead", "serum", (Note(0, 200, 1, 100),)),))
""",
        "pitch.py",
    )
    ramp_path = write_script(
        tmp_path,
        """
def build():
    notes = (Note(0, 60, 1, 100),)
    ramp = (ControlChange(0, 1, 0, "linear"),)
    return Song(120, (Track("lead", "serum", notes, cc=ramp),))
""",
        "ramp.py",
    )

    with pytest.raises(SongError) as pitch_error:
        load_song(pitch_path, SONG_FORMATS)
    with pytest.raises(SongError) as ramp_error:
        load_song(ramp_path, SONG_FORMATS)

    assert pitch_error.value.problems == [
        f"{pitch_path}: Note.pitch: must be a number from 0 to 127, got 200"
    ]
    assert ramp_error.value.problems == [
        f"{ramp_path}: Track.cc[0].shape: linear needs a later change to control 1"
    ]


def test_script_and_yaml_with_one_stem_both_load(tmp_path: Path) -> None:
    write_script(
        tmp_path,
        "def build():\n    return Song(90, (Track('lead', 'serum', (Note(0, 60, 1, 100),)),))\n",
        "x.py",
    )
    (tmp_path / "x.yaml").write_text(yaml.safe_dump(valid_song()))

    assert load_song(tmp_path / "x.py", SONG_FORMATS).graph.bpm == 90
    assert load_song(tmp_path / "x.yaml", SONG_FORMATS).graph.bpm == 120


@pytest.mark.parametrize("where", ["import", "build"])
def test_script_song_that_exits_is_a_song_error(tmp_path: Path, where: str) -> None:
    exits = "import sys\nsys.exit(2)\n"
    body = exits if where == "import" else "def build():\n    import sys\n    sys.exit(2)\n"
    path = write_script(tmp_path, body)

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert "SystemExit" in error.value.problems[0]


@pytest.mark.parametrize(
    ("build", "problem"),
    [
        ("Song(120, None)", "Song.tracks:"),
        ('Song(120, (Track("lead", "serum", None),))', "Track.notes:"),
        (
            'Song(120, (Track("lead", "serum", ((0, 60, 1, 100),)),))',
            "Track.notes[0]:",
        ),
    ],
)
def test_misshapen_script_song_reports_the_path(tmp_path: Path, build: str, problem: str) -> None:
    path = write_script(tmp_path, f"def build():\n    return {build}\n")

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert any(p.startswith(f"{path}: {problem}") for p in error.value.problems), (
        error.value.problems
    )


def test_both_formats_meet_the_song_format_contract(tmp_path: Path) -> None:
    for fmt in SONG_FORMATS:
        directory = tmp_path / type(fmt).__name__
        directory.mkdir()
        check_song_format_contract(fmt, helper_song(), directory)


def test_yaml_format_meets_the_record_writer_contract() -> None:
    for song in (helper_song(), gain_song(), cc_track_song()):
        check_record_writer_contract(YamlSongFormat(), song)


def test_the_yaml_record_writer_accepts_a_graph() -> None:
    """C17: a graph song's record text is the graph's own data, wiring and mode included."""
    fmt = YamlSongFormat()
    lead = TrackNode("lead", sound="serum", notes=(Note(0, 60, 1, 100),))
    drive = EffectNode("lead.drive", effect="crush")
    graph = Graph(120, (lead, drive), (Wire("lead", "lead.drive", "audio"),))

    text = fmt.dump(graph)

    assert text == fmt.dump(graph)
    data = yaml.safe_load(text)
    assert data["bpm"] == 120
    assert data["mode"] == "acyclic"
    assert data["wires"] == [
        {"from_name": "lead", "to_name": "lead.drive", "kind": "audio", "gain_db": 0}
    ]


def test_format_suffixes() -> None:
    assert YamlSongFormat().suffixes == (".yaml", ".yml")
    assert YamlSongFormat().suffix == ".yaml"
    assert ScriptSongFormat().suffixes == (".py",)


def test_yml_suffix_loads_as_yaml(tmp_path: Path) -> None:
    path = tmp_path / "song.yml"
    path.write_text(yaml.safe_dump(valid_song()))
    assert load_song(path, SONG_FORMATS).graph.bpm == 120


def test_text_file_is_not_a_song(tmp_path: Path) -> None:
    path = tmp_path / "x.txt"
    path.write_text("bpm: 120\n")

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert error.value.problems == [f"cannot read {path}: a song is a .yaml, .yml, or .py file"]


def test_two_formats_claiming_one_suffix_raise(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text(yaml.safe_dump(valid_song()))

    with pytest.raises(ValueError, match=r"more than one song format claims the suffix \.yaml"):
        load_song(path, (YamlSongFormat(), YamlSongFormat()))


def test_yaml_format_loads_its_own_dump(tmp_path: Path) -> None:
    fmt = YamlSongFormat()
    for song in (helper_song(), gain_song(), cc_track_song()):
        path = tmp_path / f"song{fmt.suffix}"
        path.write_text(fmt.dump(song))
        assert fmt.load(path) == song


def test_yaml_data_with_engine_is_refused(tmp_path: Path) -> None:
    data = valid_song()
    tracks = data["tracks"]
    assert isinstance(tracks, list)
    tracks[0] = {**tracks[0], "engine": "pedalboard"}
    path = tmp_path / "song.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert error.value.problems == ["tracks[0]: unknown key 'engine'"]


# Marks: records keep their text without marks, carry marks when present, and keys never repeat.

# The dump of `helper_song()` before marks existed, pinned so records of songs without marks keep
# their bytes and their `song_sha256`.
HELPER_SONG_SHA256 = "9c522a73c99dbbfb9b40dbf4226849606cb293d61776495ddfc0e0c323ec32ed"


def test_a_dump_without_marks_is_unchanged() -> None:
    text = YamlSongFormat().dump(helper_song())
    assert "marks" not in text
    assert hashlib.sha256(text.encode()).hexdigest() == HELPER_SONG_SHA256


def test_a_dump_with_marks_loads_back_equal(tmp_path: Path) -> None:
    song = two_layer_song()
    text = YamlSongFormat().dump(song)
    assert text.startswith(YamlSongFormat().dump(helper_song()))
    assert "marks:\n  chords:\n" in text
    path = tmp_path / "song.yaml"
    path.write_text(text)
    assert loaded(path) == song


def test_a_deliberate_mark_round_trips_through_yaml(tmp_path: Path) -> None:
    marks = Marks({"chords": [Mark(0, 4, "Am"), Mark(4, 8, "C", deliberate=True)]})
    song = Song(helper_song().bpm, helper_song().tracks, marks=marks)

    text = YamlSongFormat().dump(song)

    assert text.count("deliberate: true") == 1
    path = tmp_path / "song.yaml"
    path.write_text(text)
    assert loaded(path) == song
    assert YamlSongFormat().dump(song.graph()).count("deliberate: true") == 1


def test_a_yaml_mark_states_its_flag_as_true_or_false(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text(MARKED_SONG.replace("text: intro}", "text: intro, deliberate: true}"))
    assert loaded(path).marks == Marks({"parts": [Mark(0, 4, "intro", deliberate=True)]})

    path.write_text(MARKED_SONG.replace("text: intro}", "text: intro, deliberate: 'yes'}"))
    with pytest.raises(SongError) as error:
        loaded(path)
    assert error.value.problems == ["marks.parts[0].deliberate: must be true or false, got 'yes'"]


def test_a_script_song_keeps_its_deliberate_marks(tmp_path: Path) -> None:
    path = write_script(
        tmp_path,
        """
from poser import Mark, Marks, Note, Song, Track


def build():
    lead = Track("lead", "serum", (Note(0, 60, 4, 90),))
    return Song(120, (lead,), marks=Marks({"chords": [Mark(0, 4, "C", deliberate=True)]}))
""",
    )

    song = load_song(path, SONG_FORMATS).source

    assert isinstance(song, Song)
    assert song.marks == Marks({"chords": [Mark(0, 4, "C", deliberate=True)]})


MARKED_SONG = """bpm: 120
tracks:
  - name: lead
    sound: serum
    notes:
      - {beat: 0, pitch: 60, length: 4, velocity: 90}
marks:
  parts:
    - {beat: 0, end: 4, text: intro}
"""


@pytest.mark.parametrize(
    ("text", "key", "line"),
    [
        (MARKED_SONG + "  parts:\n    - {beat: 4, end: 8, text: verse}\n", "parts", 10),
        ("bpm: 90\n" + MARKED_SONG, "bpm", 2),
        (MARKED_SONG.replace("velocity: 90}", "velocity: 90, beat: 1}"), "beat", 6),
    ],
)
def test_a_repeated_yaml_key_fails_naming_the_key_and_line(
    tmp_path: Path, text: str, key: str, line: int
) -> None:
    path = tmp_path / "song.yaml"
    path.write_text(text)
    with pytest.raises(SongError) as error:
        loaded(path)
    assert error.value.problems == [f"cannot read {path}: duplicate key {key!r} at line {line}"]


def test_yaml_merge_keys_still_load(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text(
        "bpm: 120\ntracks:\n  - name: lead\n    sound: serum\n    notes:\n"
        "      - &note {beat: 0, pitch: 60, length: 1, velocity: 90}\n"
        "      - {<<: *note, beat: 1}\n"
    )
    song = loaded(path)
    assert isinstance(song, Song)
    assert [(n.beat, n.pitch, n.velocity) for n in song.tracks[0].notes] == [
        (0, 60, 90),
        (1, 60, 90),
    ]


def test_a_merge_of_a_mapping_that_overrides_its_own_merge_is_no_duplicate(
    tmp_path: Path,
) -> None:
    # The alias `y` merges `x` before `x` itself is built, which flattens `x` in place.
    text = "base: &base {a: 1}\nouter:\n  x: &x {<<: *base, a: 2}\ny: {<<: *x}\n"
    path = tmp_path / "song.yaml"
    path.write_text(text)
    with pytest.raises(SongError) as error:
        loaded(path)
    assert not any("duplicate" in problem for problem in error.value.problems)
    assert yaml.load(text, Loader=_UniqueKeyLoader) == yaml.safe_load(text)  # noqa: S506


def test_a_mapping_merged_elsewhere_still_refuses_its_own_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "song.yaml"
    path.write_text("m: &m {a: 1, a: 2}\nn: {<<: *m}\n")
    with pytest.raises(SongError, match="duplicate key 'a' at line 1"):
        loaded(path)


def test_a_script_song_keeps_its_marks(tmp_path: Path) -> None:
    path = write_script(
        tmp_path,
        """
from poser import Mark, Marks, Note, Song, Track


def build():
    lead = Track("lead", "serum", (Note(0, 60, 4, 90),))
    return Song(120, (lead,), marks=Marks({"parts": [Mark(0, 8, "intro")]}))
""",
    )

    song = load_song(path, SONG_FORMATS).source

    assert isinstance(song, Song)
    assert song.marks == Marks({"parts": [Mark(0, 8, "intro")]})


def test_only_a_failed_import_from_poser_points_to_the_upgrade_guide(tmp_path: Path) -> None:
    path = write_script(tmp_path, "import no_such_module_anywhere\n")

    with pytest.raises(SongError) as error:
        load_song(path, SONG_FORMATS)

    assert "earlier poser" not in error.value.problems[0]
