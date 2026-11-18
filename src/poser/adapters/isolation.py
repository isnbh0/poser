"""Run a function in a subprocess, so a crash or hang in plugin code cannot stop poser.

Process isolation protects poser from plugin code, which only adapters run. The child process
inherits the environment but not the changes the parent made to `sys.path` at run time, so a
target must live in an installed module or in one on `PYTHONPATH`. Isolation uses process groups
and descriptor passing on macOS and Linux, and a Job Object on Windows (`isolation_windows`);
`check_platform` names other platforms.
"""

import contextlib
import importlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeGuard

from poser.core.jsonvalue import JsonValue, is_json_value, json_object
from poser.core.paths import child_cwd
from poser.core.ports import PREREQUISITES_URL, UnsupportedPlatformError

# The worker writes its stderr as UTF-8, whatever the locale, UTF-8 mode, or PYTHONIOENCODING.
CHILD_CODE = (
    "import sys; sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace'); "
    "from poser.adapters.isolation import child_main; child_main()"
)
CREATE_SUSPENDED = 0x00000004
WAIT_SLICE_S = 0.1


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


def check_platform() -> None:
    """Raise UnsupportedPlatformError unless the running platform has worker isolation.

    An adapter that runs work through `run_isolated` calls this when built, so the platform gate
    refuses it at build instead of at its first render. Reads only `sys.platform`.
    """
    platform = sys.platform
    if platform in {"darwin", "win32"} or platform.startswith("linux"):
        return
    raise UnsupportedPlatformError(
        f"poser's plugin worker isolation does not cover this platform ({platform}); it covers "
        f"macOS, Linux, and Windows. See {PREREQUISITES_URL}"
    )


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

    On macOS and Linux, the worker dies with poser, and so do its descendants that stay in its
    process group; a descendant that starts its own session or group escapes. The worker leads its
    own session and process group, so the terminal's interrupt reaches poser, not plugin code.
    When poser times the worker out or is interrupted or raises while waiting, it kills the
    worker's group before raising. A watcher process in that group holds the read end of a
    lifeline pipe whose write end only poser holds; however poser ends, even by SIGKILL, the
    kernel closes the write end, and the watcher kills the group. Being a separate process, the
    watcher works even while plugin code holds the worker's GIL. A worker whose target returns
    ends its watcher first, so helper processes it left running survive; after any other exit,
    the watcher kills the group once `run_isolated` closes the write end.

    On Windows, a Job Object that only poser holds contains the worker and every process it
    starts. The worker is created suspended and joins the job before it runs any code, because a
    virtual environment's `python.exe` is a launcher that starts the real interpreter at once.
    However poser ends, the system closes poser's handle to the job, and the job kills its
    processes. When poser times the worker out or is interrupted or raises while waiting, it
    kills the job and waits for its processes to end before raising. The worker starts in a new
    process group, whose processes ignore Ctrl+C, so Ctrl+C interrupts only poser; Ctrl+Break and
    closing the console end both. A worker that exits 0 after writing its result releases the
    job, so helper processes it left running survive, and the temporary files they hold stay
    behind; after any other exit, poser kills the job. The job controls lifetime and does not
    sandbox plugin code. Windows starts no child in a working directory of more than
    `poser.core.paths.CREATE_PROCESS_CWD_LIMIT` characters (`poser.core.paths.child_cwd`), so a
    worker poser runs from a longer directory starts in the directory that holds its job and
    result files.
    """
    module = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None)
    if not (isinstance(module, str) and isinstance(qualname, str)):
        raise ValueError(f"a child process cannot import {target!r}")  # noqa: TRY004
    if module == "__main__":
        raise ValueError(f"a child process cannot import {qualname} from __main__")
    if "." in qualname or "<" in qualname:
        raise ValueError(f"a child process cannot import {module}.{qualname}")

    # On Windows, a helper the worker left running may still hold stderr.txt.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=sys.platform == "win32") as directory:
        job_path = Path(directory) / "job.json"
        result_path = Path(directory) / "result.json"
        stderr_path = Path(directory) / "stderr.txt"
        job_path.write_text(json.dumps(job, allow_nan=False), encoding="utf-8", newline="\n")
        cwd = child_cwd(Path(directory))
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
        if sys.platform == "win32":
            returncode = _run_in_job(command, stderr_path, result_path, timeout_s, cwd)
        else:
            returncode = _run_in_group(command, stderr_path, timeout_s, cwd)

        if returncode != 0:
            # Native code in the worker can write any bytes to its stderr.
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
            lines = stderr.strip().splitlines()
            last_line = lines[-1] if lines else "no error output"
            raise WorkerCrashedError(f"exit code {returncode}: {last_line}")

        try:
            result: object = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WorkerCrashedError("worker wrote no readable result") from error
        if not is_json_value(result):
            raise WorkerCrashedError("worker wrote no readable result")
        return result


def _timed_out(timeout_s: float | None) -> WorkerTimedOutError:
    return WorkerTimedOutError(f"worker exceeded {timeout_s:g} seconds")


def _run_in_group(
    command: list[str], stderr_path: Path, timeout_s: float | None, cwd: str | None
) -> int:
    """Run the worker as the leader of a new session, passing it the lifeline's read end, and
    return its exit code."""
    assert sys.platform != "win32"  # noqa: S101 - narrows the platform for type checkers.
    read_fd, write_fd = os.pipe()
    try:
        try:
            with stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [*command, str(read_fd)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    cwd=cwd,
                    start_new_session=True,
                    pass_fds=(read_fd,),
                )
        finally:
            os.close(read_fd)
        try:
            return process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as error:
            _end_worker(process)
            raise _timed_out(timeout_s) from error
        except BaseException:
            _end_worker(process)
            raise
    finally:
        os.close(write_fd)


def _run_in_job(
    command: list[str],
    stderr_path: Path,
    result_path: Path,
    timeout_s: float | None,
    cwd: str | None,
) -> int:
    """Run the worker in a Job Object, created suspended and assigned before it runs, and return
    its exit code."""
    assert sys.platform == "win32"  # noqa: S101 - narrows the platform for type checkers.
    from poser.adapters.isolation_windows import WorkerJob

    job = WorkerJob()
    process: subprocess.Popen[bytes] | None = None
    try:
        # One handler covers the worker from the moment `Popen` returns it, so an interrupt kills
        # it whether or not it has joined the job: the job's processes first, and then the
        # worker, which the job may not hold. An interrupt inside `Popen`, after the system
        # created the worker, leaves it suspended outside the job.
        try:
            with stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    cwd=cwd,
                    creationflags=CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            job.start(process.pid)
            returncode = _wait_in_slices(process, timeout_s)
        except BaseException as error:
            if process is not None:
                try:
                    job.kill()
                finally:
                    process.kill()
                    process.wait()
            if isinstance(error, subprocess.TimeoutExpired):
                raise _timed_out(timeout_s) from error
            raise
        if returncode == 0 and result_path.exists():
            job.release()
        else:
            job.kill()
        return returncode
    finally:
        job.close()


def _wait_in_slices(process: subprocess.Popen[bytes], timeout_s: float | None) -> int:
    """Wait for `process` as `Popen.wait` does, in slices of at most `WAIT_SLICE_S` seconds.

    On Windows, `Popen.wait` makes one `WaitForSingleObject` call, and Python raises
    KeyboardInterrupt only once that call returns, so a single long wait would hold Ctrl+C back.
    """
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    while True:
        slice_s = WAIT_SLICE_S
        if deadline is not None:
            slice_s = max(0.0, min(slice_s, deadline - time.monotonic()))
        try:
            return process.wait(timeout=slice_s)
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                raise


def _end_worker(process: subprocess.Popen[bytes]) -> None:
    """Kill the worker's process group: the worker, its watcher, and the descendants still in
    the group."""
    assert sys.platform != "win32"  # noqa: S101 - narrows the platform for type checkers.
    # The leader is unreaped, so its group exists. macOS refuses with EPERM when every member
    # is a zombie, which, like ESRCH, means nothing is left to kill.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def _start_watcher(lifeline: int) -> int:
    """Fork a process that kills this process group once the lifeline reaches EOF, which means
    poser has ended or closed it, and return its PID. Call only while single-threaded."""
    assert sys.platform != "win32"  # noqa: S101 - narrows the platform for type checkers.
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
    assert sys.platform != "win32"  # noqa: S101 - narrows the platform for type checkers.
    os.kill(watcher, signal.SIGKILL)
    os.waitpid(watcher, 0)


def child_main() -> None:
    """Run in the child: call the target named on the command line and write its result.

    An exception propagates, so the child exits nonzero with the error as its last stderr line.
    On macOS and Linux, the last argument is the lifeline. Before importing the target, while the
    worker is still single-threaded, it forks a watcher that keeps the lifeline and kills the
    group at EOF; the worker closes its own copy, so processes the target starts never hold it.
    After writing the result, the worker ends the watcher, so the EOF that follows leaves its
    helpers running. Any other exit leaves the watcher, which kills the group when `run_isolated`
    closes the lifeline. On Windows, the job `run_isolated` holds does this work, and there is no
    lifeline.
    """
    if sys.platform == "win32":
        module_name, qualname, job_path, result_path = sys.argv[1:]
        watcher = None
    else:
        module_name, qualname, job_path, result_path, lifeline = sys.argv[1:]
        watcher = _start_watcher(int(lifeline))
    target = getattr(importlib.import_module(module_name), qualname)
    result = target(json.loads(Path(job_path).read_text(encoding="utf-8")))
    Path(result_path).write_text(
        json.dumps(result, allow_nan=False), encoding="utf-8", newline="\n"
    )
    if watcher is not None:
        _end_watcher(watcher)
