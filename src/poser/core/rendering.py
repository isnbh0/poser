"""Rendering: turning each node of a song's graph into a stem, reusing stems whose inputs are
unchanged.

The instrument writes raw audio to a temporary file the core owns. The core alone makes it
`Audio`, decides whether it is `rendered` or `silent`, writes the stem, and stamps it, so no
adapter can get a bad stem stamped.
"""

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from poser.core.audio import SILENCE_DBFS, AudioProblem, measure_peak_dbfs, read_raw_audio
from poser.core.cache import Stamp, node_key, sound_key
from poser.core.graph import Graph, Node
from poser.core.names import Identifier, SongId, TrackName
from poser.core.performance import perform
from poser.core.ports import (
    AudioFiles,
    Instrument,
    RenderFailure,
    RenderRequest,
    RenderStore,
    ResolvedSound,
    SongStore,
    SoundFailure,
)
from poser.core.song import Song, is_number
from poser.core.sounds import parse_sound_ref
from poser.core.statuses import RenderFailureStatus, RenderStatus, SoundFailureStatus, StemStatus


@dataclass(frozen=True)
class ResolvedNode:
    """A node's instrument, its resolved sound, and the sound's cache key."""

    instrument: Instrument
    sound: ResolvedSound
    sound_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.sound, ResolvedSound):
            raise ValueError(f"ResolvedNode.sound must be a ResolvedSound, got {self.sound!r}")


@dataclass(frozen=True)
class TrackResult:
    """What happened to one track. A stem and its peak exist exactly when the status is a
    StemStatus, and a detail exactly when it is not."""

    track: TrackName
    status: RenderStatus
    peak_dbfs: float | None = None
    stem: Path | None = None
    detail: str | None = None
    cached: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.track, TrackName):
            raise ValueError(f"TrackResult.track must be a TrackName, got {self.track!r}")
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
    node: Node, instruments: Mapping[Identifier, Instrument]
) -> ResolvedNode | SoundFailure:
    """Resolve a track node's sound through its instrument, or return why it cannot be used."""
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
    timeout_s: float,
    force: bool,
) -> list[TrackResult]:
    """Render each node whose inputs changed since its stem was written, or every node if forced.

    Nodes render in the graph's topological order. Raises SongBusyError when another run holds
    the song's lock, and ValueError when the store's stem suffix differs from the audio files'.
    """
    require_stem_format(renders, audio_files)
    store = renders.song(song_id)
    graph = song.graph()
    with store.lock():
        # Results from older stems must not survive a render.
        store.clear_latest()
        return [
            _render_node(song, graph, node, store, instruments, audio_files, timeout_s, force=force)
            for node in graph.topological()
        ]


def _render_node(
    song: Song,
    graph: Graph,
    node: Node,
    store: SongStore,
    instruments: Mapping[Identifier, Instrument],
    audio_files: AudioFiles,
    timeout_s: float,
    *,
    force: bool,
) -> TrackResult:
    name = TrackName(node.name)
    resolved = resolve_node(node, instruments)
    if isinstance(resolved, SoundFailure):
        # A node whose sound cannot be resolved leaves no stem or stamp for later steps.
        store.discard(name)
        return TrackResult(name, resolved.status, detail=resolved.detail)

    # The stamp's key is compared alone: the resolved sound is digested into the node key.
    key = node_key(graph, node, resolved.sound_key, ())
    if not force:
        stamp = store.read_stamp(name)
        if isinstance(stamp, Stamp) and stamp.track_key == key:
            return TrackResult(
                name, stamp.status, stamp.peak_dbfs, store.stem_path(name), cached=True
            )

    # Computed before anything is removed, so a failure here keeps the old stem and stamp.
    performance = perform(song, node)
    # Only a stem written in this run can be stamped.
    store.discard(name)
    with tempfile.TemporaryDirectory() as directory:
        request = RenderRequest(resolved.sound, performance, Path(directory) / "audio.npy")
        failure = resolved.instrument.render(request, timeout_s)
        if isinstance(failure, RenderFailure):
            return TrackResult(name, failure.status, detail=failure.detail)
        audio = read_raw_audio(request.audio_path, performance.samples)
    if isinstance(audio, AudioProblem):
        return TrackResult(
            name,
            RenderFailureStatus.CRASHED,
            detail=f"instrument wrote unusable audio: {audio.message}",
        )

    stem = store.stem_path(name)
    audio_files.write(stem, audio)
    peak = measure_peak_dbfs(audio.samples)
    status = StemStatus.SILENT if peak < SILENCE_DBFS else StemStatus.RENDERED
    store.write_stamp(name, Stamp(key, resolved.sound_key, status, peak))
    return TrackResult(name, status, peak, stem)
