"""Tests for running a function in a child process."""

import importlib
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
import os
import sys
import time


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

    monkeypatch.setattr(subprocess, "run", refuse_processes)
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
