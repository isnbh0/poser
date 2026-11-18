"""`start_python` starts the interpreter itself, and it is the only way a test starts one it can
kill."""

import ast
import os
import sys
from pathlib import Path

from interpreter import start_python

import poser

TESTS = Path(__file__).parent

REPORT = "import os, sys, poser; print(os.getpid()); print(sys.prefix); print(poser.__file__)"


def test_the_child_is_the_interpreter_with_this_environment() -> None:
    with start_python("-c", REPORT) as child:
        output, _ = child.communicate(timeout=60)
    assert child.returncode == 0
    pid, prefix, module = output.splitlines()
    assert int(pid) == child.pid
    assert os.path.normcase(prefix) == os.path.normcase(sys.prefix)
    assert Path(module) == Path(poser.__file__)


def _starts_this_executable(node: ast.Call) -> bool:
    """Whether `node` is `subprocess.Popen([sys.executable, ...], ...)` or `Popen(...)` alike."""
    function, arguments = node.func, node.args
    name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", None)
    if not (
        name == "Popen" and arguments and isinstance(arguments[0], ast.List) and arguments[0].elts
    ):
        return False
    first = arguments[0].elts[0]
    return (
        isinstance(first, ast.Attribute)
        and first.attr == "executable"
        and isinstance(first.value, ast.Name)
        and first.value.id == "sys"
    )


def popens_of_this_executable(source: str) -> list[int]:
    """The lines of `source` that start a `Popen` of `sys.executable`."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _starts_this_executable(node)
    ]


def test_the_check_finds_a_popen_of_this_executable() -> None:
    assert popens_of_this_executable(
        "import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', 'pass'])\n"
    ) == [2]
    assert popens_of_this_executable(
        "from subprocess import Popen\nimport sys\nPopen([sys.executable, '-c', 'pass'])\n"
    ) == [3]
    assert (
        popens_of_this_executable(
            "import subprocess, sys\nsubprocess.run([sys.executable, '-c', 'pass'])\n"
        )
        == []
    )


def test_no_test_starts_a_popen_of_this_executable() -> None:
    """A `Popen` is a process a test may kill, and on Windows `sys.executable` may be a launcher
    whose child outlives the kill. `subprocess.run` waits for the launcher, which waits for its
    child, so it needs no exception."""
    found = {
        path.name: lines
        for path in sorted(TESTS.glob("*.py"))
        if (lines := popens_of_this_executable(path.read_text(encoding="utf-8")))
    }
    assert found == {}, f"start these with interpreter.start_python: {found}"
