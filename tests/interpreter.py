"""The Python interpreter a test starts as a child process.

On Windows, a virtual environment's `python.exe` is a launcher that starts the base interpreter
as its own child, so a `Popen` of `sys.executable` names the launcher: killing it leaves the
interpreter, and any lock it holds, running. `start_python` starts the base interpreter itself, as
`multiprocessing` does, with `__PYVENV_LAUNCHER__` naming the environment so the child still
imports the environment's packages. Elsewhere `sys.executable` is the interpreter itself.
"""

import os
import subprocess
import sys
from pathlib import Path

# Typeshed omits this attribute, which `venv` and `multiprocessing` read.
BASE_PYTHON = Path(sys._base_executable)  # ty: ignore[unresolved-attribute]
"""The interpreter a Windows virtual environment's launcher starts; `sys.executable` outside an
environment, and on macOS and Linux the interpreter an environment's link names."""

LAUNCHED = sys.platform == "win32" and os.path.normcase(sys.executable) != os.path.normcase(
    BASE_PYTHON
)
"""Whether `sys.executable` is a launcher that starts `BASE_PYTHON` as its child."""


def start_python(*args: str) -> subprocess.Popen[str]:
    """Start this environment's interpreter on `args` as a direct child, so the `Popen`'s PID is
    the interpreter's and `kill()` ends it. Its standard output is a UTF-8 text pipe."""
    if LAUNCHED:
        command = [str(BASE_PYTHON), *args]
        environment = os.environ | {"__PYVENV_LAUNCHER__": sys.executable}
    else:
        command, environment = [sys.executable, *args], None
    return subprocess.Popen(
        command, env=environment, stdout=subprocess.PIPE, text=True, encoding="utf-8"
    )
