"""The Pedalboard worker: the functions `run_isolated` calls in a child process.

This is the only module under `poser.adapters` that imports `pedalboard` plugin code or `mido`.
It imports `pedalboard` inside the functions, so a parent that imports this module to name a
target loads no plugin code.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

import mido
import numpy as np

from poser.adapters.isolation import MalformedJobError, job_fields, optional_str
from poser.adapters.pedalboard.config import INITIALIZATION_TIMEOUT_S
from poser.adapters.pedalboard.state import decode_state, encode_state
from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE, write_raw_audio
from poser.core.jsonvalue import JsonValue
from poser.core.performance import Control, NoteOff, NoteOn, Performance, performance_from_data

if TYPE_CHECKING:
    from pedalboard import AudioUnitPlugin, VST3Plugin
    from pedalboard_native import MIDIMessageLike

# Serum applies a controller change at the start of the processing block that contains it, so
# the block size bounds controller timing. 512 samples is 10.7 ms at 48 kHz.
BUFFER_SIZE = 512
SETTLE_S = 0.1

RENDER_KEYS = {"path", "plugin_name", "state", "performance", "audio_path"}
CAPTURE_KEYS = {"path", "plugin_name", "state", "performance", "preview_path"}


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


def play(plugin: "VST3Plugin | AudioUnitPlugin", performance: Performance) -> np.ndarray:
    """Render a performance on a loaded plugin, returning what Pedalboard returns."""
    # Pedalboard's reset rebuilds an instrument by saving its state, recreating it, and restoring
    # that state, so a restored sound survives. Serum renders differently without the reset.
    # Pedalboard's stubs take a `list` of its message union, and `list` is invariant.
    messages: list[MIDIMessageLike] = [*performance_messages(performance)]
    return plugin(
        messages,
        duration=render_duration_s(performance.samples),
        sample_rate=SAMPLE_RATE,
        num_channels=NUM_CHANNELS,
        buffer_size=BUFFER_SIZE,
        reset=True,
    )


def render_job(job: JsonValue) -> JsonValue:
    """Render a performance on a sound and write it to `audio_path` with write_raw_audio.

    The audio is written unchecked; the core checks it.
    """
    fields, plugin_name, state, performance, audio_path = _parse(
        job, RENDER_KEYS, "audio_path", "render job"
    )
    path = cast("str", fields["path"])  # `_parse` checked it.
    # Any exception from loading third-party plugin code, or from it rejecting a saved state, is
    # the result being reported.
    try:
        plugin = load(path, plugin_name, state)
    except Exception as error:  # noqa: BLE001
        return {"status": "load_failed", "detail": str(error)}
    if not plugin.is_instrument:
        return {"status": "load_failed", "detail": f"{path} is not an instrument"}
    write_raw_audio(audio_path, play(plugin, performance))
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

    # A newly loaded plugin proves the captured state restores before the sound is kept.
    try:
        plugin = load(path, plugin_name, state)
    except Exception as error:  # noqa: BLE001
        return {"error": f"cannot restore the captured state: {error}"}
    write_raw_audio(preview_path, play(plugin, performance))
    return {"state": encode_state(state)}
