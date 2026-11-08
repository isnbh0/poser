"""Check that listed VST3 plugins load through Pedalboard and produce sound.

Each plugin runs in its own worker subprocess, so a crash, hang, or license dialog in one plugin
cannot stop the report for the others. `check-plugins` is the parent command. Running this module
with `python -m` is the worker.
"""

import argparse
import json
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

import mido
import numpy as np

from poser.song import is_number
from poser.worker import (
    WorkerCrashedError,
    WorkerTimedOutError,
    json_object,
    run_isolated,
)
from poser.workspace import WorkspaceError, fill_path_options

if TYPE_CHECKING:
    from pedalboard_native import MIDIMessageLike

SAMPLE_RATE = 48000
NUM_CHANNELS = 2
DURATION_S = 5.0
NOTES = (48, 60, 72, 84)
VELOCITY = 100
FIRST_NOTE_S = 0.5
NOTE_SPACING_S = 1.0
NOTE_LENGTH_S = 0.5

SILENCE_DBFS = -60.0
FLOOR_DBFS = -120.0  # Reported for digital silence, because JSON has no -inf.

WORKER_TIMEOUT_S = 120.0
INITIALIZATION_TIMEOUT_S = 30.0

EXIT_ALL_PASS = 0
EXIT_SOME_FAILED = 1
EXIT_BAD_CONFIG = 2


class Status(StrEnum):
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"
    LOAD_FAILED = "load_failed"
    NOT_INSTRUMENT = "not_instrument"
    NON_FINITE = "non_finite"
    SILENT = "silent"
    MISSING_NOTES = "missing_notes"
    PASS = "pass"  # noqa: S105 -- a check status, not a password.


class ConfigError(Exception):
    """The plugin list could not be read."""


@dataclass(frozen=True)
class PluginSpec:
    name: str
    path: str
    plugin_name: str | None = None
    presets: str | None = None  # Directory of the plugin's .fxp presets.


@dataclass(frozen=True)
class Analysis:
    status: Status
    peak_dbfs: float
    note_peaks_dbfs: list[float]
    clipped: bool
    repeatable: bool
    max_difference_dbfs: float


@dataclass(frozen=True)
class PluginResult:
    name: str
    path: str
    status: Status
    is_instrument: bool | None = None
    peak_dbfs: float | None = None
    note_peaks_dbfs: list[float] = field(default_factory=list)
    clipped: bool | None = None
    repeatable: bool | None = None
    max_difference_dbfs: float | None = None
    detail: str | None = None


WORKER_FIELDS = {f.name for f in fields(PluginResult)} - {"name", "path"}


class WorkerResult(TypedDict):
    """What a check worker reports: the fields of `PluginResult` other than name and path."""

    status: Status
    is_instrument: NotRequired[bool | None]
    peak_dbfs: NotRequired[float | None]
    note_peaks_dbfs: NotRequired[list[float]]
    clipped: NotRequired[bool | None]
    repeatable: NotRequired[bool | None]
    max_difference_dbfs: NotRequired[float | None]
    detail: NotRequired[str | None]


def note_pattern() -> list[mido.Message]:
    """Build the test pattern. Pedalboard reads `time` as absolute seconds, not mido deltas."""
    messages = []
    for index, note in enumerate(NOTES):
        start = FIRST_NOTE_S + index * NOTE_SPACING_S
        messages.append(mido.Message("note_on", note=note, velocity=VELOCITY, time=start))
        messages.append(mido.Message("note_off", note=note, time=start + NOTE_LENGTH_S))
    return messages


def _to_dbfs(peak: float) -> float:
    floor = 10.0 ** (FLOOR_DBFS / 20.0)
    return FLOOR_DBFS if peak <= floor else 20.0 * float(np.log10(peak))


def measure_peak_dbfs(samples: np.ndarray) -> float:
    return _to_dbfs(float(np.abs(samples[np.isfinite(samples)]).max(initial=0.0)))


def _note_peaks_dbfs(render: np.ndarray) -> list[float]:
    peaks = []
    for index in range(len(NOTES)):
        start_s = FIRST_NOTE_S + index * NOTE_SPACING_S
        start = round(start_s * SAMPLE_RATE)
        stop = round((start_s + NOTE_LENGTH_S) * SAMPLE_RATE)
        peaks.append(measure_peak_dbfs(render[..., start:stop]))
    return peaks


def analyze(first: np.ndarray, second: np.ndarray) -> Analysis:
    """Measure two channels-first renders of the test pattern.

    Peaks report the quieter of the two renders, so a plugin that goes silent on reload is caught.
    """
    first = np.asarray(first, dtype=np.float64)
    second = np.asarray(second, dtype=np.float64)

    both_finite = np.isfinite(first) & np.isfinite(second)
    difference = np.abs(first[both_finite] - second[both_finite]).max(initial=0.0)

    peak_dbfs = min(measure_peak_dbfs(first), measure_peak_dbfs(second))
    note_peaks_dbfs = [
        min(a, b) for a, b in zip(_note_peaks_dbfs(first), _note_peaks_dbfs(second), strict=True)
    ]

    if not bool(both_finite.all()):
        status = Status.NON_FINITE
    elif peak_dbfs < SILENCE_DBFS:
        status = Status.SILENT
    elif min(note_peaks_dbfs) < SILENCE_DBFS:
        status = Status.MISSING_NOTES
    else:
        status = Status.PASS

    return Analysis(
        status=status,
        peak_dbfs=peak_dbfs,
        note_peaks_dbfs=note_peaks_dbfs,
        clipped=bool((np.abs(first) >= 1.0).any() or (np.abs(second) >= 1.0).any()),
        repeatable=bool(np.array_equal(first, second)),
        max_difference_dbfs=_to_dbfs(float(difference)),
    )


def load_config(path: Path) -> list[PluginSpec]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot read {path}: {error}") from error

    entries = data.get("plugin")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path} has no [[plugin]] entries")

    specs = []
    for entry in entries:
        valid = (
            isinstance(entry, dict)
            and {"name", "path"} <= set(entry) <= {"name", "path", "plugin_name", "presets"}
            and all(isinstance(value, str) and value.strip() for value in entry.values())
        )
        if not valid:
            raise ConfigError(f"{path} has an invalid entry: {entry!r}")
        # The first `:` in a sound name ends the plugin name of a preset.
        if ":" in entry["name"]:
            raise ConfigError(f"{path}: plugin name {entry['name']!r} contains ':'")
        specs.append(
            PluginSpec(entry["name"], entry["path"], entry.get("plugin_name"), entry.get("presets"))
        )
    return specs


def worker_command(spec: PluginSpec) -> list[str]:
    command = [sys.executable, "-m", "poser.plugin_check", spec.path]
    if spec.plugin_name is not None:
        command += ["--plugin-name", spec.plugin_name]
    return command


def _optional_float(value: object) -> tuple[bool, float | None]:
    """Return whether the value is a number or None, and the value as a float or None."""
    if value is None:
        return True, None
    if is_number(value):
        return True, float(value)
    return False, None


def parse_worker_result(payload: object) -> WorkerResult:
    """Turn a check worker's JSON result into a result, reporting anything malformed as a crash."""
    malformed: WorkerResult = {
        "status": Status.CRASHED,
        "detail": "worker wrote a malformed result",
    }
    fields = json_object(payload)
    if fields is None or not set(fields) <= WORKER_FIELDS:
        return malformed
    status = fields.get("status")
    if not isinstance(status, str) or status not in Status:
        return {"status": Status.CRASHED, "detail": "worker wrote an unknown status"}

    result: WorkerResult = {"status": Status(status)}
    for key in ("is_instrument", "clipped", "repeatable"):
        if key in fields:
            flag = fields[key]
            if not (flag is None or isinstance(flag, bool)):
                return malformed
            result[key] = flag
    for key in ("peak_dbfs", "max_difference_dbfs"):
        if key in fields:
            valid, number = _optional_float(fields[key])
            if not valid:
                return malformed
            result[key] = number
    if "note_peaks_dbfs" in fields:
        peaks = fields["note_peaks_dbfs"]
        if not (isinstance(peaks, list) and all(is_number(peak) for peak in peaks)):
            return malformed
        result["note_peaks_dbfs"] = [float(peak) for peak in peaks]
    if "detail" in fields:
        detail = fields["detail"]
        if not (detail is None or isinstance(detail, str)):
            return malformed
        result["detail"] = detail
    return result


def run_worker(command: list[str], timeout_s: float) -> WorkerResult:
    """Run a check worker and return its validated result, or a timed-out or crashed result."""
    try:
        payload = run_isolated(command, timeout_s)
    except WorkerTimedOutError as error:
        return {"status": Status.TIMED_OUT, "detail": str(error)}
    except WorkerCrashedError as error:
        return {"status": Status.CRASHED, "detail": str(error)}
    return parse_worker_result(payload)


def run_check(specs: list[PluginSpec], timeout_s: float = WORKER_TIMEOUT_S) -> list[PluginResult]:
    results = []
    for spec in specs:
        payload = run_worker(worker_command(spec), timeout_s)
        results.append(PluginResult(name=spec.name, path=spec.path, **payload))
    return results


def exit_code(results: list[PluginResult]) -> int:
    all_pass = all(result.status is Status.PASS for result in results)
    return EXIT_ALL_PASS if all_pass else EXIT_SOME_FAILED


def align_rows(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]


YES_NO = {None: "-", True: "yes", False: "no"}


def format_table(results: list[PluginResult]) -> str:
    rows = [("PLUGIN", "STATUS", "INSTRUMENT", "PEAK DBFS", "REPEATABLE")]
    details = []
    for result in results:
        peak = "-" if result.peak_dbfs is None else f"{result.peak_dbfs:.1f}"
        rows.append(
            (
                result.name,
                str(result.status),
                YES_NO[result.is_instrument],
                peak,
                YES_NO[result.repeatable],
            )
        )
        if result.detail:
            details.append(f"{result.name}: {result.detail}")

    return "\n".join([*align_rows(rows), *details])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-plugins", description=__doc__)
    parser.add_argument("--config", type=Path, help="plugin list (TOML)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    try:
        fill_path_options(args)
    except WorkspaceError as error:
        print(f"check-plugins: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    try:
        specs = load_config(args.config)
    except ConfigError as error:
        print(f"check-plugins: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    results = run_check(specs)
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(format_table(results))
    return exit_code(results)


def _worker_result(path: str, plugin_name: str | None) -> WorkerResult:
    # Imported here so the parent never loads plugin code.
    from pedalboard import AudioUnitPlugin, VST3Plugin, load_plugin

    def load() -> VST3Plugin | AudioUnitPlugin:
        # `load_plugin` is annotated as returning `ExternalPlugin`, but it returns one of these.
        return cast(
            "VST3Plugin | AudioUnitPlugin",
            load_plugin(
                path, plugin_name=plugin_name, initialization_timeout=INITIALIZATION_TIMEOUT_S
            ),
        )

    def render(plugin: VST3Plugin | AudioUnitPlugin) -> np.ndarray:
        # Pedalboard's stubs take a `list` of its message union, and `list` is invariant.
        messages: list[MIDIMessageLike] = [*note_pattern()]
        audio = plugin(
            messages,
            duration=DURATION_S,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            reset=True,
        )
        expected = (NUM_CHANNELS, round(DURATION_S * SAMPLE_RATE))
        if audio.shape != expected:
            raise RuntimeError(f"render shape {audio.shape}, expected {expected}")
        return audio

    # Any exception from loading third-party plugin code is the result being measured.
    try:
        plugin = load()
    except Exception as error:  # noqa: BLE001
        return {"status": Status.LOAD_FAILED, "detail": str(error)}

    if not plugin.is_instrument:
        return {"status": Status.NOT_INSTRUMENT, "is_instrument": False}

    first = render(plugin)
    try:
        plugin = load()
    except Exception as error:  # noqa: BLE001
        return {"status": Status.LOAD_FAILED, "is_instrument": True, "detail": str(error)}

    analysis = analyze(first, render(plugin))
    return {
        "status": analysis.status,
        "is_instrument": True,
        "peak_dbfs": analysis.peak_dbfs,
        "note_peaks_dbfs": analysis.note_peaks_dbfs,
        "clipped": analysis.clipped,
        "repeatable": analysis.repeatable,
        "max_difference_dbfs": analysis.max_difference_dbfs,
    }


def worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="poser.plugin_check")
    parser.add_argument("path")
    parser.add_argument("--plugin-name")
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args(argv)

    # The result goes to a file because plugins may print to stdout.
    args.result_path.write_text(json.dumps(_worker_result(args.path, args.plugin_name)))
    return 0


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv[1:]))
