"""Rendering: turning each node of a song's graph into a stem, reusing stems whose inputs are
unchanged.

The instrument or effect adapter writes raw audio to a temporary file the core owns. The core
alone makes it `Audio`, decides whether it is `rendered` or `silent`, writes the stem, and stamps
it, so no adapter can get a bad stem stamped. Nodes render in the graph's topological order, so
every upstream stem a node reads was written or found current in the same run.
"""

import tempfile
from collections.abc import Mapping
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
from poser.core.cache import Stamp, node_key, sound_key
from poser.core.graph import Graph, Node
from poser.core.names import Identifier, NodeName, SongId
from poser.core.performance import perform
from poser.core.ports import (
    AudioFileError,
    AudioFiles,
    Effect,
    EffectRenderRequest,
    Instrument,
    RenderFailure,
    RenderRequest,
    RenderStore,
    ResolvedEffect,
    ResolvedSound,
    SongStore,
    SoundFailure,
)
from poser.core.song import Song, is_number
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
) -> ResolvedNode | SoundFailure:
    """Resolve a node's sound or recipe through its adapter, or return why it cannot be used."""
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


def render_song(
    song: Song,
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

    Nodes render in the graph's topological order, so a node reads stems its run just wrote or
    found current. A node whose upstream did not produce a stem is not rendered. Raises SongError
    for a track naming an unknown effect recipe, SongBusyError when another run holds the song's
    lock, and ValueError when the store's stem suffix differs from the audio files'.
    """
    require_stem_format(renders, audio_files)
    store = renders.song(song_id)
    graph = song.graph([*effects])
    with store.lock():
        # Results from older stems must not survive a render.
        store.clear_latest()
        keys: dict[NodeName, str] = {}
        results: dict[NodeName, TrackResult] = {}
        for node in graph.topological():
            result = _render_node(
                song,
                graph,
                node,
                store,
                instruments,
                effects,
                audio_files,
                keys,
                results,
                timeout_s=timeout_s,
                force=force,
            )
            results[NodeName(node.name)] = result
        return list(results.values())


def _render_node(
    song: Song,
    graph: Graph,
    node: Node,
    store: SongStore,
    instruments: Mapping[Identifier, Instrument],
    effects: Mapping[Identifier, Effect],
    audio_files: AudioFiles,
    keys: dict[NodeName, str],
    results: Mapping[NodeName, TrackResult],
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
    key = node_key(graph, node, resolved.sound_key, _upstream_keys(graph, node, keys))
    keys[name] = key
    if not force:
        stamp = store.read_stamp(name)
        if isinstance(stamp, Stamp) and stamp.track_key == key:
            return TrackResult(
                name, stamp.status, stamp.peak_dbfs, store.stem_path(name), cached=True
            )

    store.discard(name)
    if node.kind == "effect":
        rendered = _render_effect_node(song, graph, node, resolved, store, audio_files, timeout_s)
    else:
        rendered = _render_track_node(song, node, resolved, timeout_s)
    if isinstance(rendered, TrackResult):
        return rendered
    return _write_stem(name, key, resolved.sound_key, store, audio_files, rendered)


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
    """Return the node's input: each upstream stem scaled by its wire's gain, summed.

    The sum accumulates in float64 and rounds to stem format once, as the mixer's does, so a sum
    that overflows to a non-finite value raises ValueError instead of clipping.
    """
    total = np.zeros((NUM_CHANNELS, frames), dtype=np.float64)
    for wire in graph.wires:
        if wire.to_name != node.name:
            continue
        stem = audio_files.read(store.stem_path(NodeName(wire.from_name)), frames)
        total += stem.samples * 10 ** (wire.gain_db / 20)
    return Audio(total.astype(np.float32))


def _render_track_node(
    song: Song,
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
    song: Song,
    graph: Graph,
    node: Node,
    resolved: ResolvedNode,
    store: SongStore,
    audio_files: AudioFiles,
    timeout_s: float,
) -> Audio | TrackResult:
    """Render an effect node: the recipe processes the node's upstream stems into raw audio."""
    adapter, recipe = resolved.adapter, resolved.resolved
    if not isinstance(adapter, Effect) or not isinstance(recipe, ResolvedEffect):
        raise ValueError(f"an effect node resolved to a {type(adapter).__name__}, not an effect")
    name = NodeName(node.name)
    # The input is read before the old stem is discarded, so a failure here keeps both.
    frames = stem_samples(song)
    try:
        audio = _upstream_audio(graph, node, store, audio_files, frames)
    except AudioFileError as error:
        return TrackResult(
            name, RenderFailureStatus.CRASHED, detail=f"upstream wrote unusable audio: {error}"
        )
    with tempfile.TemporaryDirectory() as directory:
        request = EffectRenderRequest(audio, recipe, Path(directory) / "audio.npy")
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
