"""Rendering: turning each unit of a song's render plan into stems, reusing stems whose key is
unchanged.

The plan (`poser.core.planning`) divides the graph into units, a node or a whole cycle group, and
gives each planned unit a render input and its digest as key. Each render function receives only
its unit: the input and the resolved adapter whose identity the input digests, so a render cannot
read anything its key does not cover. The instrument or effect adapter writes raw audio to a
temporary file the core owns. The core alone makes it `Audio`, decides whether it is `rendered` or
`silent`, and publishes the unit's stems in the store under the unit's key, so no adapter can get a
bad stem kept. A unit whose key the store already holds is reused, and a unit that fails publishes
nothing. A track whose sound begins late after its first note is rendered again, up to
`RENDER_ATTEMPTS` times, because a plugin that loads its samples in the background can miss notes
played before they arrive. If every attempt starts late by the same amount, the start is the
sound's own and the track renders; if by different amounts, the track fails as `late_start`.
Units render in plan order, every upstream unit first, so every upstream stem a unit reads was
published or found in the same run. A feedback graph's cycle groups render one job per group,
stepped through fixed blocks with previous-block feedback buffers that live in the job.
"""

import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from poser.core.audio import (
    NUM_CHANNELS,
    SAMPLE_RATE,
    SILENCE_DBFS,
    Audio,
    AudioProblem,
    measure_peak_dbfs,
    read_raw_audio,
)
from poser.core.cache import DelayMemberInput, EffectMemberInput, StemRef
from poser.core.loading import LoadedSong
from poser.core.names import Identifier, NodeName, SongId
from poser.core.performance import NoteOn, Performance
from poser.core.planning import (
    EMPTY_EFFECTS,
    DelayUnit,
    EffectUnit,
    GroupUnit,
    Plan,
    ResolvedEffectNode,
    TrackUnit,
    Unit,
    Unplanned,
    plan,
    resolve_all,
)
from poser.core.ports import (
    AudioFileError,
    AudioFiles,
    DelayMember,
    Effect,
    EffectMember,
    EffectRenderRequest,
    GroupEffect,
    GroupRenderRequest,
    Instrument,
    RenderFailure,
    RenderRequest,
    RenderStore,
    SongStore,
    StoredUnit,
)
from poser.core.session import song_session
from poser.core.song import is_number
from poser.core.statuses import RenderFailureStatus, SkipStatus, SoundFailureStatus, StemStatus

# How many times a track is rendered when its first note comes out silent.
RENDER_ATTEMPTS = 3
# How long after the first note-on sound may begin before the render counts as starting late, so
# a plugin's output latency or a soft attack is not mistaken for a missed note.
LATE_START_TOLERANCE_SAMPLES = SAMPLE_RATE // 10

type Notify = Callable[[str], None]


def ignore_progress(_: str) -> None:
    """Drop a progress line."""


def late_start_s(samples: np.ndarray, performance: Performance) -> float | None:
    """Return how long after the first note-on the audio first rises above silence, in seconds,
    when that is longer than LATE_START_TOLERANCE_SAMPLES; otherwise None.

    None also covers a performance without notes and audio silent from the first note-on to the
    end, which is the sound's own result. Audio already sounding at the first note-on counts as
    on time, so a sound that never falls silent cannot be judged.
    """
    onset = next((e.sample for e in performance.events if isinstance(e, NoteOn)), None)
    if onset is None:
        return None
    loud = np.abs(samples[:, onset:]).max(axis=0) > 10 ** (SILENCE_DBFS / 20)
    if not loud.any():
        return None
    delay = int(np.argmax(loud))
    return delay / SAMPLE_RATE if delay > LATE_START_TOLERANCE_SAMPLES else None


@dataclass(frozen=True)
class Rendered:
    """A node with a stem: rendered this run, or found under its key when `cached`."""

    track: NodeName
    status: StemStatus
    peak_dbfs: float
    stem: Path
    cached: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.track, NodeName):
            raise ValueError(f"Rendered.track must be a NodeName, got {self.track!r}")
        if not isinstance(self.status, StemStatus):
            raise ValueError(f"Rendered.status must be a StemStatus, got {self.status!r}")
        if not is_number(self.peak_dbfs):
            raise ValueError(f"Rendered.peak_dbfs must be a finite number, got {self.peak_dbfs!r}")
        if not isinstance(self.stem, Path):
            raise ValueError(f"Rendered.stem must be a Path, got {self.stem!r}")
        if not isinstance(self.cached, bool):
            raise ValueError(f"Rendered.cached must be a bool, got {self.cached!r}")


@dataclass(frozen=True)
class NotRendered:
    """A node left without a stem, and why."""

    track: NodeName
    status: SoundFailureStatus | SkipStatus | RenderFailureStatus
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.track, NodeName):
            raise ValueError(f"NotRendered.track must be a NodeName, got {self.track!r}")
        if not isinstance(self.status, SoundFailureStatus | SkipStatus | RenderFailureStatus):
            raise ValueError(f"NotRendered.status must be a failure status, got {self.status!r}")
        if not (isinstance(self.detail, str) and self.detail):
            raise ValueError(f"NotRendered.detail must be a non-empty str, got {self.detail!r}")


type TrackResult = Rendered | NotRendered


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


@dataclass(frozen=True)
class _RenderRun:
    """One render's plan, store, and settings, and each node's result so far."""

    plan: Plan
    store: SongStore
    audio_files: AudioFiles
    timeout_s: float
    force: bool
    notify: Notify
    results: dict[NodeName, TrackResult] = field(default_factory=dict[NodeName, TrackResult])
    published: set[str] = field(default_factory=set[str])
    """Keys published by this run, which a unit with an identical input reuses even when
    forced."""


def render_song(
    loaded: LoadedSong,
    renders: RenderStore,
    song_id: SongId,
    instruments: Mapping[Identifier, Instrument],
    audio_files: AudioFiles,
    *,
    effects: Mapping[Identifier, Effect] = EMPTY_EFFECTS,
    timeout_s: float,
    force: bool,
    notify: Notify = ignore_progress,
) -> list[TrackResult]:
    """Render each unit whose key the store does not hold, or every unit if forced.

    The song's effect references are checked against the configured recipes first. Units render
    in plan order, every upstream unit first, so a unit reads stems its run just wrote or found
    current. A feedback graph's cycle group renders as one job, stepped through fixed blocks,
    with the whole job under the one timeout. A node whose upstream did not produce a stem is not
    rendered, and a group none of whose external inputs are met fails whole. Each rendering of a
    track again after a late start is reported to `notify` as one line. Raises SongError for a
    track or node naming an unknown effect recipe, SongBusyError when another run holds the
    song's lock, and ValueError when the store's stem suffix differs from the audio files' or a
    node's inputs do not sum to finite audio.
    """
    require_stem_format(renders, audio_files)
    with song_session(renders, song_id) as store:
        return _render(
            loaded, store, instruments, audio_files, effects, timeout_s, force=force, notify=notify
        )


def _render(
    loaded: LoadedSong,
    store: SongStore,
    instruments: Mapping[Identifier, Instrument],
    audio_files: AudioFiles,
    effects: Mapping[Identifier, Effect],
    timeout_s: float,
    *,
    force: bool,
    notify: Notify,
) -> list[TrackResult]:
    """Render inside a caller-owned song session; never acquire a second lock."""
    loaded.check_effects([*effects])
    graph = loaded.graph
    planned = plan(graph, resolve_all(graph, instruments, effects))
    store.sweep()
    # Results from older stems must not survive a render.
    store.clear_latest()
    run = _RenderRun(planned, store, audio_files, timeout_s, force, notify)
    for unit in planned.units:
        for result in _render_unit(run, unit):
            run.results[result.track] = result
    store.keep_units(planned.keys())
    return list(run.results.values())


def _render_unit(run: _RenderRun, unit: Unit) -> tuple[TrackResult, ...]:
    """Return one result per node of the unit, in the unit's node order."""
    if (skipped := _upstream_skips(run, unit)) is not None:
        # An upstream left no stem, so this unit's inputs cannot be met this run.
        return skipped
    if isinstance(unit, Unplanned):
        failures = ((name, *unit.failure_of(name)) for name in unit.nodes)
        return tuple(NotRendered(name, status, detail) for name, status, detail in failures)
    stored = run.store.find(unit.key)
    current = not run.force or unit.key in run.published
    if current and stored is not None and len(stored.stems()) == len(unit.nodes):
        return _cached(unit.nodes, stored)
    rendered: tuple[Audio, ...] | _Failed
    match unit:
        case GroupUnit():
            rendered = _render_group(run, unit)
        case TrackUnit():
            rendered = _one(_render_track_node(run, unit))
        case EffectUnit():
            rendered = _one(_render_effect_node(run, unit))
        case DelayUnit():
            rendered = _one(_render_delay_node(run, unit))
    if isinstance(rendered, _Failed):
        # A failed unit leaves nothing under its key, even a unit a forced render meant to
        # replace, so a later run renders it again.
        if stored is not None:
            run.store.remove_unit(unit.key)
        return tuple(NotRendered(name, rendered.status, rendered.detail) for name in unit.nodes)
    return _publish(run, unit.key, unit.nodes, rendered, replace=stored is not None)


@dataclass(frozen=True)
class _Failed:
    """Why a unit produced no audio; every node of the unit fails with it."""

    status: RenderFailureStatus | SoundFailureStatus
    detail: str


def _one(rendered: Audio | _Failed) -> tuple[Audio, ...] | _Failed:
    return rendered if isinstance(rendered, _Failed) else (rendered,)


def _publish(
    run: _RenderRun,
    key: str,
    nodes: tuple[NodeName, ...],
    outputs: tuple[Audio, ...],
    *,
    replace: bool,
) -> tuple[TrackResult, ...]:
    """Write a unit's finished renders as its stems, publish them under its key, and report them.

    `replace` swaps out a unit the store already holds under the key, which a forced render or an
    incomplete stored unit needs.
    """
    with run.store.write_unit(key, replace=replace) as draft:
        for index, audio in enumerate(outputs):
            run.audio_files.write(draft.stem_path(index), audio)
            peak = measure_peak_dbfs(audio.samples)
            status = StemStatus.SILENT if peak < SILENCE_DBFS else StemStatus.RENDERED
            draft.describe(index, status, peak)
    stored = run.store.find(key)
    if stored is None:
        raise RuntimeError(f"the store did not publish unit {key}")
    run.published.add(key)
    return tuple(
        Rendered(name, meta.status, meta.peak_dbfs, stored.stem_path(index))
        for index, (name, meta) in enumerate(zip(nodes, stored.stems(), strict=True))
    )


def _upstream_skips(run: _RenderRun, unit: Unit) -> tuple[TrackResult, ...] | None:
    """Fail the unit naming its unmet upstream, or return None when every upstream has a stem.

    A node with an upstream outside the unit that produced no stem names that upstream; any other
    node of the unit names the first such node, since a cycle group renders whole or not at all.
    """
    unmet: dict[NodeName, NodeName] = {}
    for name in unit.nodes:
        for source in run.plan.upstream[name]:
            if source in unit.nodes:
                continue
            upstream = run.results.get(source)
            if not isinstance(upstream, Rendered):
                unmet[name] = source
                break
    if not unmet:
        return None
    trigger = next(name for name in unit.nodes if name in unmet)
    return tuple(
        NotRendered(
            name,
            SkipStatus.UPSTREAM_FAILED,
            f"upstream {unmet.get(name, trigger)!r} did not render",
        )
        for name in unit.nodes
    )


def _cached(nodes: tuple[NodeName, ...], stored: StoredUnit) -> tuple[TrackResult, ...]:
    """Return every node's result from the unit the store holds under its key."""
    return tuple(
        Rendered(name, meta.status, meta.peak_dbfs, stored.stem_path(index), cached=True)
        for index, (name, meta) in enumerate(zip(nodes, stored.stems(), strict=True))
    )


def _render_group(run: _RenderRun, unit: GroupUnit) -> tuple[Audio, ...] | _Failed:
    """Render one cycle group as a single block pass, one output per member in the unit's order.

    The group is a strongly connected component the constructor admitted: effect and delay nodes
    holding every cycle, with the instantaneous graph acyclic. Each member's external inputs are
    summed as for any node, and the adapter steps the whole group through fixed blocks inside one
    worker job, so the feedback buffers never become files and the render timeout covers the whole
    job. A failure fails the whole group, since the loop needs every member.
    """
    found = _group_runner(unit.resolved)
    outputs: dict[NodeName, Audio] = {}
    if isinstance(found, RenderFailure):
        failure = found
    else:
        outputs, failure = _run_group(run, unit, found)
    if failure is not None:
        return _Failed(failure.status, failure.detail)
    return tuple(outputs[name] for name in unit.nodes)


def _group_runner(resolved: Mapping[str, ResolvedEffectNode]) -> GroupEffect | RenderFailure:
    """Return the adapter that runs the group's job, or why no single adapter can.

    Every effect member must resolve through one adapter, and that adapter must render groups; a
    delay member needs no adapter, since its line travels in the job as a declared number. A
    group of delays alone has no adapter to run it.
    """
    runners = [found.adapter for found in resolved.values()]
    if not runners:
        return RenderFailure(
            RenderFailureStatus.LOAD_FAILED,
            "the cycle group holds only delay nodes; a loop needs an effect whose adapter has a "
            "block pass to run it",
        )
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
    run: _RenderRun, unit: GroupUnit, runner: GroupEffect
) -> tuple[dict[NodeName, Audio], RenderFailure | None]:
    """Build the group's request, run the adapter's one job, and read the outputs back.

    The request's member inputs are summed and scaled by their wires' gains as for any node, and
    the outputs are read while the job's temporary directory still holds them; the core alone
    makes them `Audio` and keeps them.
    """
    frames = unit.input.frames
    members: list[EffectMember | DelayMember] = []
    try:
        for member in unit.input.members:
            feeds = [feed for feed in unit.input.inputs if feed.member == member.name]
            audio = _sum_audio(
                run, [feed.ref for feed in feeds if feed.kind == "audio"], frames, member.name
            )
            controls = [feed.ref for feed in feeds if feed.kind == "control"]
            control = _read_stem(run, controls[0], frames) if controls else None
            match member:
                case EffectMemberInput():
                    recipe = unit.resolved[member.name].resolved
                    members.append(EffectMember(member.name, recipe, audio, control))
                case DelayMemberInput():
                    members.append(DelayMember(member.name, member.delay_samples, audio, control))
    except AudioFileError as error:
        return {}, RenderFailure(
            RenderFailureStatus.CRASHED, f"upstream wrote unusable audio: {error}"
        )
    with tempfile.TemporaryDirectory() as directory:
        request = GroupRenderRequest(
            frames=frames,
            members=tuple(members),
            wires=unit.input.wires,
            outputs=tuple((name, Path(directory) / f"{name}.npy") for name in unit.nodes),
        )
        failure = runner.render_group(request, run.timeout_s)
        if failure is not None:
            return {}, failure
        try:
            outputs = {
                name: run.audio_files.read(Path(directory) / f"{name}.npy", frames)
                for name in unit.nodes
            }
        except AudioFileError as error:
            return {}, RenderFailure(
                RenderFailureStatus.CRASHED, f"the group's worker wrote unusable audio: {error}"
            )
    return outputs, None


def _read_stem(run: _RenderRun, ref: StemRef, frames: int) -> Audio:
    """Return the upstream stem `ref` names, which this run published or found.

    Units render after their upstream units, so the upstream unit exists; a missing one is a bug.
    """
    stored = run.store.find(ref.unit_key)
    if stored is None:
        raise RuntimeError(f"upstream unit {ref.unit_key} is not in the store")
    return run.audio_files.read(stored.stem_path(ref.index), frames)


def _sum_audio(run: _RenderRun, refs: Sequence[StemRef], frames: int, name: str) -> Audio:
    """Return the upstream stems scaled by their wires' gains and summed.

    The sum accumulates in float64 and rounds to stem format once, as the mixer's does, and a sum
    that overflows to a non-finite value raises ValueError instead of clipping, as the mixer's
    law has it.
    """
    total = np.zeros((NUM_CHANNELS, frames), dtype=np.float64)
    # A value beyond range becomes infinite, or not a number when infinities cancel, which numpy
    # reports as warnings; the check below reports it instead.
    with np.errstate(over="ignore", invalid="ignore"):
        for ref in refs:
            total += _read_stem(run, ref, frames).samples * 10 ** (ref.gain_db / 20)
        summed = total.astype(np.float32)
    if not np.isfinite(summed).all():
        raise ValueError(
            f"node {name!r}'s inputs are not finite as a sum: a sum exceeds the float32 range"
        )
    return Audio(summed)


def _render_delay_node(run: _RenderRun, unit: DelayUnit) -> Audio | _Failed:
    """Render a delay node off a cycle: the pure delay equation on its summed inputs.

    A delay off any cycle is a straight-line node, so the equation runs here on numpy, not in a
    worker: out[t] = in[t - delay_samples], silence before the line fills, no dry term, and every
    input sample moved exactly.
    """
    try:
        audio = _sum_audio(run, unit.input.inputs, unit.input.frames, unit.name)
    except AudioFileError as error:
        return _Failed(RenderFailureStatus.CRASHED, f"upstream wrote unusable audio: {error}")
    empty = np.zeros((NUM_CHANNELS, 0), dtype=np.float32)
    return Audio(delay_line(empty, audio.samples, unit.input.delay_samples))


def _render_track_node(run: _RenderRun, unit: TrackUnit) -> Audio | _Failed:
    """Render a track node: the instrument plays the unit's performance into raw audio.

    A render whose sound begins late after its first note (`late_start_s`) is tried again, up to
    RENDER_ATTEMPTS times, each attempt under its own timeout. A failed attempt is not retried. When
    every attempt starts late by the same amount, the late start is the sound's own and the last
    render is kept; otherwise the track fails as `late_start`.
    """
    adapter, sound = unit.resolved.adapter, unit.resolved.resolved
    name, performance, notify = unit.name, unit.input.performance, run.notify
    with tempfile.TemporaryDirectory() as directory:
        attempt = 1
        lateness: list[float] = []
        while True:
            path = Path(directory) / f"audio-{attempt}.npy"
            request = RenderRequest(sound, performance, path, attempt=attempt)
            failure = adapter.render(request, run.timeout_s)
            if isinstance(failure, RenderFailure):
                return _Failed(failure.status, failure.detail)
            audio = read_raw_audio(path, performance.samples)
            if isinstance(audio, AudioProblem):
                return _Failed(
                    RenderFailureStatus.CRASHED,
                    f"instrument wrote unusable audio: {audio.message}",
                )
            late_s = late_start_s(audio.samples, performance)
            if late_s is None:
                return audio
            lateness.append(late_s)
            if attempt == RENDER_ATTEMPTS:
                # A loading race varies between renders; a start that never moves is the sound's.
                if len(set(lateness)) == 1:
                    notify(
                        f"{name}: sound began {late_s:.2f} s after its first note in all "
                        f"{RENDER_ATTEMPTS} renders; keeping it as the sound's own start"
                    )
                    return audio
                return _Failed(
                    RenderFailureStatus.LATE_START,
                    (
                        f"sound began late after its first note in all {RENDER_ATTEMPTS} renders, "
                        f"not all by the same amount; in the last, {late_s:.2f} s after it"
                    ),
                )
            attempt += 1
            notify(
                f"{name}: sound began {late_s:.2f} s after its first note; rendering again "
                f"(attempt {attempt} of {RENDER_ATTEMPTS})"
            )


def _render_effect_node(run: _RenderRun, unit: EffectUnit) -> Audio | _Failed:
    """Render an effect node: the recipe processes the unit's upstream stems into raw audio.

    The audio inputs are summed and scaled by their wires' gains before the call, and a control
    wire's stem travels with the job for the recipe to steer by, its frames matching the audio's.
    """
    adapter, recipe = unit.resolved.adapter, unit.resolved.resolved
    name, frames = unit.name, unit.input.frames
    try:
        audio = _sum_audio(run, unit.input.inputs, frames, name)
        control = (
            None if unit.input.control is None else _read_stem(run, unit.input.control, frames)
        )
    except AudioFileError as error:
        return _Failed(RenderFailureStatus.CRASHED, f"upstream wrote unusable audio: {error}")
    with tempfile.TemporaryDirectory() as directory:
        request = EffectRenderRequest(audio, recipe, Path(directory) / "audio.npy", control=control)
        failure = adapter.render(request, run.timeout_s)
        if isinstance(failure, RenderFailure):
            return _Failed(failure.status, failure.detail)
        rendered = read_raw_audio(request.audio_path, frames)
    if isinstance(rendered, AudioProblem):
        return _Failed(
            RenderFailureStatus.CRASHED,
            f"effect wrote unusable audio: {rendered.message}",
        )
    return rendered
