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
from poser.core.jsonvalue import JsonValue
from poser.core.performance import Control, NoteOff, NoteOn, Performance, performance_from_data
from poser.core.rules import is_int, is_number

if TYPE_CHECKING:
    from pedalboard import AudioUnitPlugin, VST3Plugin
    from pedalboard_native import MIDIMessageLike

# Serum applies a controller change at the start of the processing block that contains it, so
# the block size bounds controller timing. 512 samples is 10.7 ms at 48 kHz.
BUFFER_SIZE = 512
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


def load(path: str, plugin_name: str | None, state: bytes | None) -> "VST3Plugin | AudioUnitPlugin":
    """Load a plugin and restore a state into it. Raises whatever the plugin code raises."""
    # Imported here so the parent never loads plugin code.
    from pedalboard import load_plugin

    # `load_plugin` is annotated as returning `ExternalPlugin`, which lacks `raw_state`, but it
    # returns one of these two classes.
    plugin = cast(
        "VST3Plugin | AudioUnitPlugin",
        load_plugin(path, plugin_name=plugin_name, initialization_timeout=INITIALIZATION_TIMEOUT_S),
    )
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
        # Imported here so the parent never loads plugin code.
        from pedalboard import load_plugin

        return cast(
            "LoadedEffect", load_plugin(path, initialization_timeout=INITIALIZATION_TIMEOUT_S)
        )
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
