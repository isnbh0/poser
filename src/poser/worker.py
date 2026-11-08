"""Run plugin code in a subprocess, so a crash or hang cannot stop work on anything else."""

import json
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path


def json_object(value: object) -> Mapping[str, object] | None:
    """Return parsed JSON as a mapping of untyped values, or None when it is not an object.

    The declared return type makes the type checker require each value to be checked before use.
    """
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return None


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
