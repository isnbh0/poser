"""Run plugin code in a subprocess, so a crash or hang cannot stop work on anything else."""

import json
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TypeGuard

import mido

from poser.core.audio import SAMPLE_RATE
from poser.core.jsonvalue import json_object
from poser.core.performance import Control, NoteOff, NoteOn, Performance


class MalformedJobError(ValueError):
    """A worker's job file does not hold the job the parent writes. The parent writes every job
    itself, so this is a bug, and the worker fails loudly."""

    def __init__(self, what: str, value: object) -> None:
        super().__init__(f"malformed {what}: {value!r}")


def job_fields(value: object, keys: set[str], what: str) -> Mapping[str, object]:
    """Return a job's JSON object, raising MalformedJobError unless it has exactly `keys`."""
    fields = json_object(value)
    if fields is None or set(fields) != keys:
        raise MalformedJobError(what, value)
    return fields


def optional_str(value: object) -> TypeGuard[str | None]:
    return value is None or isinstance(value, str)


class WorkerTimedOutError(Exception):
    """The worker exceeded its timeout."""


class WorkerCrashedError(Exception):
    """The worker exited nonzero or wrote no readable result."""


def run_isolated(command: list[str], timeout_s: float | None) -> object:
    """Run a worker, appending its result path as the last argument, and return its JSON result.

    `timeout_s=None` waits for as long as the worker runs, as when it shows a plugin window.

    The result goes to a file because plugins may print to stdout. Stdout and stderr avoid pipes,
    so a helper process left running by a plugin cannot hold them open and make a finished worker
    look hung.
    """
    with tempfile.TemporaryDirectory() as directory:
        result_path = Path(directory) / "result.json"
        stderr_path = Path(directory) / "stderr.txt"
        try:
            with stderr_path.open("wb") as stderr:
                completed = subprocess.run(
                    [*command, str(result_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    timeout=timeout_s,
                    check=False,
                )
        except subprocess.TimeoutExpired as error:
            raise WorkerTimedOutError(f"worker exceeded {timeout_s:g} seconds") from error

        if completed.returncode != 0:
            lines = stderr_path.read_text(errors="replace").strip().splitlines()
            last_line = lines[-1] if lines else "no error output"
            raise WorkerCrashedError(f"exit code {completed.returncode}: {last_line}")

        try:
            return json.loads(result_path.read_text())
        except (OSError, ValueError) as error:
            raise WorkerCrashedError("worker wrote no readable result") from error


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
