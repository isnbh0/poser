"""Check that listed VST3 plugins load through Pedalboard and produce sound.

Each plugin runs in its own worker subprocess, so a crash, hang, or license dialog in one plugin
cannot stop the report for the others. `check-plugins` is the parent command. Running this module
with `python -m` is the worker.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any

import mido
import numpy as np

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_DIR / "plugins.toml"

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
    PASS = "pass"


class ConfigError(Exception):
    """The plugin list could not be read."""


@dataclass(frozen=True)
class PluginSpec:
    name: str
    path: str
    plugin_name: str | None = None


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


def _peak_dbfs(samples: np.ndarray) -> float:
    return _to_dbfs(float(np.abs(samples[np.isfinite(samples)]).max(initial=0.0)))


def _note_peaks_dbfs(render: np.ndarray) -> list[float]:
    peaks = []
    for index in range(len(NOTES)):
        start_s = FIRST_NOTE_S + index * NOTE_SPACING_S
        start = round(start_s * SAMPLE_RATE)
        stop = round((start_s + NOTE_LENGTH_S) * SAMPLE_RATE)
        peaks.append(_peak_dbfs(render[..., start:stop]))
    return peaks


def analyze(first: np.ndarray, second: np.ndarray) -> Analysis:
    """Measure two channels-first renders of the test pattern.

    Peaks report the quieter of the two renders, so a plugin that goes silent on reload is caught.
    """
    first = np.asarray(first, dtype=np.float64)
    second = np.asarray(second, dtype=np.float64)

    both_finite = np.isfinite(first) & np.isfinite(second)
    difference = np.abs(first[both_finite] - second[both_finite]).max(initial=0.0)

    peak_dbfs = min(_peak_dbfs(first), _peak_dbfs(second))
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
        match entry:
            case {"name": str(name), "path": str(plugin_path), "plugin_name": str(plugin_name)}:
                specs.append(PluginSpec(name, plugin_path, plugin_name))
            case {"name": str(name), "path": str(plugin_path)} if "plugin_name" not in entry:
                specs.append(PluginSpec(name, plugin_path))
            case _:
                raise ConfigError(f"{path} has an invalid entry: {entry!r}")
    return specs


def worker_command(spec: PluginSpec) -> list[str]:
    command = [sys.executable, "-m", "vsti_composer.plugin_check", spec.path]
    if spec.plugin_name is not None:
        command += ["--plugin-name", spec.plugin_name]
    return command


def run_worker(command: list[str], timeout_s: float) -> dict[str, Any]:
    """Run a worker, appending its result path as the last argument, and return its result.

    Stdout and stderr avoid pipes, so a helper process left running by a plugin cannot hold them
    open and make a finished worker look hung.
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
        except subprocess.TimeoutExpired:
            return {
                "status": Status.TIMED_OUT,
                "detail": f"worker exceeded {timeout_s:g} seconds",
            }

        stderr_lines = stderr_path.read_text(errors="replace").strip().splitlines()
        last_line = stderr_lines[-1] if stderr_lines else "no error output"
        if completed.returncode != 0:
            return {
                "status": Status.CRASHED,
                "detail": f"exit code {completed.returncode}: {last_line}",
            }
        return _read_worker_result(result_path)


def _read_worker_result(result_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(result_path.read_text())
    except (OSError, ValueError):
        return {"status": Status.CRASHED, "detail": "worker wrote no readable result"}

    if not isinstance(payload, dict) or not set(payload) <= WORKER_FIELDS:
        return {"status": Status.CRASHED, "detail": "worker wrote a malformed result"}
    try:
        payload["status"] = Status(payload.get("status"))
    except ValueError:
        return {"status": Status.CRASHED, "detail": "worker wrote an unknown status"}
    return payload


def run_check(specs: list[PluginSpec], timeout_s: float = WORKER_TIMEOUT_S) -> list[PluginResult]:
    results = []
    for spec in specs:
        payload = run_worker(worker_command(spec), timeout_s)
        results.append(PluginResult(name=spec.name, path=spec.path, **payload))
    return results


def exit_code(results: list[PluginResult]) -> int:
    all_pass = all(result.status is Status.PASS for result in results)
    return EXIT_ALL_PASS if all_pass else EXIT_SOME_FAILED


def format_table(results: list[PluginResult]) -> str:
    def yes_no(value: bool | None) -> str:
        return "-" if value is None else ("yes" if value else "no")

    rows = [("PLUGIN", "STATUS", "INSTRUMENT", "PEAK DBFS", "REPEATABLE")]
    details = []
    for result in results:
        peak = "-" if result.peak_dbfs is None else f"{result.peak_dbfs:.1f}"
        rows.append(
            (
                result.name,
                str(result.status),
                yes_no(result.is_instrument),
                peak,
                yes_no(result.repeatable),
            )
        )
        if result.detail:
            details.append(f"{result.name}: {result.detail}")

    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)) for row in rows]
    return "\n".join(line.rstrip() for line in [*lines, *details])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check-plugins", description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="plugin list (TOML)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

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


def _worker_result(path: str, plugin_name: str | None) -> dict[str, Any]:
    from pedalboard import load_plugin  # Imported here so the parent never loads plugin code.

    def load() -> Any:
        return load_plugin(
            path, plugin_name=plugin_name, initialization_timeout=INITIALIZATION_TIMEOUT_S
        )

    def render(plugin: Any) -> np.ndarray:
        audio = plugin(
            note_pattern(),
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

    return {"is_instrument": True, **asdict(analyze(first, render(plugin)))}


def worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="vsti_composer.plugin_check")
    parser.add_argument("path")
    parser.add_argument("--plugin-name")
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args(argv)

    # The result goes to a file because plugins may print to stdout.
    args.result_path.write_text(json.dumps(_worker_result(args.path, args.plugin_name)))
    return 0


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv[1:]))
