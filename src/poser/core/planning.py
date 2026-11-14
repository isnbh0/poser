"""Planning: dividing a song's graph into render units, each keyed by its render input.

A unit is one node, or a whole cycle group, which renders as one job. Every unit is either
planned, holding the render input its renderer receives and the resolved adapters it calls, with
the input's digest as its key, or unplanned, holding why it cannot render. Planning is pure:
resolution reads configuration and files, loads no plugin, and starts no process, and the plan
itself only computes.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from poser.core.audio import stem_samples
from poser.core.cache import (
    DelayInput,
    DelayMemberInput,
    EffectInput,
    EffectMemberInput,
    GroupFeed,
    GroupInput,
    StemRef,
    TrackInput,
    sound_key,
    unit_key,
)
from poser.core.graph import DelayNode, EffectNode, Graph, GraphNode, TrackNode, Wire
from poser.core.names import Identifier, NodeName
from poser.core.performance import PitchProblem, perform_for
from poser.core.ports import Effect, Instrument, ResolvedEffect, ResolvedSound, SoundFailure
from poser.core.sounds import parse_sound_ref
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus

EMPTY_EFFECTS: Mapping[Identifier, Effect] = cast(
    "Mapping[Identifier, Effect]", MappingProxyType({})
)


@dataclass(frozen=True)
class ResolvedInstrument:
    """A track node's instrument, its resolved sound, and the cache key of that identity."""

    adapter: Instrument
    resolved: ResolvedSound
    sound_key: str  # `sound_key` of the adapter's id and version and the resolved identity.


@dataclass(frozen=True)
class ResolvedEffectNode:
    """An effect node's adapter, its resolved recipe, and the cache key of that identity."""

    adapter: Effect
    resolved: ResolvedEffect
    sound_key: str  # `sound_key` of the adapter's id and version and the resolved identity.


@dataclass(frozen=True)
class ResolvedDelay:
    """A delay node's resolution: its declared delay. A delay renders through no adapter, since
    the core applies the pure delay equation itself."""

    delay_samples: int


type Resolution = ResolvedInstrument | ResolvedEffectNode | ResolvedDelay | SoundFailure


def resolve_node(
    node: GraphNode,
    instruments: Mapping[Identifier, Instrument],
    effects: Mapping[Identifier, Effect] = EMPTY_EFFECTS,
) -> Resolution:
    """Resolve a node's sound, recipe, or declared delay, or return why it cannot be used."""
    match node:
        case DelayNode():
            return ResolvedDelay(node.delay_samples)
        case EffectNode():
            return _resolve_effect(node, effects)
        case TrackNode():
            return _resolve_track(node, instruments)


def resolve_all(
    graph: Graph,
    instruments: Mapping[Identifier, Instrument],
    effects: Mapping[Identifier, Effect] = EMPTY_EFFECTS,
) -> Mapping[str, Resolution]:
    """Resolve every node of the graph, in node order, by node name."""
    return MappingProxyType(
        {node.name: resolve_node(node, instruments, effects) for node in graph.nodes}
    )


def _resolve_track(
    node: TrackNode, instruments: Mapping[Identifier, Instrument]
) -> ResolvedInstrument | SoundFailure:
    """Resolve a track node's sound through its instrument, or return why it cannot be used."""
    # The node's constructor checked that its sound parses as a reference.
    ref = parse_sound_ref(node.sound)
    instrument = instruments.get(ref.instrument)
    if instrument is None:
        return SoundFailure(
            SoundFailureStatus.UNKNOWN_SOUND, f"no instrument {ref.instrument!r} in poser.toml"
        )
    sound = instrument.resolve(ref)
    if isinstance(sound, SoundFailure):
        return sound
    key = sound_key(instrument.adapter_id, instrument.adapter_version, sound.identity)
    return ResolvedInstrument(instrument, sound, key)


def _resolve_effect(
    node: EffectNode, effects: Mapping[Identifier, Effect]
) -> ResolvedEffectNode | SoundFailure:
    """Resolve an effect node's recipe through its adapter, or return why it cannot be used."""
    recipe = node.effect
    if (
        not Identifier.PATTERN.fullmatch(recipe)
        or (effect := effects.get(Identifier(recipe))) is None
    ):
        return SoundFailure(SoundFailureStatus.UNKNOWN_SOUND, f"no effect {recipe!r} in poser.toml")
    resolved = effect.resolve(recipe)
    if isinstance(resolved, SoundFailure):
        return resolved
    key = sound_key(effect.adapter_id, effect.adapter_version, resolved.identity)
    return ResolvedEffectNode(effect, resolved, key)


# Units. A planned unit's key is derived from its input in the constructor, so the two cannot
# disagree; each also checks that its input digests the identity of the adapter it calls.


def _keyed(unit: object, render_input: TrackInput | EffectInput | DelayInput | GroupInput) -> None:
    object.__setattr__(unit, "key", unit_key(render_input))


@dataclass(frozen=True)
class TrackUnit:
    """A track node: its instrument plays `input.performance` on the resolved sound."""

    name: NodeName
    input: TrackInput
    resolved: ResolvedInstrument
    key: str = field(init=False)

    def __post_init__(self) -> None:
        if self.input.sound != self.resolved.sound_key:
            raise ValueError(f"track {self.name!r}'s input digests another sound than it plays")
        _keyed(self, self.input)

    @property
    def nodes(self) -> tuple[NodeName, ...]:
        return (self.name,)


@dataclass(frozen=True)
class EffectUnit:
    """An effect node off any cycle: its recipe processes the stems `input` names."""

    name: NodeName
    input: EffectInput
    resolved: ResolvedEffectNode
    key: str = field(init=False)

    def __post_init__(self) -> None:
        if self.input.recipe != self.resolved.sound_key:
            raise ValueError(f"effect {self.name!r}'s input digests another recipe than it runs")
        _keyed(self, self.input)

    @property
    def nodes(self) -> tuple[NodeName, ...]:
        return (self.name,)


@dataclass(frozen=True)
class DelayUnit:
    """A delay node off any cycle: the core delays the stems `input` names."""

    name: NodeName
    input: DelayInput
    key: str = field(init=False)

    def __post_init__(self) -> None:
        _keyed(self, self.input)

    @property
    def nodes(self) -> tuple[NodeName, ...]:
        return (self.name,)


@dataclass(frozen=True)
class GroupUnit:
    """A cycle group, rendered as one job: its members in name order, and each effect member's
    resolved recipe by name."""

    nodes: tuple[NodeName, ...]
    input: GroupInput
    resolved: Mapping[str, ResolvedEffectNode]
    key: str = field(init=False)

    def __post_init__(self) -> None:
        if tuple(member.name for member in self.input.members) != self.nodes:
            raise ValueError(f"cycle group {self.nodes}'s input names other members")
        recipes = {
            member.name: member.recipe
            for member in self.input.members
            if isinstance(member, EffectMemberInput)
        }
        if recipes != {name: found.sound_key for name, found in self.resolved.items()}:
            raise ValueError(f"cycle group {self.nodes}'s input digests other recipes than it runs")
        object.__setattr__(self, "resolved", MappingProxyType(dict(self.resolved)))
        _keyed(self, self.input)


type Planned = TrackUnit | EffectUnit | DelayUnit | GroupUnit

type UnplannedStatus = SoundFailureStatus | RenderFailureStatus


@dataclass(frozen=True)
class Unplanned:
    """A unit that cannot render, and so has no input and no key.

    `cause` is the unit's node that `status` and `detail` describe; every other node of the unit
    is upstream_failed naming it, since a cycle group renders whole or not at all.
    """

    nodes: tuple[NodeName, ...]
    status: UnplannedStatus
    detail: str
    cause: NodeName

    def __post_init__(self) -> None:
        if self.cause not in self.nodes:
            raise ValueError(f"Unplanned.cause {self.cause!r} is not one of {self.nodes}")

    def failure_of(self, name: NodeName) -> tuple[UnplannedStatus, str]:
        """Return the status and detail the unit's node `name` fails with."""
        if name == self.cause:
            return self.status, self.detail
        return RenderFailureStatus.UPSTREAM_FAILED, f"upstream {self.cause!r} did not render"


type Unit = Planned | Unplanned


@dataclass(frozen=True)
class Plan:
    """A graph's render units in render order, every upstream unit first, and each node's
    wire sources in wire order."""

    units: tuple[Unit, ...]
    upstream: Mapping[str, tuple[NodeName, ...]]
    _places: Mapping[str, tuple[Unit, int]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        places: dict[str, tuple[Unit, int]] = {}
        for unit in self.units:
            for index, name in enumerate(unit.nodes):
                if name in places:
                    raise ValueError(f"node {name!r} sits in two units")
                places[name] = (unit, index)
        if set(places) != set(self.upstream):
            raise ValueError("a plan's units and upstream map must name the same nodes")
        object.__setattr__(self, "_places", MappingProxyType(places))
        object.__setattr__(self, "upstream", MappingProxyType(dict(self.upstream)))

    def place(self, name: str) -> tuple[Unit, int]:
        """Return the node's unit and its position among the unit's nodes."""
        return self._places[name]


def plan(graph: Graph, resolutions: Mapping[str, Resolution]) -> Plan:
    """Divide the graph into units in `graph.groups()` order and plan each one.

    A unit is unplanned when a unit it reads from is unplanned (upstream_failed, naming that
    upstream node), when a node's sound or recipe does not resolve (the failure's status and
    detail), or when a track plays fractional pitches on an instrument that plays only whole ones
    (unsupported_pitch). Upstream failures are checked first, so a node downstream of a failure
    reports that failure whatever its own resolution.
    """
    frames = stem_samples(graph)
    units: list[Unit] = []
    places: dict[str, tuple[Unit, int]] = {}
    for group in graph.groups():
        if _holds_cycle(graph, group):
            unit = _plan_group(graph, group, resolutions, places, frames)
        else:
            unit = _plan_node(graph, group[0], resolutions[group[0].name], places, frames)
        units.append(unit)
        for index, name in enumerate(unit.nodes):
            places[name] = (unit, index)
    upstream = {
        node.name: tuple(NodeName(wire.from_name) for wire in graph.incoming(node.name))
        for node in graph.nodes
    }
    return Plan(tuple(units), upstream)


def _holds_cycle(graph: Graph, group: tuple[GraphNode, ...]) -> bool:
    """Whether a strongly connected component holds a cycle: more than one node, or one node
    wired to itself. The graph admits one only in feedback mode, with every loop passing a delay.
    """
    match group:
        case (node,):
            return any(w.from_name == node.name and w.to_name == node.name for w in graph.wires)
        case _:
            return True


def _ref(places: Mapping[str, tuple[Unit, int]], wire: Wire) -> StemRef:
    """Return the stem a wire carries, from an upstream unit already planned."""
    unit, index = places[wire.from_name]
    if isinstance(unit, Unplanned):
        raise AssertionError(f"wire from unplanned {wire.from_name!r} reached a planned unit")
    return StemRef(unit.key, index, wire.gain_db)


def _plan_node(
    graph: Graph,
    node: GraphNode,
    resolution: Resolution,
    places: Mapping[str, tuple[Unit, int]],
    frames: int,
) -> Unit:
    name = NodeName(node.name)
    wires = graph.incoming(node.name)
    for wire in wires:
        if isinstance(places[wire.from_name][0], Unplanned):
            detail = f"upstream {wire.from_name!r} did not render"
            return Unplanned((name,), RenderFailureStatus.UPSTREAM_FAILED, detail, name)
    if isinstance(resolution, SoundFailure):
        return Unplanned((name,), resolution.status, resolution.detail, name)
    audio = tuple(_ref(places, wire) for wire in wires if wire.kind == "audio")
    match node, resolution:
        case TrackNode(), ResolvedInstrument():
            performance = perform_for(graph, node, resolution.adapter.pitches)
            if isinstance(performance, PitchProblem):
                instrument = parse_sound_ref(node.sound).instrument
                detail = f"instrument '{instrument}' {performance.detail}"
                return Unplanned((name,), RenderFailureStatus.UNSUPPORTED_PITCH, detail, name)
            return TrackUnit(name, TrackInput(resolution.sound_key, performance), resolution)
        case EffectNode(), ResolvedEffectNode():
            # The graph keeps a node to at most one control wire.
            control = next((_ref(places, w) for w in wires if w.kind == "control"), None)
            render_input = EffectInput(resolution.sound_key, frames, audio, control)
            return EffectUnit(name, render_input, resolution)
        case DelayNode(), ResolvedDelay():
            return DelayUnit(name, DelayInput(resolution.delay_samples, frames, audio))
        case _:
            raise AssertionError(f"node {node.name!r} resolved to another kind's resolution")


def _plan_group(
    graph: Graph,
    group: tuple[GraphNode, ...],
    resolutions: Mapping[str, Resolution],
    places: Mapping[str, tuple[Unit, int]],
    frames: int,
) -> Unit:
    names = tuple(NodeName(member.name) for member in group)  # Already in name order.
    inside = {member.name for member in group}
    external = [w for w in graph.wires if w.to_name in inside and w.from_name not in inside]
    for member in names:
        for wire in external:
            if wire.to_name == member and isinstance(places[wire.from_name][0], Unplanned):
                detail = f"upstream {wire.from_name!r} did not render"
                return Unplanned(names, RenderFailureStatus.UPSTREAM_FAILED, detail, member)
    for member in names:
        if isinstance(failure := resolutions[member], SoundFailure):
            return Unplanned(names, failure.status, failure.detail, member)
    members: list[EffectMemberInput | DelayMemberInput] = []
    resolved: dict[str, ResolvedEffectNode] = {}
    for node in group:
        match node, resolutions[node.name]:
            case EffectNode(), ResolvedEffectNode() as found:
                members.append(EffectMemberInput(NodeName(node.name), found.sound_key))
                resolved[node.name] = found
            case DelayNode(), ResolvedDelay() as found:
                members.append(DelayMemberInput(NodeName(node.name), found.delay_samples))
            case _:
                raise AssertionError(f"{node.kind} {node.name!r} cannot render in a cycle group")
    render_input = GroupInput(
        frames,
        tuple(members),
        tuple(w for w in graph.wires if w.from_name in inside and w.to_name in inside),
        tuple(GroupFeed(NodeName(w.to_name), _ref(places, w), w.kind) for w in external),
    )
    return GroupUnit(names, render_input, resolved)
