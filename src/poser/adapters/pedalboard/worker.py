"""The Pedalboard worker: the functions `run_isolated` calls in a child process.

This is the only module under `poser.adapters` that imports `pedalboard` plugin code or `mido`.
It imports `pedalboard` inside the functions, so a parent that imports this module to name a
target loads no plugin code. Every performance renders through `render_fresh`, on a plugin it has
just loaded.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

import mido
import numpy as np

from poser.adapters.isolation import MalformedJobError, job_fields, optional_str
from poser.adapters.pedalboard.config import INITIALIZATION_TIMEOUT_S
from poser.adapters.pedalboard.state import decode_state, encode_state
from poser.core.audio import (
    NUM_CHANNELS,
    SAMPLE_RATE,
    AudioProblem,
    read_raw_audio,
    write_raw_audio,
)
from poser.core.graph import BUFFER_SIZE, instantaneous_order
from poser.core.jsonvalue import JsonValue
from poser.core.performance import Control, NoteOff, NoteOn, Performance, performance_from_data
from poser.core.rendering import delay_line, delay_signal
from poser.core.rules import is_int, is_number

if TYPE_CHECKING:
    from pedalboard import AudioUnitPlugin, VST3Plugin
    from pedalboard_native import MIDIMessageLike

# Serum applies a controller change at the start of the processing block that contains it, so
# the block size bounds controller timing. The size itself is `BUFFER_SIZE` in `poser.core.graph`,
# where the feedback regime's one-block bound on in-cycle delays is enforced next to it.
SETTLE_S = 0.1

RENDER_KEYS = {"path", "plugin_name", "state", "performance", "audio_path"}
CAPTURE_KEYS = {"path", "plugin_name", "state", "performance", "preview_path"}
EFFECT_KEYS = {
    "path",
    "builtin",
    "parameters",
    "frames",
    "audio_path",
    "control_path",
    "output_path",
}

# Poser's own effects, which need no plugin and hold no state: a bus's passthrough summer, and
# the sidechain duck, the one effect that takes a control wire.
SUM = "sum"
DUCK = "duck"
# The duck's ballistics, as declared in a recipe's `parameters`: the follower rises to a louder
# control level over `attack_ms` and falls from a quieter one over `release_ms`, and at a
# full-scale control level the gain falls by exactly `amount_db`, in proportion below it.
DUCK_DEFAULTS: Mapping[str, float] = MappingProxyType(
    {"attack_ms": 10.0, "release_ms": 150.0, "amount_db": 6.0}
)


def performance_messages(performance: Performance) -> list[mido.Message]:
    """Return a performance as MIDI messages for Pedalboard, in the performance's order.

    Pedalboard reads `time` as absolute seconds, not mido deltas, converts it to a 32-bit float,
    and truncates `time * sample_rate` to a sample, so each time sits half a sample past its
    sample. That lands exactly for the first 128 seconds; later, float32 rounding can move a
    message by a sample or more, without reordering messages.
    """
    messages = []
    for event in performance.events:
        match event:
            case NoteOff(sample, pitch):
                message = mido.Message("note_off", note=pitch)
            case Control(sample, control, value):
                message = mido.Message("control_change", control=control, value=value)
            case NoteOn(sample, pitch, velocity):
                message = mido.Message("note_on", note=pitch, velocity=velocity)
        messages.append(message.copy(time=(sample + 0.5) / SAMPLE_RATE))
    return messages


def render_duration_s(samples: int) -> float:
    """Return the duration to ask Pedalboard for, which truncates `duration * sample_rate`, so
    that it renders exactly `samples` frames."""
    return (samples + 0.5) / SAMPLE_RATE


def _parse(
    value: JsonValue, keys: set[str], output: str, what: str
) -> tuple[Mapping[str, object], str | None, bytes | None, Performance, Path]:
    """Return a job's fields, plugin name, state, performance, and output path."""
    fields = job_fields(value, keys, what)
    path, plugin_name, state = fields["path"], fields["plugin_name"], fields["state"]
    target = fields[output]
    if not (isinstance(path, str) and optional_str(plugin_name) and optional_str(state)):
        raise MalformedJobError(what, value)
    if not isinstance(target, str):
        raise MalformedJobError(what, value)
    try:
        performance = performance_from_data(fields["performance"])
        raw_state = None if state is None else decode_state(state)
    except ValueError as error:
        raise MalformedJobError(what, value) from error
    return fields, plugin_name, raw_state, performance, Path(target)


def load_external(path: str, plugin_name: str | None) -> "VST3Plugin | AudioUnitPlugin":
    """Load the plugin at `path` with the class its suffix names, so a failure reports only that
    format's error. A path with another suffix tries every format. Raises whatever the plugin
    code raises."""
    # Imported here so the parent never loads plugin code, and per branch because pedalboard
    # defines `AudioUnitPlugin` only on macOS.
    suffix = Path(path).suffix.lower()
    if suffix == ".vst3":
        from pedalboard import VST3Plugin

        return VST3Plugin(
            path, plugin_name=plugin_name, initialization_timeout=INITIALIZATION_TIMEOUT_S
        )
    if suffix == ".component":
        from pedalboard import AudioUnitPlugin

        return AudioUnitPlugin(
            path, plugin_name=plugin_name, initialization_timeout=INITIALIZATION_TIMEOUT_S
        )
    from pedalboard import load_plugin

    # `load_plugin` is annotated as returning `ExternalPlugin`, which lacks `raw_state`, but it
    # returns one of these two classes.
    return cast(
        "VST3Plugin | AudioUnitPlugin",
        load_plugin(path, plugin_name=plugin_name, initialization_timeout=INITIALIZATION_TIMEOUT_S),
    )


def load(path: str, plugin_name: str | None, state: bytes | None) -> "VST3Plugin | AudioUnitPlugin":
    """Load a plugin and restore a state into it. Raises whatever the plugin code raises."""
    plugin = load_external(path, plugin_name)
    if state is not None:
        plugin.raw_state = state
    return plugin


@dataclass(frozen=True)
class NotLoaded:
    """A plugin that could not be loaded as an instrument with its state, and why."""

    detail: str


class LoadedEffect(Protocol):
    """What an effect plugin offers: its role, and processing that returns what it was given."""

    @property
    def is_instrument(self) -> bool: ...

    def __call__(self, audio: np.ndarray, sample_rate: int, **kwargs: int) -> np.ndarray: ...


def duck_parameters(parameters: Mapping[str, JsonValue]) -> dict[str, float]:
    """Return the duck's ballistics, filling its declared defaults and refusing anything else."""
    if unknown := sorted(set(parameters) - set(DUCK_DEFAULTS)):
        raise ValueError(f"duck takes only the parameters {sorted(DUCK_DEFAULTS)}, got {unknown}")
    values: dict[str, float] = {}
    for name, default in DUCK_DEFAULTS.items():
        value = parameters.get(name, default)
        if not is_number(value):
            raise ValueError(f"duck's {name} must be a number, got {value!r}")
        number = float(value)
        if number < 0 or (name != "amount_db" and number == 0):
            kind = "non-negative decibels" if name == "amount_db" else "milliseconds above zero"
            raise ValueError(f"duck's {name} must be {kind}, got {value!r}")
        values[name] = number
    return values


def follower(level: np.ndarray, attack_samples: float, release_samples: float) -> np.ndarray:
    """Follow `level` sample by sample: rising to a louder level over the attack, falling to a
    quieter one over the release.

    Each sample is the lower of two bounds on the past: an attack bound that rises linearly to
    every level it has seen, and a release bound that decays exponentially from every level it
    has seen. Both bounds are one running accumulate, so the follower is deterministic at any
    length and identical for equal control signals.
    """
    index = np.arange(level.size, dtype=np.float64)
    rise = 1.0 / attack_samples  # Linear rise: full scale in attack_samples samples.
    attack = np.minimum.accumulate(level - index * rise) + index * rise
    decay = 1.0 / release_samples  # Exponential fall, by one e-fold in release_samples samples.
    logged = np.maximum.accumulate(np.log(np.maximum(level, 1e-12)) + index * decay)
    return np.minimum(attack, np.exp(logged - index * decay))


def render_duck(
    audio: np.ndarray, control: np.ndarray | None, parameters: Mapping[str, float]
) -> np.ndarray:
    """Pull the audio down while the control signal is loud, by the declared ballistics.

    The control's level, as the mean of its channels, is followed with the declared attack and
    release; a full-scale control level pulls the gain down by exactly amount_db, in proportion
    below it. With no control wire nothing steers the follower, and the output is the input.
    """
    if control is None:
        return audio
    level = np.abs(control.mean(axis=0, dtype=np.float64))
    seconds = 1000.0 / SAMPLE_RATE
    pulled = follower(
        level,
        parameters["attack_ms"] / seconds,
        parameters["release_ms"] / seconds,
    )
    gain = 10.0 ** (-parameters["amount_db"] * np.clip(pulled, 0.0, 1.0) / 20.0)
    return cast("np.ndarray", (audio * gain[np.newaxis, :]).astype(np.float32))


def load_effect(
    path: str | None, builtin: str | None, parameters: Mapping[str, JsonValue]
) -> LoadedEffect:
    """Load an effect: a plugin from `path`, or the pedalboard-native class `builtin` names.

    Raises whatever the plugin code raises, including AttributeError for an unknown class name.
    """
    if path is not None:
        return cast("LoadedEffect", load_external(path, None))
    # The builtin's name was checked at build time, but the class exists only where the native
    # library does, so an unknown name is reported here, as a crashed worker.
    import pedalboard_native

    return getattr(pedalboard_native, cast("str", builtin))(**dict(parameters))


def render_effect_fresh(
    path: str | None,
    builtin: str | None,
    parameters: Mapping[str, JsonValue],
    audio: np.ndarray,
    control: np.ndarray | None = None,
) -> np.ndarray | NotLoaded:
    """Load the effect on a new instance and process `audio` through it, with the control edge's
    samples when the node was fed a control wire.

    Poser's own effects load nothing and consume the control edge here; every other recipe takes
    no control edge, since only `duck` has a sidechain input poser can promise. An exception
    raised while processing escapes; the frame-count law is asserted below.
    """
    where = path if path is not None else cast("str", builtin)
    if builtin == SUM:
        return audio
    if builtin == DUCK:
        try:
            return render_duck(audio, control, duck_parameters(parameters))
        except Exception as error:  # noqa: BLE001
            return NotLoaded(str(error))
    if control is not None:
        return NotLoaded(f"{where} takes no control wire: it has no sidechain input")
    try:
        plugin = load_effect(path, builtin, parameters)
    except Exception as error:  # noqa: BLE001
        return NotLoaded(str(error))
    if plugin.is_instrument:
        return NotLoaded(f"{where} is not an effect")
    return plugin(audio, SAMPLE_RATE, buffer_size=BUFFER_SIZE)


def render_effect(job: JsonValue) -> JsonValue:
    """Render audio through an effect and write it to `output_path` with write_raw_audio.

    The audio is written unchecked; the core checks it. The control edge's audio, when the job
    carries one, must hold exactly `frames` frames, as the input must. The frame-count law is
    asserted here: an effect returns exactly the frames it was given, and one that does not is a
    crashed worker.
    """
    fields = job_fields(job, EFFECT_KEYS, "effect render job")
    path, builtin, parameters = fields["path"], fields["builtin"], fields["parameters"]
    frames, audio_path, output_path = fields["frames"], fields["audio_path"], fields["output_path"]
    control_path = fields["control_path"]
    if not (
        optional_str(path)
        and optional_str(builtin)
        and (path is None) != (builtin is None)
        and isinstance(parameters, dict)
        and is_int(frames)
        and frames >= 1
        and isinstance(audio_path, str)
        and isinstance(output_path, str)
        and optional_str(control_path)
    ):
        raise MalformedJobError("effect render job", job)
    problem = read_raw_audio(Path(audio_path), frames)
    if isinstance(problem, AudioProblem):
        # A wrong-type input crashes the worker, like every rule the input breaks.
        raise ValueError(f"the effect's input is unusable: {problem.message}")  # noqa: TRY004
    control: np.ndarray | None = None
    if control_path is not None:
        read = read_raw_audio(Path(control_path), frames)
        if isinstance(read, AudioProblem):
            raise ValueError(f"the control wire's audio is unusable: {read.message}")
        control = read.samples
    result = render_effect_fresh(
        path,
        builtin,
        cast("Mapping[str, JsonValue]", parameters),
        problem.samples,
        control,
    )
    if isinstance(result, NotLoaded):
        return {"status": "load_failed", "detail": result.detail}
    if not isinstance(result, np.ndarray) or result.shape != problem.samples.shape:
        shape = getattr(result, "shape", type(result).__name__)
        raise ValueError(
            f"the effect returned {shape}, expected {problem.samples.shape}; an effect returns "
            "exactly the frames it is given"
        )
    write_raw_audio(Path(output_path), result)
    return {"status": "ok"}


# A cycle group's block pass (C22): one job per group, fixed BUFFER_SIZE blocks, previous-block
# feedback. The buffers live here, in the job, never in the render store.
GROUP_KEYS = {"frames", "members", "wires"}
GROUP_MEMBER_KEYS = {
    "name",
    "recipe",
    "delay_samples",
    "input_path",
    "control_path",
    "output_path",
}


@dataclass
class GroupMemberJob:
    """One cycle-group member the block pass runs, with its buffers kept through the pass."""

    name: str
    recipe: Mapping[str, JsonValue] | None  # path/builtin/parameters, or None for a delay.
    delay_samples: int | None  # None exactly when the member is an effect.
    input_path: Path
    control_path: Path | None
    output_path: Path
    input: np.ndarray | None = None  # (2, frames) The member's summed external audio.
    control: np.ndarray | None = None  # The member's external control edge, whole horizon.
    buffer: np.ndarray | None = None  # (2, frames) The member's output so far.
    line: np.ndarray | None = None  # (2, frames) A delay member's input line, by sample index.
    plugin: LoadedEffect | None = None  # The recipe's one loaded instance, for an effect member.


def _group_members(value: object, what: str, job: JsonValue) -> dict[str, GroupMemberJob]:
    """Read a group job's members, each naming exactly one of a recipe and a delay."""
    if not isinstance(value, list) or not value:
        raise MalformedJobError(what, job)
    members: dict[str, GroupMemberJob] = {}
    for entry in value:
        fields = job_fields(entry, GROUP_MEMBER_KEYS, what)
        name, recipe, delay_samples = fields["name"], fields["recipe"], fields["delay_samples"]
        input_path, control_path, output_path = (
            fields["input_path"],
            fields["control_path"],
            fields["output_path"],
        )
        is_effect = isinstance(recipe, dict)
        is_delay = is_int(delay_samples) and delay_samples >= 1
        if not (
            isinstance(name, str)
            and name
            and name not in members
            and is_effect != is_delay
            and optional_str(control_path)
            and isinstance(input_path, str)
            and isinstance(output_path, str)
        ):
            raise MalformedJobError(what, job)
        members[name] = GroupMemberJob(
            name=name,
            recipe=cast("Mapping[str, JsonValue] | None", recipe) if is_effect else None,
            delay_samples=delay_samples if is_delay else None,
            input_path=Path(input_path),
            control_path=None if control_path is None else Path(control_path),
            output_path=Path(output_path),
        )
    return members


def _group_wires(
    value: object, names: set[str], what: str, job: JsonValue
) -> list[tuple[str, str, str, float]]:
    """Read a group job's inside wires, each [from, to, kind, gain] between two members."""
    if not isinstance(value, list):
        raise MalformedJobError(what, job)
    wires: list[tuple[str, str, str, float]] = []
    for entry in value:
        if not isinstance(entry, list) or len(entry) != 4:
            raise MalformedJobError(what, job)
        source, target, kind, gain = entry
        if not (
            isinstance(source, str)
            and isinstance(target, str)
            and source in names
            and target in names
            and kind in ("audio", "control")
            and is_number(gain)
        ):
            raise MalformedJobError(what, job)
        wires.append((source, target, str(kind), float(gain)))
    return wires


def _load_group_member(member: GroupMemberJob) -> str | None:
    """Load the member's recipe once and read its external control, or say why it cannot.

    Every recipe loads here, once for the whole pass, so its state carries between blocks. Only
    the duck has a sidechain input poser can promise, so any other member fed a control edge,
    from outside the group or across it, is refused by name.
    """
    where = (
        None
        if member.recipe is None
        else (
            member.recipe["path"] if member.recipe["path"] is not None else member.recipe["builtin"]
        )
    )
    if member.control_path is not None:
        frames = member.input.shape[1] if member.input is not None else 0
        read = read_raw_audio(member.control_path, frames)
        if isinstance(read, AudioProblem):
            return f"the control wire's audio is unusable: {read.message}"
        member.control = read.samples
    if member.recipe is None:
        return None
    builtin = member.recipe.get("builtin")
    if member.control is not None and builtin != DUCK:
        return f"{where} takes no control wire: it has no sidechain input"
    if builtin == SUM:
        return None
    if builtin == DUCK:
        try:
            duck_parameters(cast("Mapping[str, JsonValue]", member.recipe["parameters"]))
        except Exception as error:  # noqa: BLE001
            return str(error)
        return None
    try:
        plugin = load_effect(
            cast("str | None", member.recipe.get("path")),
            cast("str | None", builtin),
            cast("Mapping[str, JsonValue]", member.recipe["parameters"]),
        )
    except Exception as error:  # noqa: BLE001
        return str(error)
    if plugin.is_instrument:
        return f"{where} is not an effect"
    member.plugin = plugin
    return None


def _group_effect_block(
    member: GroupMemberJob, block: np.ndarray, control: np.ndarray | None
) -> np.ndarray:
    """Process one block through the member's already-loaded recipe.

    A plugin is called per block, so its state carries between blocks and the loop's granularity
    is one block; the duck's follower therefore restarts each block, which the one-block bound on
    in-cycle delays declares. The frame-count law is asserted here as everywhere.
    """
    recipe = cast("Mapping[str, JsonValue]", member.recipe)
    builtin = recipe.get("builtin")
    if builtin == SUM:
        return block
    if builtin == DUCK:
        return render_duck(
            block, control, duck_parameters(cast("Mapping[str, JsonValue]", recipe["parameters"]))
        )
    out = cast("LoadedEffect", member.plugin)(block, SAMPLE_RATE, buffer_size=BUFFER_SIZE)
    if not isinstance(out, np.ndarray) or out.shape != block.shape:
        shape = getattr(out, "shape", type(out).__name__)
        raise ValueError(
            f"the effect returned {shape}, expected {block.shape}; an effect returns exactly "
            "the frames it is given"
        )
    return out


def render_group_job(job: JsonValue) -> JsonValue:
    """Render one cycle group as a block pass and write every member's output.

    One job per group: each recipe loads once, then the horizon steps through fixed BUFFER_SIZE
    blocks in the instantaneous order, which drops every wire leaving a delay member. Within a
    block each member reads its summed inputs, rounds them to float32 once, and writes its output
    buffer. A wire out of a delay member reads that delay's output signal, its input line read
    delay_samples earlier: silence before the line fills, no dry term, and for an in-cycle delay
    only frames earlier blocks wrote. The frame-count law holds by construction: every member's
    output is exactly `frames` frames.
    """
    what = "group render job"
    fields = job_fields(job, GROUP_KEYS, what)
    frames = fields["frames"]
    if not is_int(frames) or frames < 1:
        raise MalformedJobError(what, job)
    members = _group_members(fields["members"], what, job)
    wires = _group_wires(fields["wires"], set(members), what, job)
    for member in members.values():
        read = read_raw_audio(member.input_path, frames)
        if isinstance(read, AudioProblem):
            # A wrong-type input crashes the worker, like every rule the input breaks.
            raise ValueError(f"the group member's input is unusable: {read.message}")  # noqa: TRY004
        member.input = read.samples.astype(np.float32, copy=True)
        member.buffer = np.zeros_like(member.input)
        if member.delay_samples is not None:
            member.line = np.zeros_like(member.input)
    for member in members.values():
        if problem := _load_group_member(member):
            return {"status": "load_failed", "detail": f"member {member.name!r}: {problem}"}
    delayed = {name for name, member in members.items() if member.line is not None}
    order = instantaneous_order(
        list(members),
        sorted(delayed),
        [(source, target) for source, target, _, _ in wires],
    )
    for start in range(0, frames, BUFFER_SIZE):
        end = min(start + BUFFER_SIZE, frames)
        for name in order:
            member = members[name]
            block = cast("np.ndarray", member.input)[:, start:end].astype(np.float64)
            control = None if member.control is None else member.control[:, start:end]
            for source, target, kind, gain in wires:
                if target != name:
                    continue
                if source in delayed:
                    sample = delay_signal(
                        cast("np.ndarray", members[source].line),
                        start,
                        end - start,
                        cast("int", members[source].delay_samples),
                    )
                else:
                    sample = cast("np.ndarray", members[source].buffer)[:, start:end]
                if kind == "audio":
                    block += sample.astype(np.float64) * 10 ** (gain / 20)
                else:
                    control = sample
            stepped = block.astype(np.float32)
            if member.line is not None:
                member.line[:, start:end] = stepped
                out = delay_line(member.line[:, :start], stepped, cast("int", member.delay_samples))
            else:
                out = _group_effect_block(member, stepped, control)
            cast("np.ndarray", member.buffer)[:, start:end] = out
    for member in members.values():
        write_raw_audio(member.output_path, cast("np.ndarray", member.buffer))
    return {"status": "ok"}


def render_fresh(
    path: str, plugin_name: str | None, state: bytes | None, performance: Performance
) -> np.ndarray | NotLoaded:
    """Load a plugin, restore a state into it, and render a performance on that new instance.

    Returns what Pedalboard returns, unchecked. An exception raised while processing escapes.
    """
    # Any exception from loading third-party plugin code, or from it rejecting a saved state, is
    # the result being reported.
    try:
        plugin = load(path, plugin_name, state)
    except Exception as error:  # noqa: BLE001
        return NotLoaded(str(error))
    if not plugin.is_instrument:
        return NotLoaded(f"{path} is not an instrument")
    # The plugin was loaded for this render and has processed nothing, so Pedalboard's reset (save
    # the state, recreate the plugin, restore the state) has nothing to clear. Its rebuild also
    # plays a note no event asked for, which a long amp release or an effect tail carries into the
    # render.
    # Pedalboard's stubs take a `list` of its message union, and `list` is invariant.
    messages: list[MIDIMessageLike] = [*performance_messages(performance)]
    return plugin(
        messages,
        duration=render_duration_s(performance.samples),
        sample_rate=SAMPLE_RATE,
        num_channels=NUM_CHANNELS,
        buffer_size=BUFFER_SIZE,
        reset=False,
    )


def render_job(job: JsonValue) -> JsonValue:
    """Render a performance on a sound and write it to `audio_path` with write_raw_audio.

    The audio is written unchecked; the core checks it.
    """
    fields, plugin_name, state, performance, audio_path = _parse(
        job, RENDER_KEYS, "audio_path", "render job"
    )
    path = cast("str", fields["path"])  # `_parse` checked it.
    result = render_fresh(path, plugin_name, state, performance)
    if isinstance(result, NotLoaded):
        return {"status": "load_failed", "detail": result.detail}
    write_raw_audio(audio_path, result)
    return {"status": "ok"}


def capture_state(plugin: "VST3Plugin | AudioUnitPlugin") -> bytes:
    """Return a plugin's `raw_state` after a short render with no notes.

    Serum's `raw_state`, read straight after a parameter change, restores the earlier sound. After
    the plugin processes any audio, it restores the change. `reset=False` keeps the instance the
    owner edited, because Pedalboard's reset recreates an instrument before rendering.
    """
    plugin([], duration=SETTLE_S, sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS, reset=False)
    return bytes(plugin.raw_state)


def capture_job(job: JsonValue) -> JsonValue:
    """Show the plugin's window, then render a preview from the state it leaves.

    Returns `{"state": base64}`, or `{"error": text}` when the plugin or its state fails.
    """
    fields, plugin_name, start, performance, preview_path = _parse(
        job, CAPTURE_KEYS, "preview_path", "capture job"
    )
    path = cast("str", fields["path"])  # `_parse` checked it.
    try:
        plugin = load(path, plugin_name, start)
    except Exception as error:  # noqa: BLE001
        return {"error": f"cannot load the plugin: {error}"}
    if not plugin.is_instrument:
        return {"error": f"{path} is not an instrument"}

    plugin.show_editor()  # Blocks until the owner closes the window.
    state = capture_state(plugin)
    # Release the edited instance, so only the preview's instance is alive while it renders.
    del plugin

    # A newly loaded plugin proves the captured state restores before the sound is kept.
    result = render_fresh(path, plugin_name, state, performance)
    if isinstance(result, NotLoaded):
        return {"error": f"cannot restore the captured state: {result.detail}"}
    write_raw_audio(preview_path, result)
    return {"state": encode_state(state)}
