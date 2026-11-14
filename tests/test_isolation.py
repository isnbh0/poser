"""Tests for running a function in a child process."""

import contextlib
import importlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import NoReturn, cast

import pytest

from poser.adapters.isolation import WorkerCrashedError, WorkerTimedOutError, run_isolated
from poser.core.jsonvalue import JsonValue

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


def sleep(job):
    time.sleep(job["seconds"])
    return {"slept": job["seconds"]}


def noisy(job):
    print("not json {")
    sys.stdout.flush()
    os.write(1, b"more noise\\n")
    return {"done": True}


def silent_exit(job):
    sys.exit(3)


def start_grandchild(job):
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    pid_file = Path(job["pid_file"])
    partial = pid_file.with_suffix(".partial")
    partial.write_text(f"{os.getpid()} {grandchild.pid}")
    partial.rename(pid_file)


def spawn_and_sleep(job):
    start_grandchild(job)
    time.sleep(job["seconds"])
    return {"slept": job["seconds"]}


def spawn_and_hold_the_gil(job):
    start_grandchild(job)
    ctypes.PyDLL(None).sleep(job["seconds"])
    return {"slept": job["seconds"]}


def spawn_and_return(job):
    start_grandchild(job)
    return {"done": True}
"""

Target = Callable[[JsonValue], JsonValue]


@pytest.fixture
def workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import a worker module from a folder on both the test's and the child's path."""
    folder = tmp_path / "workers"
    folder.mkdir()
    (folder / "isolation_workers.py").write_text(WORKERS)
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
    (tmp_path / "json.py").write_text("raise SystemExit('the planted json.py ran')\n")
    assert Path.cwd() == tmp_path
    assert run_isolated(target(workers, "done"), {}, 30) == {"done": True}


posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")


def read_pids(pid_file: Path) -> tuple[int, int]:
    """Wait for the worker to write its own and its grandchild's PIDs, and return them."""
    deadline = time.monotonic() + 30
    while not pid_file.exists():
        assert time.monotonic() < deadline, "the worker wrote no PIDs"
        time.sleep(0.05)
    worker, grandchild = pid_file.read_text().split()
    return int(worker), int(grandchild)


def exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until_gone(pid: int) -> None:
    """Poll until `pid` no longer exists. A killed orphan exists until init reaps it."""
    deadline = time.monotonic() + 30
    while exists(pid):
        assert time.monotonic() < deadline, f"process {pid} is still running"
        time.sleep(0.05)


@posix_only
def test_a_timeout_kills_the_processes_the_worker_started(
    workers: ModuleType, tmp_path: Path
) -> None:
    pid_file = tmp_path / "pids"
    job: JsonValue = {"pid_file": str(pid_file), "seconds": 60}
    with pytest.raises(WorkerTimedOutError):
        run_isolated(target(workers, "spawn_and_sleep"), job, 1)
    worker, grandchild = read_pids(pid_file)
    wait_until_gone(grandchild)
    assert not exists(worker)


@posix_only
def test_a_normal_exit_leaves_the_processes_the_worker_started(
    workers: ModuleType, tmp_path: Path
) -> None:
    pid_file = tmp_path / "pids"
    job: JsonValue = {"pid_file": str(pid_file), "seconds": 0}
    assert run_isolated(target(workers, "spawn_and_return"), job, 30) == {"done": True}
    _, grandchild = read_pids(pid_file)
    try:
        time.sleep(0.5)
        assert exists(grandchild)
    finally:
        os.kill(grandchild, signal.SIGKILL)


@posix_only
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
        f"run_isolated(isolation_workers.{worker}, job, None)\n"
    )
    parent = subprocess.Popen([sys.executable, str(script)])
    try:
        worker_pid, grandchild = read_pids(pid_file)
    finally:
        parent.send_signal(signal.SIGKILL)
        parent.wait()
    try:
        wait_until_gone(worker_pid)
        wait_until_gone(grandchild)
    finally:
        for pid in (worker_pid, grandchild):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
