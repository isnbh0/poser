"""Run a function in a subprocess, so a crash or hang in plugin code cannot stop poser.

Process isolation protects poser from plugin code, which only adapters run. The child process
inherits the environment but not the changes the parent made to `sys.path` at run time, so a
target must live in an installed module or in one on `PYTHONPATH`.
"""

import importlib
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeGuard

from poser.core.jsonvalue import JsonValue, is_json_value, json_object

CHILD_CODE = "from poser.adapters.isolation import child_main; child_main()"


class MalformedJobError(ValueError):
    """A worker's job does not hold the job the parent writes. The parent writes every job itself,
    so this is a bug, and the worker fails loudly."""

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


def run_isolated(
    target: Callable[[JsonValue], JsonValue], job: JsonValue, timeout_s: float | None
) -> JsonValue:
    """Call `target(job)` in a child process and return its JSON result.

    `target` must be a module-level function of an importable module. `timeout_s=None` waits for
    as long as the child runs, as when it shows a plugin window.

    The result goes to a file because plugins may print to stdout. Stdout and stderr avoid pipes,
    so a helper process left running by a plugin cannot hold them open and make a finished worker
    look hung. `-P` keeps the working directory off the child's `sys.path`, and `-c` means no
    poser module runs as `__main__`, so none is loaded twice under two names.
    """
    module = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None)
    if not (isinstance(module, str) and isinstance(qualname, str)):
        raise ValueError(f"a child process cannot import {target!r}")  # noqa: TRY004
    if module == "__main__":
        raise ValueError(f"a child process cannot import {qualname} from __main__")
    if "." in qualname or "<" in qualname:
        raise ValueError(f"a child process cannot import {module}.{qualname}")

    with tempfile.TemporaryDirectory() as directory:
        job_path = Path(directory) / "job.json"
        result_path = Path(directory) / "result.json"
        stderr_path = Path(directory) / "stderr.txt"
        job_path.write_text(json.dumps(job, allow_nan=False))
        command = [
            sys.executable,
            "-P",
            "-c",
            CHILD_CODE,
            module,
            qualname,
            str(job_path),
            str(result_path),
        ]
        try:
            with stderr_path.open("wb") as stderr:
                completed = subprocess.run(
                    command,
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
            result: object = json.loads(result_path.read_text())
        except (OSError, ValueError) as error:
            raise WorkerCrashedError("worker wrote no readable result") from error
        if not is_json_value(result):
            raise WorkerCrashedError("worker wrote no readable result")
        return result


def child_main() -> None:
    """Run in the child: call the target named on the command line and write its result.

    An exception propagates, so the child exits nonzero with the error as its last stderr line.
    """
    module_name, qualname, job_path, result_path = sys.argv[1:]
    target = getattr(importlib.import_module(module_name), qualname)
    result = target(json.loads(Path(job_path).read_text()))
    Path(result_path).write_text(json.dumps(result, allow_nan=False))
