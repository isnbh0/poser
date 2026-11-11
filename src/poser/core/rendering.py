"""Rendering: turning each node of a song's graph into a stem, reusing stems whose inputs are
unchanged.

The instrument or effect adapter writes raw audio to a temporary file the core owns. The core
alone makes it `Audio`, decides whether it is `rendered` or `silent`, writes the stem, and stamps
it, so no adapter can get a bad stem stamped. Nodes render in the graph's cycle-group order, its
topological order while no group holds a cycle, so every upstream stem a node reads was written or
found current in the same run. A feedback graph's cycle groups render one job per group, stepped
through fixed blocks with previous-block feedback buffers that live in the job.
"""

import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import cast

import numpy as np

from poser.core.audio import (
    NUM_CHANNELS,
    SILENCE_DBFS,
    Audio,
    AudioProblem,
    measure_peak_dbfs,
    read_raw_audio,
    stem_samples,
)
from poser.core.cache import Stamp, cycle_key, delay_identity, node_key, sound_key
from poser.core.graph import Graph, Node, Wire
from poser.core.names import Identifier, NodeName, SongId
from poser.core.performance import perform
from poser.core.ports import (
    AudioFileError,
    AudioFiles,
    Effect,
    EffectRenderRequest,
    GroupEffect,
    GroupMember,
    GroupRenderRequest,
    Instrument,
    RenderFailure,
    RenderRequest,
    RenderStore,
    ResolvedEffect,
    ResolvedSound,
    SongStore,
    SoundFailure,
)
from poser.core.song import Song, is_number, song_graph
from poser.core.sounds import parse_sound_ref
from poser.core.statuses import RenderFailureStatus, RenderStatus, SoundFailureStatus, StemStatus

EMPTY_EFFECTS: Mapping[Identifier, Effect] = cast(
    "Mapping[Identifier, Effect]", MappingProxyType({})
)


@dataclass(frozen=True)
class ResolvedNode:
    """A node's adapter, its resolved sound or recipe, and the cache key of that identity."""

    adapter: Instrument | Effect
    resolved: ResolvedSound | ResolvedEffect
    sound_key: str  # `sound_key` of the adapter's id and version and the resolved identity.

    def __post_init__(self) -> None:
        if not isinstance(self.resolved, ResolvedSound | ResolvedEffect):
            raise ValueError(
                "ResolvedNode.resolved must be a ResolvedSound or ResolvedEffect, "
                f"got {self.resolved!r}"
            )


@dataclass(frozen=True)
class ResolvedDelay:
    """A delay node's resolution: its declared delay, digested as its identity.

    A delay renders through no adapter: the core applies the pure delay equation itself, so its
    identity is the declaration alone.
    """

    identity: str


@dataclass(frozen=True)
class TrackResult:
    """What happened to one node. A stem and its peak exist exactly when the status is a
    StemStatus, and a detail exactly when it is not."""

    track: NodeName
    status: RenderStatus
    peak_dbfs: float | None = None
    stem: Path | None = None
    detail: str | None = None
    cached: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.track, NodeName):
            raise ValueError(f"TrackResult.track must be a NodeName, got {self.track!r}")
        if not isinstance(self.status, StemStatus | SoundFailureStatus | RenderFailureStatus):
            raise ValueError(f"TrackResult.status must be a render status, got {self.status!r}")
        if not isinstance(self.cached, bool):
            raise ValueError(f"TrackResult.cached must be a bool, got {self.cached!r}")
        if isinstance(self.status, StemStatus):
            if not isinstance(self.stem, Path):
                raise ValueError(f"TrackResult with status {self.status} needs a stem path")
            if not is_number(self.peak_dbfs):
                raise ValueError(f"TrackResult with status {self.status} needs a finite peak")
            if self.detail is not None:
                raise ValueError(f"TrackResult with status {self.status} has no detail")
        else:
            if self.stem is not None or self.peak_dbfs is not None:
                raise ValueError(f"TrackResult with status {self.status} has no stem or peak")
            if not (isinstance(self.detail, str) and self.detail):
                raise ValueError(f"TrackResult with status {self.status} needs a detail")
            if self.cached:
                raise ValueError(f"TrackResult with status {self.status} cannot be cached")


def resolve_node(
    node: Node,
    instruments: Mapping[Identifier, Instrument],
    effects: Mapping[Identifier, Effect] = EMPTY_EFFECTS,
) -> ResolvedNode | ResolvedDelay | SoundFailure:
    """Resolve a node's sound, recipe, or declared delay, or return why it cannot be used."""
    if node.kind == "delay":
        return ResolvedDelay(delay_identity(cast("int", node.delay_samples)))
    if node.kind == "effect":
        return _resolve_effect(node, effects)
    # The node's constructor checked that a track node's sound parses as a reference.
    ref = parse_sound_ref(cast("str", node.sound))
    instrument = instruments.get(ref.instrument)
    if instrument is None:
        return SoundFailure(
            SoundFailureStatus.UNKNOWN_SOUND, f"no instrument {ref.instrument!r} in poser.toml"
        )
    sound = instrument.resolve(ref)
    if isinstance(sound, SoundFailure):
        return sound
    key = sound_key(instrument.adapter_id, instrument.adapter_version, sound.identity)
    return ResolvedNode(instrument, sound, key)


def _resolve_effect(
    node: Node, effects: Mapping[Identifier, Effect]
) -> ResolvedNode | SoundFailure:
    """Resolve an effect node's recipe through its adapter, or return why it cannot be used."""
    recipe = cast("str", node.effect)
    if (
        not Identifier.PATTERN.fullmatch(recipe)
        or (effect := effects.get(Identifier(recipe))) is None
    ):
        return SoundFailure(SoundFailureStatus.UNKNOWN_SOUND, f"no effect {recipe!r} in poser.toml")
    resolved = effect.resolve(recipe)
    if isinstance(resolved, SoundFailure):
        return resolved
    key = sound_key(effect.adapter_id, effect.adapter_version, resolved.identity)
    return ResolvedNode(effect, resolved, key)


def require_stem_format(renders: RenderStore, audio_files: AudioFiles) -> None:
    """Raise ValueError unless the store names stems with the audio files' suffix."""
    if renders.stem_suffix != audio_files.suffix:
        raise ValueError(
            f"the render store's stem suffix {renders.stem_suffix} differs from the audio "
            f"files' suffix {audio_files.suffix}"
        )


def delay_line(history: np.ndarray, block: np.ndarray, delay_samples: int) -> np.ndarray:
    """Return `block` through the delay equation: out[t] = in[t - delay_samples], where `history`
    holds every input frame before the block, silence before the line fills, and no dry term.

    Slicing only, so any block size and any delay work without copying the line, and the samples
    are exactly the input's, moved.
    """
    frames = block.shape[1]
    out = np.zeros_like(block)
    filled = history.shape[1]  # Input frames the line already holds.
    first = max(0, delay_samples - filled)  # The first output whose read reaches the line.
    if first < frames:
        read = filled + first - delay_samples  # The input frame output frame `first` reads.
        take = frames - first
        past = min(filled - read, take)  # Reads that land in `history`.
        if past > 0:
            out[:, first : first + past] = history[:, read : read + past]
        if take > past:
            out[:, first + past :] = block[:, : take - past]
    return out


def delay_signal(line: np.ndarray, start: int, count: int, delay_samples: int) -> np.ndarray:
    """Return the delay equation's output for `count` frames from `start`, reading the line that
    holds the delay's input by absolute sample index: out[t] = in[t - delay_samples], silence
    before the line fills, and no dry term.

    A wire out of a delay member of a block pass reads its source this way, so a reader takes the
    delay's true output signal whether the delay has run in the block yet or not; an in-cycle
    delay reads only frames earlier blocks wrote.
    """
    out = np.zeros((line.shape[0], count), dtype=np.float32)
    first = max(start - delay_samples, 0)  # The first line frame any output reads.
    last = min(start + count - delay_samples, line.shape[1])  # One past the last.
    if last > first:
        out[:, first - start + delay_samples : last - start + delay_samples] = line[:, first:last]
    return out


def cycle_groups(graph: Graph) -> dict[str, tuple[Node, ...]]:
    """Map each member of a cycle group to its group's members, in name order.

    A cycle group is a strongly connected component holding a cycle: more than one node, or one
    node wired to itself. The constructor admits one only in feedback mode, with the group's
    instantaneous graph acyclic, so every loop inside passes a delay member.
    """
    groups: dict[str, tuple[Node, ...]] = {}
    for group in graph.groups():
        held = len(group) > 1 or any(
            wire.from_name == group[0].name and wire.to_name == group[0].name
            for wire in graph.wires
        )
        if not held:
            continue
        for node in group:
            groups[node.name] = group
    return groups


def node_cache_key(
    graph: Graph,
    node: Node,
    identity_of: Mapping[str, str | None],
    keys: Mapping[NodeName, str],
    group: tuple[Node, ...] | None,
) -> str:
    """Return the node's stamp key: the cycle group's digest for a member, the node key otherwise.

    A group renders as one unit, so its members share one digest of the whole group, computed from
    the members and the upstream keys alone, with no member's own key in it: a member's key digests
    its group, and its group must not digest the member's key back. `keys` must already hold every
    upstream outside the group, which the scheduling order guarantees.
    """
    if group is None:
        return node_key(graph, node, identity_of[node.name], _upstream_keys(graph, node, keys))
    names = {member.name for member in group}
    inside = [wire for wire in graph.wires if wire.from_name in names and wire.to_name in names]
    upstream = tuple(
        (wire.to_name, keys[NodeName(wire.from_name)], wire.kind, wire.gain_db)
        for wire in graph.wires
        if wire.to_name in names and wire.from_name not in names
    )
    return cycle_key(graph, node, group, identity_of, inside, upstream)


def render_song(
    song: Song | Graph,
    renders: RenderStore,
    song_id: SongId,
    instruments: Mapping[Identifier, Instrument],
    audio_files: AudioFiles,
    *,
    effects: Mapping[Identifier, Effect] = EMPTY_EFFECTS,
    timeout_s: float,
    force: bool,
) -> list[TrackResult]:
    """Render each node whose inputs changed since its stem was written, or every node if forced.

    The song is a Song, which compiles to its graph, or a Graph, whose effect references resolve
    against the configured recipes first. Nodes render in the graph's cycle-group order, its
    topological order while no group holds a cycle, so a node reads stems its run just wrote or
    found current. A feedback graph's cycle group renders as one job, stepped through fixed
    blocks, with the whole job under the one timeout. A node whose upstream did not produce a
    stem is not rendered, and a group none of whose external inputs are met fails whole. Raises
    SongError for a node naming an unknown effect recipe, SongBusyError when another run holds
    the song's lock, and ValueError when the store's stem suffix differs from the audio files' or
    a node's inputs do not sum to finite audio.
    """
    require_stem_format(renders, audio_files)
    store = renders.song(song_id)
    graph = song_graph(song, [*effects])
    groups = cycle_groups(graph)
    frames = stem_samples(song)
    with store.lock():
        # Results from older stems must not survive a render.
        store.clear_latest()
        keys: dict[NodeName, str] = {}
        results: dict[NodeName, TrackResult] = {}
        for group in graph.groups():
            if len(group) == 1 and group[0].name not in groups:
                results[NodeName(group[0].name)] = _render_node(
                    song,
                    graph,
                    group[0],
                    store,
                    instruments,
                    effects,
                    audio_files,
                    keys,
                    results,
                    groups,
                    timeout_s=timeout_s,
                    force=force,
                )
            else:
                results.update(
                    _render_group(
                        graph,
                        group,
                        store,
                        effects,
                        audio_files,
                        keys,
                        results,
                        groups,
                        frames,
                        timeout_s=timeout_s,
                        force=force,
                    )
                )
        return list(results.values())


def _render_node(
    song: Song | Graph,
    graph: Graph,
    node: Node,
    store: SongStore,
    instruments: Mapping[Identifier, Instrument],
    effects: Mapping[Identifier, Effect],
    audio_files: AudioFiles,
    keys: dict[NodeName, str],
    results: Mapping[NodeName, TrackResult],
    groups: Mapping[str, tuple[Node, ...]],
    timeout_s: float,
    *,
    force: bool,
) -> TrackResult:
    name = NodeName(node.name)
    if (failed := _failed_upstream(graph, node, results)) is not None:
        # The upstream left no stem, so this node's inputs cannot be met this run.
        store.discard(name)
        return TrackResult(name, RenderFailureStatus.UPSTREAM_FAILED, detail=failed)
    resolved = resolve_node(node, instruments, effects)
    if isinstance(resolved, SoundFailure):
        # A node whose sound cannot be resolved leaves no stem or stamp for later steps.
        store.discard(name)
        return TrackResult(name, resolved.status, detail=resolved.detail)

    # The stamp's key is compared alone: the resolved identity and the upstream keys are digested
    # into the node key, so a changed input changes every key downstream of it. Computed before
    # anything is removed, so a failure below keeps the old stem and stamp, and so every node
    # downstream digests this node's key whether this one renders or not.
    identity = resolved.sound_key if isinstance(resolved, ResolvedNode) else resolved.identity
    key = node_cache_key(graph, node, {node.name: identity}, keys, groups.get(node.name))
    keys[name] = key
    if not force:
        stamp = store.read_stamp(name)
        if isinstance(stamp, Stamp) and stamp.track_key == key:
            return TrackResult(
                name, stamp.status, stamp.peak_dbfs, store.stem_path(name), cached=True
            )

    store.discard(name)
    if node.kind == "effect":
        rendered = _render_effect_node(
            song, graph, node, cast("ResolvedNode", resolved), store, audio_files, timeout_s
        )
    elif node.kind == "delay":
        rendered = _render_delay_node(song, graph, node, store, audio_files)
    else:
        rendered = _render_track_node(song, node, cast("ResolvedNode", resolved), timeout_s)
    if isinstance(rendered, TrackResult):
        return rendered
    return _write_stem(name, key, identity, store, audio_files, rendered)


def _render_group(
    graph: Graph,
    members: Sequence[Node],
    store: SongStore,
    effects: Mapping[Identifier, Effect],
    audio_files: AudioFiles,
    keys: dict[NodeName, str],
    results: Mapping[NodeName, TrackResult],
    groups: Mapping[str, tuple[Node, ...]],
    frames: int,
    timeout_s: float,
    *,
    force: bool,
) -> dict[NodeName, TrackResult]:
    """Render one cycle group as a single block pass, stamping every member from one digest.

    The group is a strongly connected component the constructor admitted: effect and delay nodes
    holding every cycle, with the instantaneous graph acyclic. Each member's external inputs are
    summed as for any node, and the adapter steps the whole group through fixed blocks inside one
    worker job, so the feedback buffers never become files and the render timeout covers the whole
    job. A member that cannot be met fails the whole group, since the loop needs every member.
    """
    by_name = {node.name: node for node in members}
    ordered = tuple(by_name[name] for name in sorted(by_name))
    inside = [wire for wire in graph.wires if wire.from_name in by_name and wire.to_name in by_name]
    external = [
        wire for wire in graph.wires if wire.from_name not in by_name and wire.to_name in by_name
    ]
    if skips := _group_skips(ordered, external, results, store):
        return skips

    resolved: dict[str, ResolvedNode] = {}
    unresolved: dict[str, SoundFailure] = {}
    identity_of: dict[str, str | None] = {}
    for member in ordered:
        if member.kind == "delay":
            identity_of[member.name] = delay_identity(cast("int", member.delay_samples))
            continue
        found = _resolve_effect(member, effects)
        if isinstance(found, SoundFailure):
            unresolved[member.name] = found
            identity_of[member.name] = None
        else:
            resolved[member.name] = found
            identity_of[member.name] = found.sound_key
    if unresolved:
        name = next(member.name for member in ordered if member.name in unresolved)
        failure = unresolved[name]
        return _group_failures(ordered, store, name, failure.status, failure.detail)

    for member in ordered:
        keys[NodeName(member.name)] = node_cache_key(
            graph, member, identity_of, keys, groups.get(member.name)
        )
    if not force and (cached := _cached_group(ordered, keys, store)) is not None:
        return cached

    for member in ordered:
        store.discard(NodeName(member.name))
    found = _group_runner(ordered, resolved)
    outputs: dict[NodeName, Audio] = {}
    if isinstance(found, RenderFailure):
        failure = found
    else:
        outputs, failure = _run_group(
            ordered,
            resolved,
            inside,
            external,
            store,
            audio_files,
            found,
            frames,
            timeout_s,
        )
    if failure is not None:
        return {
            NodeName(member.name): TrackResult(
                NodeName(member.name), failure.status, detail=failure.detail
            )
            for member in ordered
        }
    written: dict[NodeName, TrackResult] = {}
    for member in ordered:
        name = NodeName(member.name)
        written[name] = _write_stem(
            name,
            keys[name],
            cast("str", identity_of[member.name]),
            store,
            audio_files,
            outputs[name],
        )
    return written


def _group_skips(
    ordered: tuple[Node, ...],
    external: Sequence[Wire],
    results: Mapping[NodeName, TrackResult],
    store: SongStore,
) -> dict[NodeName, TrackResult] | None:
    """Fail the whole group naming its unmet external inputs, or return None when all are met."""
    skips: dict[str, TrackResult] = {}
    for member in ordered:
        for wire in external:
            if wire.to_name != member.name:
                continue
            upstream = results.get(NodeName(wire.from_name))
            if upstream is None or not isinstance(upstream.status, StemStatus):
                skips[member.name] = TrackResult(
                    NodeName(member.name),
                    RenderFailureStatus.UPSTREAM_FAILED,
                    detail=f"upstream {wire.from_name!r} did not render",
                )
    if not skips:
        return None
    trigger = next(wire.to_name for wire in external if wire.to_name in skips)
    for member in ordered:
        skips.setdefault(
            member.name,
            TrackResult(
                NodeName(member.name),
                RenderFailureStatus.UPSTREAM_FAILED,
                detail=f"upstream {trigger!r} did not render",
            ),
        )
    for name in skips:
        store.discard(NodeName(name))
    return {NodeName(name): skip for name, skip in skips.items()}


def _group_failures(
    ordered: tuple[Node, ...],
    store: SongStore,
    name: str,
    status: RenderFailureStatus | SoundFailureStatus,
    detail: str,
) -> dict[NodeName, TrackResult]:
    """Fail the whole group over one member's failure, naming that member to the rest."""
    for member in ordered:
        store.discard(NodeName(member.name))
    return {
        NodeName(member.name): TrackResult(
            NodeName(member.name),
            status if member.name == name else RenderFailureStatus.UPSTREAM_FAILED,
            detail=detail if member.name == name else f"upstream {name!r} did not render",
        )
        for member in ordered
    }


def _cached_group(
    ordered: tuple[Node, ...],
    keys: Mapping[NodeName, str],
    store: SongStore,
) -> dict[NodeName, TrackResult] | None:
    """Return every member's cached result, or None when any member's stamp misses its key."""
    cached: dict[NodeName, TrackResult] = {}
    for member in ordered:
        name = NodeName(member.name)
        stamp = store.read_stamp(name)
        if not (isinstance(stamp, Stamp) and stamp.track_key == keys[name]):
            return None
        cached[name] = TrackResult(
            name, stamp.status, stamp.peak_dbfs, store.stem_path(name), cached=True
        )
    return cached


def _group_runner(
    ordered: tuple[Node, ...], resolved: Mapping[str, ResolvedNode]
) -> GroupEffect | RenderFailure:
    """Return the adapter that runs the group's job, or why no single adapter can.

    Every effect member must resolve through one adapter, and that adapter must render groups; a
    delay member needs no adapter, since its line travels in the job as a declared number.
    """
    runners = [resolved[member.name].adapter for member in ordered if member.kind == "effect"]
    ids = sorted({adapter.adapter_id for adapter in runners})
    if len(ids) > 1:
        listed = ", ".join(str(identifier) for identifier in ids)
        return RenderFailure(
            RenderFailureStatus.LOAD_FAILED,
            f"the cycle group's effects resolve through different adapters ({listed}); every "
            "effect in a loop must come from one adapter",
        )
    runner = runners[0]
    if not isinstance(runner, GroupEffect):
        return RenderFailure(
            RenderFailureStatus.LOAD_FAILED,
            f"adapter {runner.adapter_id!r} cannot render a cycle group: it has no block pass",
        )
    return runner


def _run_group(
    ordered: tuple[Node, ...],
    resolved: Mapping[str, ResolvedNode],
    inside: Sequence[Wire],
    external: Sequence[Wire],
    store: SongStore,
    audio_files: AudioFiles,
    runner: GroupEffect,
    frames: int,
    timeout_s: float,
) -> tuple[dict[NodeName, Audio], RenderFailure | None]:
    """Build the group's request, run the adapter's one job, and read the outputs back.

    The request's member inputs are summed and scaled by their wires' gains as for any node, and
    the outputs are read while the job's temporary directory still holds them; the core alone
    makes them `Audio` and stamps them.
    """
    members = []
    try:
        for member in ordered:
            audio_wires = [
                wire for wire in external if wire.to_name == member.name and wire.kind == "audio"
            ]
            control_wires = [
                wire for wire in external if wire.to_name == member.name and wire.kind == "control"
            ]
            members.append(
                GroupMember(
                    name=NodeName(member.name),
                    recipe=(
                        None
                        if member.kind == "delay"
                        else cast("ResolvedEffect", resolved[member.name].resolved)
                    ),
                    delay_samples=(None if member.kind == "effect" else member.delay_samples),
                    audio=_sum_audio(audio_wires, store, audio_files, frames, member.name),
                    control=(
                        _control_audio_of(control_wires, store, audio_files, frames)
                        if control_wires
                        else None
                    ),
                )
            )
    except AudioFileError as error:
        return {}, RenderFailure(
            RenderFailureStatus.CRASHED, f"upstream wrote unusable audio: {error}"
        )
    with tempfile.TemporaryDirectory() as directory:
        request = GroupRenderRequest(
            frames=frames,
            members=tuple(members),
            wires=tuple(inside),
            outputs=tuple(
                (NodeName(member.name), Path(directory) / f"{member.name}.npy")
                for member in ordered
            ),
        )
        failure = runner.render_group(request, timeout_s)
        if failure is not None:
            return {}, failure
        try:
            outputs = {
                NodeName(member.name): audio_files.read(
                    Path(directory) / f"{member.name}.npy", frames
                )
                for member in ordered
            }
        except AudioFileError as error:
            return {}, RenderFailure(
                RenderFailureStatus.CRASHED, f"the group's worker wrote unusable audio: {error}"
            )
    return outputs, None


def _failed_upstream(
    graph: Graph, node: Node, results: Mapping[NodeName, TrackResult]
) -> str | None:
    """Return why the node is skipped, naming the first upstream that produced no stem.

    An upstream has a stem exactly when its result's status is a StemStatus, so a failed or
    already-skipped upstream stops this node here.
    """
    for wire in graph.wires:
        if wire.to_name == node.name:
            upstream = results.get(NodeName(wire.from_name))
            if upstream is None or not isinstance(upstream.status, StemStatus):
                return f"upstream {wire.from_name!r} did not render"
    return None


def _upstream_keys(
    graph: Graph, node: Node, keys: Mapping[NodeName, str]
) -> tuple[tuple[str, str, float], ...]:
    """Return each feeding wire's upstream key, wire kind, and wire gain, in wire order."""
    return tuple(
        (keys[NodeName(wire.from_name)], wire.kind, wire.gain_db)
        for wire in graph.wires
        if wire.to_name == node.name
    )


def _upstream_audio(
    graph: Graph, node: Node, store: SongStore, audio_files: AudioFiles, frames: int
) -> Audio:
    """Return the node's audio input: each upstream audio stem scaled by its wire's gain, summed."""
    return _sum_audio(
        [wire for wire in graph.wires if wire.to_name == node.name and wire.kind == "audio"],
        store,
        audio_files,
        frames,
        node.name,
    )


def _sum_audio(
    wires: Sequence[Wire], store: SongStore, audio_files: AudioFiles, frames: int, name: str
) -> Audio:
    """Return the wires' upstream stems scaled by their gains and summed.

    The sum accumulates in float64 and rounds to stem format once, as the mixer's does, and a sum
    that overflows to a non-finite value raises ValueError instead of clipping, as the mixer's
    law has it.
    """
    total = np.zeros((NUM_CHANNELS, frames), dtype=np.float64)
    # A value beyond range becomes infinite, or not a number when infinities cancel, which numpy
    # reports as warnings; the check below reports it instead.
    with np.errstate(over="ignore", invalid="ignore"):
        for wire in wires:
            stem = audio_files.read(store.stem_path(NodeName(wire.from_name)), frames)
            total += stem.samples * 10 ** (wire.gain_db / 20)
        summed = total.astype(np.float32)
    if not np.isfinite(summed).all():
        raise ValueError(
            f"node {name!r}'s inputs are not finite as a sum: a sum exceeds the float32 range"
        )
    return Audio(summed)


def _control_audio(
    graph: Graph, node: Node, store: SongStore, audio_files: AudioFiles, frames: int
) -> Audio | None:
    """Return the one control wire's upstream stem, or None when no control wire feeds the node.

    The graph's constructor keeps a node to at most one control wire.
    """
    return _control_audio_of(
        [wire for wire in graph.wires if wire.to_name == node.name and wire.kind == "control"],
        store,
        audio_files,
        frames,
    )


def _control_audio_of(
    wires: Sequence[Wire], store: SongStore, audio_files: AudioFiles, frames: int
) -> Audio | None:
    """Return the wire's upstream stem, or None when the list holds no control wire."""
    for wire in wires:
        if wire.kind == "control":
            return audio_files.read(store.stem_path(NodeName(wire.from_name)), frames)
    return None


def _render_delay_node(
    song: Song | Graph,
    graph: Graph,
    node: Node,
    store: SongStore,
    audio_files: AudioFiles,
) -> Audio | TrackResult:
    """Render a delay node off a cycle: the pure delay equation on its summed inputs.

    A delay off any cycle is a straight-line node, so the equation runs here on numpy, not in a
    worker: out[t] = in[t - delay_samples], silence before the line fills, no dry term, and every
    input sample moved exactly.
    """
    name = NodeName(node.name)
    frames = stem_samples(song)
    try:
        audio = _upstream_audio(graph, node, store, audio_files, frames)
    except AudioFileError as error:
        return TrackResult(
            name, RenderFailureStatus.CRASHED, detail=f"upstream wrote unusable audio: {error}"
        )
    empty = np.zeros((NUM_CHANNELS, 0), dtype=np.float32)
    return Audio(delay_line(empty, audio.samples, cast("int", node.delay_samples)))


def _render_track_node(
    song: Song | Graph,
    node: Node,
    resolved: ResolvedNode,
    timeout_s: float,
) -> Audio | TrackResult:
    """Render a track node: the instrument plays the node's performance into raw audio."""
    adapter, sound = resolved.adapter, resolved.resolved
    if not isinstance(adapter, Instrument) or not isinstance(sound, ResolvedSound):
        raise ValueError(f"a track node resolved to a {type(adapter).__name__}, not an instrument")
    # Only a stem written in this run can be stamped.
    with tempfile.TemporaryDirectory() as directory:
        performance = perform(song, node)
        request = RenderRequest(sound, performance, Path(directory) / "audio.npy")
        failure = adapter.render(request, timeout_s)
        if isinstance(failure, RenderFailure):
            return TrackResult(NodeName(node.name), failure.status, detail=failure.detail)
        audio = read_raw_audio(request.audio_path, performance.samples)
    if isinstance(audio, AudioProblem):
        return TrackResult(
            NodeName(node.name),
            RenderFailureStatus.CRASHED,
            detail=f"instrument wrote unusable audio: {audio.message}",
        )
    return audio


def _render_effect_node(
    song: Song | Graph,
    graph: Graph,
    node: Node,
    resolved: ResolvedNode,
    store: SongStore,
    audio_files: AudioFiles,
    timeout_s: float,
) -> Audio | TrackResult:
    """Render an effect node: the recipe processes the node's upstream stems into raw audio.

    The audio inputs are summed and scaled by their wires' gains before the call, and a control
    wire's stem travels with the job for the recipe to steer by, its frames matching the audio's.
    """
    adapter, recipe = resolved.adapter, resolved.resolved
    if not isinstance(adapter, Effect) or not isinstance(recipe, ResolvedEffect):
        raise ValueError(f"an effect node resolved to a {type(adapter).__name__}, not an effect")
    name = NodeName(node.name)
    # The input is read before the old stem is discarded, so a failure here keeps both.
    frames = stem_samples(song)
    try:
        audio = _upstream_audio(graph, node, store, audio_files, frames)
        control = _control_audio(graph, node, store, audio_files, frames)
    except AudioFileError as error:
        return TrackResult(
            name, RenderFailureStatus.CRASHED, detail=f"upstream wrote unusable audio: {error}"
        )
    with tempfile.TemporaryDirectory() as directory:
        request = EffectRenderRequest(audio, recipe, Path(directory) / "audio.npy", control=control)
        failure = adapter.render(request, timeout_s)
        if isinstance(failure, RenderFailure):
            return TrackResult(name, failure.status, detail=failure.detail)
        rendered = read_raw_audio(request.audio_path, frames)
    if isinstance(rendered, AudioProblem):
        return TrackResult(
            name,
            RenderFailureStatus.CRASHED,
            detail=f"effect wrote unusable audio: {rendered.message}",
        )
    return rendered


def _write_stem(
    name: NodeName,
    key: str,
    sound_key: str,
    store: SongStore,
    audio_files: AudioFiles,
    audio: Audio,
) -> TrackResult:
    """Write a finished render as the node's stem, stamp it, and report it."""
    stem = store.stem_path(name)
    audio_files.write(stem, audio)
    peak = measure_peak_dbfs(audio.samples)
    status = StemStatus.SILENT if peak < SILENCE_DBFS else StemStatus.RENDERED
    store.write_stamp(name, Stamp(key, sound_key, status, peak))
    return TrackResult(name, status, peak, stem)
