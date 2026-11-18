"""Tests for running a function in a child process.

On Windows, `sys.executable` may be a virtual environment's launcher, which starts the real
interpreter as its child. A test starts the processes it kills with `start_python`, and a process
the worker starts reports its own PID with `os.getpid()`.
"""

import _thread
import contextlib
import importlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import NoReturn, cast

import pytest
from interpreter import start_python

from poser.adapters.isolation import (
    WorkerCrashedError,
    WorkerTimedOutError,
    check_platform,
    run_isolated,
)
from poser.core.jsonvalue import JsonValue
from poser.core.ports import UnsupportedPlatformError

WORKERS = """
import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path


def echo(job):
    return {"done": True, "job": job}


def done(job):
    return {"done": True}


def fail(job):
    raise RuntimeError("the target broke")


def fail_in_text(job):
    raise RuntimeError("the target broke: caf\\u00e9 \\ud55c\\uae00")


def sleep(job):
    time.sleep(job["seconds"])
    return {"slept": job["seconds"]}


def mark(job):
    Path(job["path"]).write_text("ran")
    return {"done": True}


def where(job):
    return {"cwd": os.getcwd()}


def noisy(job):
    print("not json {")
    sys.stdout.flush()
    os.write(1, b"more noise\\n")
    return {"done": True}


def silent_exit(job):
    sys.exit(3)


GRANDCHILD = '''
import os
import sys
import time
from pathlib import Path

worker, pid_file = sys.argv[1:]
partial = Path(pid_file + ".partial")
partial.write_text(f"{worker} {os.getpid()}")
partial.rename(pid_file)
time.sleep(60)
'''


# Start a grandchild that writes the worker's PID and its own, and wait until it has.
def start_grandchild(job):
    pid_file = Path(job["pid_file"])
    subprocess.Popen([sys.executable, "-c", GRANDCHILD, str(os.getpid()), str(pid_file)])
    deadline = time.monotonic() + 30
    while not pid_file.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("the grandchild wrote no PIDs")
        time.sleep(0.01)


def spawn_and_sleep(job):
    start_grandchild(job)
    time.sleep(job["seconds"])
    return {"slept": job["seconds"]}


def spawn_and_hold_the_gil(job):
    start_grandchild(job)
    # A PyDLL call keeps the GIL while it runs.
    if sys.platform == "win32":
        ctypes.PyDLL("kernel32").Sleep(job["seconds"] * 1000)
    else:
        ctypes.PyDLL(None).sleep(job["seconds"])
    return {"slept": job["seconds"]}


def spawn_and_return(job):
    start_grandchild(job)
    return {"done": True}


def spawn_and_fail(job):
    start_grandchild(job)
    raise RuntimeError("the target broke")


def spawn_and_exit_without_a_result(job):
    start_grandchild(job)
    os._exit(0)
"""

Target = Callable[[JsonValue], JsonValue]


@pytest.fixture
def workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import a worker module from a folder on both the test's and the child's path."""
    folder = tmp_path / "workers"
    folder.mkdir()
    (folder / "isolation_workers.py").write_text(WORKERS, encoding="utf-8", newline="\n")
    monkeypatch.syspath_prepend(str(folder))
    monkeypatch.setenv("PYTHONPATH", str(folder))
    monkeypatch.delitem(sys.modules, "isolation_workers", raising=False)
    return importlib.import_module("isolation_workers")


def target(module: ModuleType, name: str) -> Target:
    return cast("Target", getattr(module, name))


def test_run_isolated_returns_the_result_of_the_job(workers: ModuleType) -> None:
    job: JsonValue = {"text": "é ✓", "numbers": [1, 2.5, None], "nested": {"ok": True}}
    assert run_isolated(target(workers, "done"), {}, 30) == {"done": True}
    assert run_isolated(target(workers, "echo"), job, 30) == {"done": True, "job": job}


def test_run_isolated_raises_the_target_s_exception_as_a_crash(workers: ModuleType) -> None:
    with pytest.raises(WorkerCrashedError, match=r"^exit code 1: .*the target broke$"):
        run_isolated(target(workers, "fail"), {}, 30)


@pytest.mark.parametrize("encoding", [None, "latin-1", "utf-8"])
def test_a_crash_s_message_reads_as_the_worker_wrote_it(
    workers: ModuleType, monkeypatch: pytest.MonkeyPatch, encoding: str | None
) -> None:
    if encoding is None:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    else:
        monkeypatch.setenv("PYTHONIOENCODING", encoding)
    with pytest.raises(WorkerCrashedError, match=r"the target broke: café 한글$"):
        run_isolated(target(workers, "fail_in_text"), {}, 30)


def test_run_isolated_reports_an_exit_without_output(workers: ModuleType) -> None:
    with pytest.raises(WorkerCrashedError, match=r"^exit code 3: no error output$"):
        run_isolated(target(workers, "silent_exit"), {}, 30)


def test_run_isolated_times_out(workers: ModuleType) -> None:
    start = time.monotonic()
    with pytest.raises(WorkerTimedOutError, match=r"^worker exceeded 1 seconds$"):
        run_isolated(target(workers, "sleep"), {"seconds": 30}, 1)
    assert time.monotonic() - start < 20


def test_run_isolated_without_a_timeout_waits(workers: ModuleType) -> None:
    assert run_isolated(target(workers, "sleep"), {"seconds": 1.5}, None) == {"slept": 1.5}


def test_run_isolated_ignores_what_the_target_prints(workers: ModuleType) -> None:
    assert run_isolated(target(workers, "noisy"), {}, 30) == {"done": True}


def test_a_worker_runs_where_child_cwd_says(
    workers: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_isolated` starts the worker in the directory `child_cwd` names. On Windows, a worker
    whose poser working directory is past CreateProcess's limit starts in the directory that
    holds its job; every other worker inherits poser's working directory."""
    monkeypatch.setattr("poser.adapters.isolation.child_cwd", lambda fallback: str(tmp_path))
    assert run_isolated(target(workers, "where"), {}, 30) == {"cwd": str(tmp_path)}


class Holder:
    def method(self, job: JsonValue) -> JsonValue:
        return job


def from_main(job: JsonValue) -> JsonValue:
    return job


def refuse_processes(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError(f"a process started: {args} {kwargs}")


def test_run_isolated_refuses_targets_a_child_cannot_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def nested(job: JsonValue) -> JsonValue:
        return job

    monkeypatch.setattr(subprocess, "Popen", refuse_processes)
    monkeypatch.setattr(from_main, "__module__", "__main__")
    for refused in (lambda job: job, nested, Holder().method, from_main):
        with pytest.raises(ValueError, match="a child process cannot import"):
            run_isolated(refused, {}, 30)


def test_the_child_ignores_modules_in_the_working_directory(
    workers: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / "json.py").write_text(
        "raise SystemExit('the planted json.py ran')\n", encoding="utf-8", newline="\n"
    )
    assert Path.cwd() == tmp_path
    assert run_isolated(target(workers, "done"), {}, 30) == {"done": True}


def read_pids(pid_file: Path) -> tuple[int, int]:
    """Wait for the worker's grandchild to write the worker's PID and its own, and return them."""
    deadline = time.monotonic() + 30
    while not pid_file.exists():
        assert time.monotonic() < deadline, "the grandchild wrote no PIDs"
        time.sleep(0.05)
    worker, grandchild = pid_file.read_text(encoding="utf-8").split()
    return int(worker), int(grandchild)


def exists(pid: int) -> bool:
    """Whether `pid` is a running process. On Windows, `os.kill(pid, 0)` would send Ctrl+C to
    process group `pid` instead."""
    if sys.platform == "win32":
        import _winapi

        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError:
            return False
        try:
            return _winapi.WaitForSingleObject(handle, 0) == _winapi.WAIT_TIMEOUT
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def kill(pid: int) -> None:
    """Kill `pid` unless it is already gone. On Windows, any signal but the console events
    terminates the process."""
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)


def ended(pid: int) -> bool:
    """Poll for up to 30 seconds until `pid` no longer exists, and return whether it ended. A
    killed orphan exists until init reaps it."""
    deadline = time.monotonic() + 30
    while exists(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def wait_until_gone(pid: int) -> None:
    assert ended(pid), f"process {pid} is still running"


@pytest.fixture
def temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make the folder where `run_isolated` puts its temporary files."""
    folder = tmp_path / "temp"
    folder.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(folder))
    return folder


def assert_left_nothing(pid_file: Path, temp: Path) -> None:
    """Check that the worker and its grandchild have ended and that no temporary file remains.
    On Windows, a file a process still holds cannot be removed."""
    worker, grandchild = read_pids(pid_file)
    assert not exists(worker)
    wait_until_gone(grandchild)
    assert list(temp.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "timeout_s", "error"),
    [
        ("spawn_and_sleep", 5, WorkerTimedOutError),
        ("spawn_and_fail", 30, WorkerCrashedError),
        ("spawn_and_exit_without_a_result", 30, WorkerCrashedError),
    ],
)
def test_a_worker_that_does_not_return_takes_its_processes_and_files_with_it(
    workers: ModuleType,
    tmp_path: Path,
    temp: Path,
    name: str,
    timeout_s: float,
    error: type[Exception],
) -> None:
    pid_file = tmp_path / "pids"
    job: JsonValue = {"pid_file": str(pid_file), "seconds": 60}
    with pytest.raises(error):
        run_isolated(target(workers, name), job, timeout_s)
    assert_left_nothing(pid_file, temp)


def test_an_interrupt_while_waiting_takes_the_worker_s_processes_and_files_with_it(
    workers: ModuleType, tmp_path: Path, temp: Path
) -> None:
    pid_file = tmp_path / "pids"
    interrupted: list[float] = []

    def interrupt_once_the_grandchild_runs() -> None:
        read_pids(pid_file)
        interrupted.append(time.monotonic())
        _thread.interrupt_main()

    interrupter = threading.Thread(target=interrupt_once_the_grandchild_runs)
    interrupter.start()
    try:
        job: JsonValue = {"pid_file": str(pid_file), "seconds": 60}
        with pytest.raises(KeyboardInterrupt):
            run_isolated(target(workers, "spawn_and_sleep"), job, 30)
        (interrupted_at,) = interrupted
        assert time.monotonic() - interrupted_at < 10
    finally:
        interrupter.join()
    assert_left_nothing(pid_file, temp)


def test_a_normal_exit_leaves_the_processes_the_worker_started(
    workers: ModuleType, tmp_path: Path
) -> None:
    pid_file = tmp_path / "pids"
    job: JsonValue = {"pid_file": str(pid_file), "seconds": 0}
    assert run_isolated(target(workers, "spawn_and_return"), job, 30) == {"done": True}
    _, grandchild = read_pids(pid_file)
    time.sleep(0.5)
    assert exists(grandchild)
    kill(grandchild)


@pytest.mark.parametrize("worker", ["spawn_and_sleep", "spawn_and_hold_the_gil"])
def test_the_worker_dies_with_the_process_that_started_it(
    workers: ModuleType, tmp_path: Path, worker: str
) -> None:
    pid_file = tmp_path / "pids"
    script = tmp_path / "parent.py"
    script.write_text(
        "import isolation_workers\n"
        "from poser.adapters.isolation import run_isolated\n"
        f"job = {{'pid_file': {str(pid_file)!r}, 'seconds': 60}}\n"
        f"run_isolated(isolation_workers.{worker}, job, None)\n",
        encoding="utf-8",
        newline="\n",
    )
    with start_python(str(script)) as parent:
        try:
            worker_pid, grandchild = read_pids(pid_file)
        finally:
            parent.kill()
    survivors = [pid for pid in (worker_pid, grandchild) if not ended(pid)]
    for pid in survivors:
        kill(pid)  # Still running, so its PID still names it.
    assert not survivors


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are Windows")
def test_a_worker_the_job_cannot_hold_is_killed_before_it_runs(
    workers: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolation_windows = importlib.import_module("poser.adapters.isolation_windows")
    started: list[int] = []

    def refuse(job: object, pid: int) -> NoReturn:
        started.append(pid)
        time.sleep(5)  # A worker that was not suspended would write the marker meanwhile.
        raise OSError("the job refused the worker")

    monkeypatch.setattr(isolation_windows.WorkerJob, "start", refuse)
    marker = tmp_path / "ran"
    with pytest.raises(OSError, match="the job refused the worker"):
        run_isolated(target(workers, "mark"), {"path": str(marker)}, 30)
    (pid,) = started
    wait_until_gone(pid)
    assert not marker.exists()


@pytest.mark.parametrize("platform", ["freebsd14", "cygwin"])
def test_check_platform_refuses_platforms_without_isolation(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(UnsupportedPlatformError) as raised:
        check_platform()
    message = str(raised.value)
    for part in (f"({platform})", "worker isolation", "docs/setup.md#prerequisites"):
        assert part in message, part


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_check_platform_accepts_macos_linux_and_windows(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    check_platform()
