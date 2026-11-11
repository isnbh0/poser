"""The signal graph: nodes and wires as one value, its laws enforced in its only constructor."""

import heapq
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict, cast

from poser.core.rules import (
    GAIN_MAX_DB,
    GAIN_MIN_DB,
    NODE_NAME,
    ControlChange,
    ControlData,
    Note,
    NoteData,
    _control_to_data,
    _duplicate_name_problems,
    _length_problems,
    _note_to_data,
    _plain,
    _song_field_problems,
    _track_problems,
    is_int,
    is_number,
)

# A node is a track, an effect chain, or a delay line; a wire carries audio or control.
type NodeKind = Literal["track", "effect", "delay"]
type WireKind = Literal["audio", "control"]
type Mode = Literal["acyclic", "feedback"]

NODE_KINDS: tuple[NodeKind, ...] = ("track", "effect", "delay")
WIRE_KINDS: tuple[WireKind, ...] = ("audio", "control")
MODES: tuple[Mode, ...] = ("acyclic", "feedback")

# Which incoming wires each kind of node accepts, per wire kind: the most wires of that kind the
# node takes, or None for any number. An effect node takes any number of audio wires, including
# none at all (it then renders the silence it is fed), and at most one control wire, which it
# takes only alongside an audio wire, since steering does nothing for a node that hears nothing;
# whether its recipe can use the control edge is a render-time fact its adapter checks. A track
# node takes no wires: sources are sources. P5 widens this table further; the acceptance rule
# never changes.
ACCEPTS_WIRES: dict[NodeKind, dict[WireKind, int | None]] = {
    "track": {"audio": 0, "control": 0},
    "effect": {"audio": None, "control": 1},
    "delay": {"audio": 0, "control": 0},
}


class GraphError(Exception):
    """A graph could not be built. Holds one message per problem, like SongError."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


# Every graph object is valid: its constructor raises `GraphError` listing every problem.


@dataclass(frozen=True)
class Node:
    """One node of the graph: a track, an effect, or a delay, with its output fader."""

    name: str
    kind: NodeKind
    gain_db: float = 0.0
    sound: str | None = None
    notes: tuple[Note, ...] = ()
    cc: tuple[ControlChange, ...] = ()
    effect: str | None = None
    delay_samples: int | None = None

    def __post_init__(self) -> None:
        if problems := _node_problems(self):
            raise GraphError(problems)
        # A list becomes a tuple, so a node built from lists equals one compiled from a song.
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(self, "cc", tuple(self.cc))
        object.__setattr__(self, "gain_db", float(self.gain_db))


@dataclass(frozen=True)
class Wire:
    """One wire of the graph: from one node to another, carrying audio or control."""

    from_name: str
    to_name: str
    kind: WireKind
    gain_db: float = 0.0

    def __post_init__(self) -> None:
        problems: list[str] = []
        for field in ("from_name", "to_name"):
            value = getattr(self, field)
            if not (isinstance(value, str) and NODE_NAME.fullmatch(value)):
                problems.append(
                    f"Wire.{field}: must contain only letters, digits, '_', '-', and '.', "
                    f"got {value!r}"
                )
        if self.kind not in WIRE_KINDS:
            problems.append(f"Wire.kind: must be audio or control, got {self.kind!r}")
        problems += _gain_problems(self.gain_db, "Wire.gain_db")
        # A control wire steers; the loudness law travels on audio wires alone.
        if self.kind == "control" and self.gain_db != 0:
            problems.append(f"Wire.gain_db: a control wire takes no gain, got {self.gain_db!r}")
        if problems:
            raise GraphError(problems)
        object.__setattr__(self, "gain_db", float(self.gain_db))


@dataclass(frozen=True)
class Graph:
    """A whole song as nodes and wires; the constructor enforces the graph's laws."""

    bpm: float
    nodes: tuple[Node, ...]
    wires: tuple[Wire, ...] = ()
    mode: Mode = "acyclic"

    def __post_init__(self) -> None:
        if problems := _graph_problems(self):
            raise GraphError(problems)
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "wires", tuple(self.wires))

    def seconds(self, beat: float) -> float:
        return beat * 60 / self.bpm

    def to_data(self) -> "GraphData":
        """Return the graph as plain data: its tempo and mode, each node's kind payload, and every
        wire whole.

        This is the analysis record's song text for a graph song. Numbers are written in one
        spelling and the field order is fixed, so structurally equal graphs give equal data and
        two graphs that differ in any node, wire, gain, or mode give different data.
        """
        return {
            "bpm": _plain(self.bpm),
            "mode": self.mode,
            "nodes": [_node_to_data(node) for node in self.nodes],
            "wires": [
                {
                    "from_name": wire.from_name,
                    "to_name": wire.to_name,
                    "kind": wire.kind,
                    "gain_db": _plain(wire.gain_db),
                }
                for wire in self.wires
            ],
        }

    def topological(self) -> tuple[Node, ...]:
        """Return the nodes so each follows the nodes its wires come from, ties broken by name.

        Kahn's algorithm, always taking the ready node whose name sorts first, so the order is
        the same for every equal graph. A feedback cycle has no topological position, so its
        nodes follow the rest, by name.
        """
        by_name = {node.name: node for node in self.nodes}
        incoming: dict[str, int] = dict.fromkeys(by_name, 0)
        outgoing: dict[str, list[str]] = {name: [] for name in by_name}
        for wire in self.wires:
            incoming[wire.to_name] += 1
            outgoing[wire.from_name].append(wire.to_name)
        ready = [name for name, count in incoming.items() if count == 0]
        heapq.heapify(ready)
        ordered: list[Node] = []
        while ready:
            name = heapq.heappop(ready)
            ordered.append(by_name[name])
            for target in outgoing[name]:
                incoming[target] -= 1
                if incoming[target] == 0:
                    heapq.heappush(ready, target)
        if len(ordered) < len(by_name):
            # Nodes the cycle holds have no topological position; they follow, by name.
            placed = {node.name for node in ordered}
            ordered += [by_name[name] for name in sorted(set(by_name) - placed)]
        return tuple(ordered)


def _gain_problems(gain_db: object, where: str) -> list[str]:
    """Check a fader against the same decibel range as a track's gain."""
    if is_number(gain_db) and GAIN_MIN_DB <= gain_db <= GAIN_MAX_DB:
        return []
    return [
        (
            f"{where}: must be a number of decibels from {GAIN_MIN_DB} to {GAIN_MAX_DB}, "
            f"got {gain_db!r}"
        )
    ]


def _node_problems(node: Node) -> list[str]:
    """Check a node's name, fader, and the payload its kind carries."""
    # A node's name becomes a file name under stems/ and images/, where `[mix]` names the mix.
    # Chain nodes carry their track's name and recipe with dots between, so dots are legal here
    # where a track's own name forbids them.
    problems: list[str] = []
    if not (isinstance(node.name, str) and NODE_NAME.fullmatch(node.name)):
        problems.append(
            f"Node.name: must contain only letters, digits, '_', '-', and '.', got {node.name!r}"
        )
    if node.kind not in NODE_KINDS:
        problems.append(f"Node.kind: must be track, effect, or delay, got {node.kind!r}")
    problems += _gain_problems(node.gain_db, "Node.gain_db")
    if node.kind == "track":
        problems += _track_problems(
            {"sound": node.sound, "notes": node.notes, "cc": node.cc}, "Node"
        )
    elif node.kind == "effect":
        # Recipes resolve at compose time, so an unknown reference cannot be checked here yet.
        if not (isinstance(node.effect, str) and node.effect):
            problems.append(f"Node.effect: must name a recipe, got {node.effect!r}")
    elif node.kind == "delay" and not (is_int(node.delay_samples) and node.delay_samples > 0):
        problems.append(
            f"Node.delay_samples: must be a positive number of samples, got {node.delay_samples!r}"
        )
    return problems


def _listed(value: object) -> list[object]:
    """Return a list or tuple's items, or none of them when it is neither."""
    return list(value) if isinstance(value, list | tuple) else []


def _graph_problems(graph: Graph) -> list[str]:
    """Check every field of a graph built in code, whose nodes and wires are objects."""
    problems = _song_field_problems(
        {"bpm": graph.bpm, "nodes": graph.nodes}, "Graph", group="nodes", noun="node"
    )
    if graph.mode not in MODES:
        problems.append(f"Graph.mode: must be acyclic or feedback, got {graph.mode!r}")
    node_items = _listed(graph.nodes)
    problems += [
        f"Graph.nodes[{index}]: must be a Node, got {node!r}"
        for index, node in enumerate(node_items)
        if not isinstance(node, Node)
    ]
    nodes = [node for node in node_items if isinstance(node, Node)]
    problems += _duplicate_name_problems(nodes, "Graph", group="nodes", noun="node")
    # The timeline rule needs a valid bpm and valid nodes, so other problems are reported first.
    if not problems and is_number(graph.bpm):
        problems += _length_problems(graph.bpm, nodes, "Graph")
    wire_items = _listed(graph.wires)
    problems += [
        f"Graph.wires[{index}]: must be a Wire, got {wire!r}"
        for index, wire in enumerate(wire_items)
        if not isinstance(wire, Wire)
    ]
    wires = [wire for wire in wire_items if isinstance(wire, Wire)]
    problems += _endpoint_problems(wires, nodes)
    problems += _acceptance_problems(nodes, wires)
    if graph.mode == "acyclic":
        problems += _cycle_problems(nodes, wires)
    return problems


def _endpoint_problems(wires: Sequence[Wire], nodes: Sequence[Node]) -> list[str]:
    """Return one problem per wire end that names no node in the graph."""
    names = {node.name for node in nodes}
    return [
        f"Graph.wires[{index}]: names unknown node {name!r}"
        for index, wire in enumerate(wires)
        for name in (wire.from_name, wire.to_name)
        if name not in names
    ]


def _acceptance_problems(nodes: Sequence[Node], wires: Sequence[Wire]) -> list[str]:
    """Return one problem per wire whose target does not accept its kind of input.

    A control wire is also rejected on its own: steering does nothing for a node that hears no
    audio, so a control wire's target must receive at least one audio wire.
    """
    kinds = {node.name: node.kind for node in nodes}
    problems: list[str] = []
    received: dict[tuple[str, str], int] = {}
    audio: dict[str, int] = {}
    for index, wire in enumerate(wires):
        node_kind = kinds.get(wire.to_name)
        if node_kind is None:
            continue  # The endpoint problem already names the unknown node.
        received[wire.to_name, wire.kind] = received.get((wire.to_name, wire.kind), 0) + 1
        if wire.kind == "audio":
            audio[wire.to_name] = audio.get(wire.to_name, 0) + 1
        limit = ACCEPTS_WIRES[node_kind].get(wire.kind, 0)
        if limit == 0:
            problems.append(
                f"Graph.wires[{index}]: node {wire.to_name!r} of kind {node_kind!r} accepts no "
                f"{wire.kind} wires"
            )
        elif limit is not None and received[wire.to_name, wire.kind] > limit:
            plural = "" if limit == 1 else "s"
            problems.append(
                f"Graph.wires[{index}]: node {wire.to_name!r} of kind {node_kind!r} accepts at "
                f"most {limit} {wire.kind} wire{plural}"
            )
    for index, wire in enumerate(wires):
        node_kind = kinds.get(wire.to_name)
        if node_kind is None or wire.kind != "control":
            continue
        limit = ACCEPTS_WIRES[node_kind].get("control", 0)
        if limit is not None and limit > 0 and audio.get(wire.to_name, 0) == 0:
            problems.append(
                f"Graph.wires[{index}]: node {wire.to_name!r} of kind {node_kind!r} takes a "
                "control wire only with an audio wire"
            )
    return problems


def _node_to_data(node: Node) -> "NodeData":
    """Return a node as plain data: its name, fader, and the payload its kind carries."""
    data: NodeData = {"name": node.name, "kind": node.kind, "gain_db": _plain(node.gain_db)}
    if node.kind == "track":
        data["sound"] = cast("str", node.sound)
        data["notes"] = [_note_to_data(note) for note in node.notes]
        if node.cc:
            data["cc"] = [_control_to_data(change) for change in node.cc]
    elif node.kind == "effect":
        data["effect"] = cast("str", node.effect)
    else:
        data["delay_samples"] = cast("int", node.delay_samples)
    return data


class NodeData(TypedDict):
    """A node's plain-data form, its kind payload present only for its own kind."""

    name: str
    kind: str
    gain_db: float
    sound: NotRequired[str]
    notes: NotRequired[list[NoteData]]
    cc: NotRequired[list[ControlData]]
    effect: NotRequired[str]
    delay_samples: NotRequired[int]


class WireData(TypedDict):
    """A wire's plain-data form: its endpoints, its kind, and its send gain."""

    from_name: str
    to_name: str
    kind: str
    gain_db: float


class GraphData(TypedDict):
    """A graph's plain-data form, the analysis record's song text for a graph song."""

    bpm: float
    mode: str
    nodes: list[NodeData]
    wires: list[WireData]


def _cycle_problems(nodes: Sequence[Node], wires: Sequence[Wire]) -> list[str]:
    """Return one problem naming the first cycle the wires close among the nodes, or none."""
    outgoing: dict[str, list[str]] = {node.name: [] for node in nodes}
    for wire in wires:
        if wire.from_name in outgoing and wire.to_name in outgoing:
            outgoing[wire.from_name].append(wire.to_name)
    unseen, on_path, done = 0, 1, 2
    state: dict[str, int] = {node.name: unseen for node in nodes}
    path: list[str] = []
    trail: list[tuple[str, Iterator[str]]] = []
    for start, targets in outgoing.items():
        if state[start] != unseen:
            continue
        state[start] = on_path
        path.append(start)
        trail.append((start, iter(targets)))
        while trail:
            node, following = trail[-1]
            for target in following:
                if state[target] == unseen:
                    state[target] = on_path
                    path.append(target)
                    trail.append((target, iter(outgoing[target])))
                    break
                if state[target] == on_path:
                    cycle = [*path[path.index(target) :], target]
                    return [f"Graph: wires form a cycle {' -> '.join(cycle)}"]
            else:
                state[node] = done
                path.pop()
                trail.pop()
    return []
