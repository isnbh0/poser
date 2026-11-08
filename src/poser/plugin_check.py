"""Check that listed VST3 plugins load through Pedalboard and produce sound.

Each plugin runs in its own worker subprocess, so a crash, hang, or license dialog in one plugin
cannot stop the report for the others. `check-plugins` is the parent command. Running this module
with `python -m` is the worker.
"""

import argparse
import json
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

import numpy as np

from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    RENDER_TIMEOUT_S,
    align_rows,
)
from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE
from poser.core.checks import CHECK_SAMPLES, CheckStatus, analyze_check, check_pattern
from poser.core.names import Identifier
from poser.core.song import is_number
from poser.worker import (
    WorkerCrashedError,
    WorkerTimedOutError,
    json_object,
    performance_messages,
    render_duration_s,
    run_isolated,
)
from poser.workspace import WorkspaceError, fill_path_options

if TYPE_CHECKING:
    from pedalboard_native import MIDIMessageLike

INITIALIZATION_TIMEOUT_S = 30.0


class ConfigError(Exception):
    """The plugin list could not be read."""


@dataclass(frozen=True)
class PluginSpec:
    name: str
    path: str
    plugin_name: str | None = None
    presets: str | None = None  # Directory of the plugin's .fxp presets.


@dataclass(frozen=True)
class PluginResult:
    name: str
    path: str
    status: CheckStatus
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

    status: CheckStatus
    is_instrument: NotRequired[bool | None]
    peak_dbfs: NotRequired[float | None]
    note_peaks_dbfs: NotRequired[list[float]]
    clipped: NotRequired[bool | None]
    repeatable: NotRequired[bool | None]
    max_difference_dbfs: NotRequired[float | None]
    detail: NotRequired[str | None]


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
        # A plugin name starts a sound reference, so it must be an instrument name.
        if not Identifier.PATTERN.fullmatch(entry["name"]):
            raise ConfigError(
                f"{path}: plugin name {entry['name']!r} must match {Identifier.PATTERN.pattern}"
            )
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
        "status": CheckStatus.CRASHED,
        "detail": "worker wrote a malformed result",
    }
    fields = json_object(payload)
    if fields is None or not set(fields) <= WORKER_FIELDS:
        return malformed
    status = fields.get("status")
    if not isinstance(status, str) or status not in CheckStatus:
        return {"status": CheckStatus.CRASHED, "detail": "worker wrote an unknown status"}

    result: WorkerResult = {"status": CheckStatus(status)}
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
        return {"status": CheckStatus.TIMED_OUT, "detail": str(error)}
    except WorkerCrashedError as error:
        return {"status": CheckStatus.CRASHED, "detail": str(error)}
    return parse_worker_result(payload)


def run_check(specs: list[PluginSpec], timeout_s: float = RENDER_TIMEOUT_S) -> list[PluginResult]:
    results = []
    for spec in specs:
        payload = run_worker(worker_command(spec), timeout_s)
        results.append(PluginResult(name=spec.name, path=spec.path, **payload))
    return results


def exit_code(results: list[PluginResult]) -> int:
    all_pass = all(result.status is CheckStatus.PASS for result in results)
    return EXIT_ALL_PASS if all_pass else EXIT_SOME_FAILED


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
        messages: list[MIDIMessageLike] = [*performance_messages(check_pattern())]
        audio = plugin(
            messages,
            duration=render_duration_s(CHECK_SAMPLES),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            reset=True,
        )
        expected = (NUM_CHANNELS, CHECK_SAMPLES)
        if audio.shape != expected:
            raise RuntimeError(f"render shape {audio.shape}, expected {expected}")
        return audio

    # Any exception from loading third-party plugin code is the result being measured.
    try:
        plugin = load()
    except Exception as error:  # noqa: BLE001
        return {"status": CheckStatus.LOAD_FAILED, "detail": str(error)}

    if not plugin.is_instrument:
        return {
            "status": CheckStatus.LOAD_FAILED,
            "is_instrument": False,
            "detail": f"{path} is not an instrument",
        }

    first = render(plugin)
    try:
        plugin = load()
    except Exception as error:  # noqa: BLE001
        return {"status": CheckStatus.LOAD_FAILED, "is_instrument": True, "detail": str(error)}

    analysis = analyze_check(first, render(plugin))
    return {
        "status": analysis.status,
        "is_instrument": True,
        "peak_dbfs": analysis.peak_dbfs,
        "note_peaks_dbfs": list(analysis.note_peaks_dbfs),
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
