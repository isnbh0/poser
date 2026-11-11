"""Tests for the signal graph and its laws. No plugins, files, or rendering."""

import math
import re
from collections.abc import Callable, Sequence
from itertools import combinations, permutations
from pathlib import Path
from typing import cast

import pytest
import yaml

from poser.core.graph import ACCEPTS_WIRES, Graph, GraphError, Node, Wire
from poser.core.song import ControlChange, Note, Song, Track, parse_song

SCAFFOLD = Path(__file__).resolve().parents[1] / "src" / "poser" / "scaffold"


def construct[T](cls: type[T], **values: object) -> T:
    """Call a graph class with values of any type, as a script without a type checker may."""
    return cast("Callable[..., T]", cls)(**values)


NOTE: tuple[Note, ...] = (Note(0, 60, 1, 100),)


def make_node(**changes: object) -> Node:
    """A track node like every song track compiles to, with the changes applied."""
    values: dict[str, object] = {
        "name": "lead",
        "kind": "track",
        "gain_db": 0.0,
        "sound": "serum",
        "notes": NOTE,
        "cc": (),
    }
    return construct(Node, **(values | changes))


def named_nodes(*names: str) -> tuple[Node, ...]:
    return tuple(make_node(name=name) for name in names)


def effect_nodes(*names: str) -> tuple[Node, ...]:
    """Effect nodes, which accept audio wires, for the wiring fixtures."""
    return tuple(make_node(name=name, kind="effect", effect="drive") for name in names)


def make_graph(**changes: object) -> Graph:
    values: dict[str, object] = {"bpm": 120, "nodes": named_nodes("lead")}
    return construct(Graph, **(values | changes))


# The enumeration: every directed graph on three labeled nodes with exactly three wires, over all
# nine possible wires — the self-edges included, because a self-edge is a cycle of length one.


NAMES = ("a", "b", "c")
EDGES: tuple[tuple[str, str], ...] = tuple((x, y) for x in NAMES for y in NAMES)
THREE_WIRE_SETS = tuple(combinations(EDGES, 3))


def edge_id(edges: tuple[tuple[str, str], ...]) -> str:
    return ",".join(f"{x}>{y}" for x, y in edges)


def cycles_in(edges: Sequence[tuple[str, str]]) -> list[tuple[str, ...]]:
    """Return every closed walk of length three or less, found without the graph's own search."""
    present = set(edges)

    def walk(*steps: tuple[str, str]) -> tuple[str, ...]:
        return (steps[0][0], *(target for _, target in steps))

    return [
        *[(x, y) for x, y in EDGES if x == y and (x, y) in present],
        *[(x, y, x) for x, y in EDGES if x < y and (x, y) in present and (y, x) in present],
        *[
            walk(*triangle)
            for triangle in (
                (("a", "b"), ("b", "c"), ("c", "a")),
                (("a", "c"), ("c", "b"), ("b", "a")),
            )
            if all(step in present for step in triangle)
        ],
    ]


def normalized(cycle: tuple[str, ...]) -> tuple[str, ...]:
    """Rotate a closed walk to begin at its smallest name, so equal cycles compare equal."""
    stops = cycle[:-1]
    return min((*stops[turn:], *stops[:turn], stops[turn]) for turn in range(len(stops)))


CYCLE_MESSAGE = re.compile(r"^Graph: wires form a cycle (.+)$")


def cycle_in(problem: str) -> tuple[str, ...] | None:
    """Return the node names the problem's cycle walks through, or None when it names no cycle."""
    if match := CYCLE_MESSAGE.match(problem):
        return tuple(match.group(1).split(" -> "))
    return None


def table_accepts(wire: Wire, wires: tuple[Wire, ...], nodes: tuple[Node, ...]) -> bool:
    """Whether the acceptance table lets the wire into its target."""
    kinds = {node.name: node.kind for node in nodes}
    limit = ACCEPTS_WIRES[kinds[wire.to_name]].get(wire.kind, 0)
    if limit is None:
        return True
    incoming = sum(
        1 for other in wires if other.to_name == wire.to_name and other.kind == wire.kind
    )
    return incoming <= limit


def test_the_three_wire_enumeration_is_complete() -> None:
    assert len(EDGES) == len(NAMES) ** 2  # All nine wires, self-edges included.
    assert len(THREE_WIRE_SETS) == math.comb(len(EDGES), 3)  # 84 sets, built by combination.
    acyclic = [edges for edges in THREE_WIRE_SETS if not cycles_in(edges)]
    # An acyclic set of three wires is exactly the forward wires of one total order of the nodes.
    forward = sorted(tuple(sorted(combinations(order, 2))) for order in permutations(NAMES))
    assert sorted(acyclic) == forward
    assert len(acyclic) == len(list(permutations(NAMES)))


@pytest.mark.parametrize(
    "edges", THREE_WIRE_SETS, ids=[edge_id(edges) for edges in THREE_WIRE_SETS]
)
def test_three_wire_sets_obey_the_laws(edges: tuple[tuple[str, str], ...]) -> None:
    nodes = effect_nodes(*NAMES)
    wires = tuple(Wire(x, y, "audio") for x, y in edges)
    closed = cycles_in(edges)
    accepted = all(table_accepts(wire, wires, nodes) for wire in wires)
    rejected = [wire for wire in wires if not table_accepts(wire, wires, nodes)]

    if accepted and not closed:
        assert Graph(120, nodes, wires).wires == wires
        return

    with pytest.raises(GraphError) as raised:
        Graph(120, nodes, wires)
    problems = raised.value.problems

    # Acceptance is diagnosed first, per rejected wire and in wire order; the cycle comes second.
    rejections = [problem for problem in problems if problem.startswith("Graph.wires[")]
    assert rejections == problems[: len(rejected)]
    assert len(rejections) == len(rejected)
    for problem, wire in zip(rejections, rejected, strict=True):
        assert wire.to_name in problem
        assert wire.kind in problem

    reported = [walk for problem in problems if (walk := cycle_in(problem)) is not None]
    assert len(reported) == (1 if closed else 0)
    assert len(problems) == len(rejected) + len(reported)
    expect = {normalized(cycle) for cycle in closed}
    assert all(normalized(walk) in expect for walk in reported)


def test_cycle_message_names_the_cycle() -> None:
    nodes = effect_nodes(*NAMES)
    wires = (Wire("a", "b", "audio"), Wire("b", "c", "audio"), Wire("c", "a", "audio"))
    with pytest.raises(GraphError) as raised:
        Graph(120, nodes, wires)
    walks = [walk for problem in raised.value.problems if (walk := cycle_in(problem))]
    assert len(walks) == 1
    assert set(walks[0]) == set(NAMES)  # All three names.
    assert walks[0][0] == walks[0][-1]  # The names close on the node they started at.


def test_self_edge_is_a_cycle_of_length_one() -> None:
    nodes = effect_nodes("a")
    with pytest.raises(GraphError) as raised:
        Graph(120, nodes, (Wire("a", "a", "audio"),))
    walks = [walk for problem in raised.value.problems if (walk := cycle_in(problem))]
    assert walks == [("a", "a")]


# Edge kinds and acceptance (L3): audio and control are distinct wires.


def test_wire_kinds_are_distinct() -> None:
    audio = Wire("a", "b", "audio")
    control = Wire("a", "b", "control")
    assert audio != control
    assert audio.kind == "audio"
    assert control.kind == "control"


def test_a_track_node_rejects_every_incoming_wire() -> None:
    for kind in ("audio", "control"):
        with pytest.raises(GraphError) as raised:
            Graph(120, named_nodes("lead", "wet"), (Wire("wet", "lead", kind),))
        assert raised.value.problems == [
            f"Graph.wires[0]: node 'lead' of kind 'track' accepts no {kind} wires"
        ]


def test_an_effect_node_accepts_many_audio_wires_and_one_control_wire() -> None:
    """C11 and C13: a bus's many sends, and the one control wire a sidechain recipe steers by."""
    nodes = (make_node(name="lead"), make_node(name="kick"), *effect_nodes("bus"))
    one = Graph(120, nodes, (Wire("lead", "bus", "audio"),))
    assert [wire.to_name for wire in one.wires] == ["bus"]
    two = Graph(120, nodes, (Wire("lead", "bus", "audio"), Wire("kick", "bus", "audio")))
    assert len(two.wires) == 2
    ducked = Graph(120, nodes, (Wire("lead", "bus", "audio"), Wire("kick", "bus", "control")))
    assert [wire.kind for wire in ducked.wires] == ["audio", "control"]


def test_a_second_control_wire_is_one_problem_naming_the_limit() -> None:
    nodes = (make_node(name="lead"), make_node(name="kick"), *effect_nodes("duck"))
    wires = (
        Wire("lead", "duck", "audio"),
        Wire("kick", "duck", "audio"),
        Wire("kick", "duck", "control"),
        Wire("lead", "duck", "control"),
    )

    with pytest.raises(GraphError) as raised:
        Graph(120, nodes, wires)

    assert raised.value.problems == [
        "Graph.wires[3]: node 'duck' of kind 'effect' accepts at most 1 control wire"
    ]


def test_a_control_wire_alone_on_an_effect_node_is_rejected() -> None:
    """Steering does nothing for a node that hears no audio, so a control wire needs company."""
    nodes = (make_node(name="kick"), *effect_nodes("duck"))

    with pytest.raises(GraphError) as raised:
        Graph(120, nodes, (Wire("kick", "duck", "control"),))

    assert raised.value.problems == [
        "Graph.wires[0]: node 'duck' of kind 'effect' takes a control wire only with an audio wire"
    ]


def test_a_zero_input_effect_node_is_accepted() -> None:
    """An effect with no wires renders from silence (C2's uniform audio law), so it constructs."""
    graph = Graph(120, effect_nodes("lead"))
    assert [node.name for node in graph.nodes] == ["lead"]
    wired = Graph(120, effect_nodes("lead", "wet"), (Wire("wet", "lead", "audio"),))
    assert [node.name for node in wired.topological()] == ["wet", "lead"]


@pytest.mark.parametrize(
    "wire", [Wire("ghost", "a", "audio"), Wire("a", "ghost", "audio")], ids=["source", "target"]
)
def test_wire_to_an_unknown_node_names_it(wire: Wire) -> None:
    nodes = (make_node(name="a"),)
    with pytest.raises(GraphError) as raised:
        Graph(120, nodes, (wire,))
    assert "Graph.wires[0]: names unknown node 'ghost'" in raised.value.problems


def test_control_wire_takes_no_gain() -> None:
    with pytest.raises(GraphError) as raised:
        Wire("a", "b", "control", gain_db=-3)
    assert raised.value.problems == ["Wire.gain_db: a control wire takes no gain, got -3"]
    assert Wire("a", "b", "audio", gain_db=-3).gain_db == -3.0


# The record (C17): to_data writes the graph as plain data, equal graphs alike.


def test_to_data_writes_the_record_of_a_wired_graph() -> None:
    wired = Graph(
        120,
        (make_node(name="lead", gain_db=-3), effect_nodes("drive")[0]),
        (Wire("lead", "drive", "audio", gain_db=-6),),
    )

    assert wired.to_data() == {
        "bpm": 120,
        "mode": "acyclic",
        "nodes": [
            {
                "name": "lead",
                "kind": "track",
                "gain_db": -3,
                "sound": "serum",
                "notes": [{"beat": 0, "pitch": 60, "length": 1, "velocity": 100}],
            },
            {"name": "drive", "kind": "effect", "gain_db": 0, "effect": "drive"},
        ],
        "wires": [{"from_name": "lead", "to_name": "drive", "kind": "audio", "gain_db": -6}],
    }


def test_to_data_separates_graphs_that_differ_and_joins_equal_ones() -> None:
    wired = Graph(
        120,
        (make_node(name="lead"), effect_nodes("drive")[0]),
        (Wire("lead", "drive", "audio"),),
    )
    record = wired.to_data()

    # A changed send gain changes the record, as does a changed mode; equal graphs do not differ.
    assert Graph(120, wired.nodes, (Wire("lead", "drive", "audio", gain_db=-12),)).to_data() != (
        record
    )
    assert Graph(120, wired.nodes, wired.wires, "feedback").to_data() != record
    assert Graph(120, wired.nodes, wired.wires).to_data() == record
    assert (
        Graph(120, wired.nodes, wired.wires).to_data()
        == Graph(120, wired.nodes, wired.wires).to_data()
    )


@pytest.mark.parametrize(
    ("build", "location"),
    [
        (lambda: construct(Node, name="lead", kind="mixer"), "Node.kind:"),
        (lambda: make_node(gain_db=13), "Node.gain_db:"),
        (lambda: make_node(gain_db="loud"), "Node.gain_db:"),
        (lambda: make_node(sound=None), "Node.sound:"),
        (lambda: make_node(sound="bad name"), "Node.sound:"),
        (lambda: make_node(notes=()), "Node.notes:"),
        (lambda: make_node(notes=("note",)), "Node.notes[0]:"),
        (lambda: make_node(cc=None), "Node.cc:"),
        (
            lambda: make_node(cc=(ControlChange(2, 1, 0), ControlChange(1, 1, 0))),
            "Node.cc[1].beat:",
        ),
        (lambda: make_node(cc=(ControlChange(0, 1, 0, "linear"),)), "Node.cc[0].shape:"),
        (lambda: construct(Node, name="wet", kind="effect"), "Node.effect:"),
        (lambda: construct(Node, name="late", kind="delay"), "Node.delay_samples:"),
        (
            lambda: construct(Node, name="late", kind="delay", delay_samples=1.5),
            "Node.delay_samples:",
        ),
        (lambda: Wire("a b", "b", "audio"), "Wire.from_name:"),
        (lambda: Wire("a", "b/c", "audio"), "Wire.to_name:"),
        (lambda: construct(Wire, from_name="a", to_name="b", kind="midi"), "Wire.kind:"),
        (lambda: Wire("a", "b", "audio", gain_db=13), "Wire.gain_db:"),
        (lambda: make_graph(bpm=0), "Graph.bpm:"),
        (lambda: make_graph(bpm="fast"), "Graph.bpm:"),
        (lambda: make_graph(bpm=True), "Graph.bpm:"),
        (lambda: make_graph(nodes=()), "Graph.nodes:"),
        (lambda: make_graph(nodes=("lead",)), "Graph.nodes[0]:"),
        (lambda: make_graph(wires=("wire",)), "Graph.wires[0]:"),
        (lambda: make_graph(mode="looped"), "Graph.mode:"),
    ],
)
def test_invalid_graph_object_raises_with_its_location(
    build: Callable[[], object], location: str
) -> None:
    with pytest.raises(GraphError) as error:
        build()

    assert len(error.value.problems) == 1, error.value.problems
    assert error.value.problems[0].startswith(location), error.value.problems


def test_effect_and_delay_nodes_carry_their_field() -> None:
    assert Node("wet", "effect", effect="plate").effect == "plate"
    assert Node("late", "delay", delay_samples=240).delay_samples == 240


# Node identity (L-names): names become file names.


def test_node_names_differing_only_in_case_are_one_problem() -> None:
    with pytest.raises(GraphError) as raised:
        Graph(120, (make_node(name="lead"), make_node(name="Lead")))
    assert raised.value.problems == ["Graph.nodes: duplicate node name 'Lead'"]


@pytest.mark.parametrize("name", ["a/b", "a[b]", "", "a b", "../escape", "line\n"])
def test_node_name_must_be_safe_as_a_file_name(name: str) -> None:
    # Names become file names, and `[mix]` is reserved for the mix's image.
    with pytest.raises(GraphError) as raised:
        make_node(name=name)
    assert raised.value.problems == [
        f"Node.name: must contain only letters, digits, '_', '-', and '.', got {name!r}"
    ]


def test_a_chain_instance_name_is_a_legal_node_name() -> None:
    assert Node("lead.drive", "effect", effect="drive").name == "lead.drive"
    assert Node("lead.drive.2", "effect", effect="drive").name == "lead.drive.2"
    assert Wire("lead", "lead.drive", "audio").to_name == "lead.drive"
    with pytest.raises(GraphError) as raised:
        make_node(name="lead..drive")
    assert raised.value.problems == [
        "Node.name: must contain only letters, digits, '_', '-', and '.', got 'lead..drive'"
    ]


# The timeline (L-timeline): node beats obey the song-length rule.


def test_graph_past_the_song_limit_is_refused_with_the_song_rule() -> None:
    node = make_node(name="lead", notes=(Note(3600, 60, 1, 100),))
    with pytest.raises(GraphError) as raised:
        Graph(60, (node,))
    assert raised.value.problems == [
        "Graph: must end within 3600 seconds, but its last beat is at 3601 seconds"
    ]


def test_graph_ending_at_the_song_limit_builds() -> None:
    node = make_node(name="lead", notes=(Note(3599, 60, 1, 100),))
    assert Graph(60, (node,)).bpm == 60


# Compilation (RC7): the scaffold demo and a two-track song compile to isolated nodes.


def scaffold_demo() -> Song:
    """The demo song the scaffold ships, as a song value."""
    data = yaml.safe_load((SCAFFOLD / "demo.yaml").read_text(encoding="utf-8"))
    return parse_song(data)


def two_track_song() -> Song:
    note = Note(0, 60, 1, 100)
    return Song(
        120,
        (
            Track("lead", "serum", (note,), gain=-6, cc=(ControlChange(0, 1, 40),)),
            Track("bass", "surge", (Note(2, 45, 2, 90),), gain=2.5),
        ),
    )


@pytest.mark.parametrize(
    "song", [scaffold_demo(), two_track_song()], ids=["scaffold demo", "two tracks"]
)
def test_song_graph_is_one_isolated_node_per_track(song: Song) -> None:
    graph = song.graph()
    assert graph.bpm == song.bpm
    assert graph.wires == ()
    assert graph.mode == "acyclic"
    assert graph.seconds(1) == song.seconds(1)
    assert [node.name for node in graph.nodes] == [track.name for track in song.tracks]
    for node, track in zip(graph.nodes, song.tracks, strict=True):
        assert node.kind == "track"
        assert node.gain_db == track.gain
        assert node.sound == track.sound
        assert node.notes == track.notes
        assert node.cc == track.cc
