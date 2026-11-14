"""Run a function in a subprocess, so a crash or hang in plugin code cannot stop poser.

Process isolation protects poser from plugin code, which only adapters run. The child process
inherits the environment but not the changes the parent made to `sys.path` at run time, so a
target must live in an installed module or in one on `PYTHONPATH`.
"""

import contextlib
import importlib
import json
import os
import signal
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

    The worker dies with poser, and so do its descendants that stay in its process group; a
    descendant that starts its own session or group escapes. The worker leads its own session and
    process group, so the terminal's interrupt reaches poser, not plugin code. When poser times
    the worker out or is interrupted or raises while waiting, it kills the worker's group before
    raising. A watcher process in that group holds the read end of a lifeline pipe whose write end
    only poser holds; however poser ends, even by SIGKILL, the kernel closes the write end, and
    the watcher kills the group. Being a separate process, the watcher works even while plugin
    code holds the worker's GIL. A worker whose target returns ends its watcher first, so helper
    processes it left running survive; after any other exit, the watcher kills the group once
    `run_isolated` closes the write end.
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
        read_fd, write_fd = os.pipe()
        command = [
            sys.executable,
            "-P",
            "-c",
            CHILD_CODE,
            module,
            qualname,
            str(job_path),
            str(result_path),
            str(read_fd),
        ]
        try:
            try:
                with stderr_path.open("wb") as stderr:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr,
                        start_new_session=True,
                        pass_fds=(read_fd,),
                    )
            finally:
                os.close(read_fd)
            try:
                returncode = process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired as error:
                _end_worker(process)
                raise WorkerTimedOutError(f"worker exceeded {timeout_s:g} seconds") from error
            except BaseException:
                _end_worker(process)
                raise
        finally:
            os.close(write_fd)

        if returncode != 0:
            lines = stderr_path.read_text(errors="replace").strip().splitlines()
            last_line = lines[-1] if lines else "no error output"
            raise WorkerCrashedError(f"exit code {returncode}: {last_line}")

        try:
            result: object = json.loads(result_path.read_text())
        except (OSError, ValueError) as error:
            raise WorkerCrashedError("worker wrote no readable result") from error
        if not is_json_value(result):
            raise WorkerCrashedError("worker wrote no readable result")
        return result


def _end_worker(process: subprocess.Popen[bytes]) -> None:
    """Kill the worker's process group: the worker, its watcher, and the descendants still in
    the group."""
    # The leader is unreaped, so its group exists. macOS refuses with EPERM when every member
    # is a zombie, which, like ESRCH, means nothing is left to kill.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _start_watcher(lifeline: int) -> int:
    """Fork a process that kills this process group once the lifeline reaches EOF, which means
    poser has ended or closed it, and return its PID. Call only while single-threaded."""
    pid = os.fork()
    if pid == 0:
        try:
            os.read(lifeline, 1)
            os.killpg(0, signal.SIGKILL)
        finally:
            os._exit(0)
    os.close(lifeline)
    return pid


def _end_watcher(watcher: int) -> None:
    os.kill(watcher, signal.SIGKILL)
    os.waitpid(watcher, 0)


def child_main() -> None:
    """Run in the child: call the target named on the command line and write its result.

    An exception propagates, so the child exits nonzero with the error as its last stderr line.
    The last argument is the lifeline. Before importing the target, while the worker is still
    single-threaded, it forks a watcher that keeps the lifeline and kills the group at EOF; the
    worker closes its own copy, so processes the target starts never hold it. After writing the
    result, the worker ends the watcher, so the EOF that follows leaves its helpers running. Any
    other exit leaves the watcher, which kills the group when `run_isolated` closes the lifeline.
    """
    module_name, qualname, job_path, result_path, lifeline_arg = sys.argv[1:]
    watcher = _start_watcher(int(lifeline_arg))
    target = getattr(importlib.import_module(module_name), qualname)
    result = target(json.loads(Path(job_path).read_text()))
    Path(result_path).write_text(json.dumps(result, allow_nan=False))
    _end_watcher(watcher)
