"""Every module of poser and poser-scsynth imports on the platform running the tests, except the
file-system backends `native()` does not select there and, off Windows, worker isolation's
Windows form.

CI runs this file on Windows, where a module that needs a POSIX-only library when loaded would
fail here instead of in a user's first command. A backend exists to use one platform's library,
so it imports only where poser selects it.
"""

import json
import subprocess
import sys
from pathlib import Path

from poser.adapters.filesystem import native
from poser.core.ports import UnsupportedPlatformError

ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOTS = (ROOT / "src", ROOT / "packages" / "poser-scsynth" / "src")
PACKAGES = (ROOT / "src" / "poser", ROOT / "packages" / "poser-scsynth" / "src" / "poser_scsynth")
BACKEND_PREFIX = "poser.adapters.filesystem_"
WINDOWS_ONLY = frozenset({"poser.adapters.isolation_windows"})

# Imports each module named in argv in this child process, so the libraries they load stay out of
# the test process, and prints the failures as JSON.
IMPORTER = """
import importlib, json, sys
failures = {}
for name in sys.argv[1:]:
    try:
        importlib.import_module(name)
    except Exception as error:
        failures[name] = f"{type(error).__name__}: {error}"
print(json.dumps(failures))
"""


def module_name(path: Path, source_root: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def modules() -> list[str]:
    return sorted(
        module_name(path, source_root)
        for source_root, package in zip(SOURCE_ROOTS, PACKAGES, strict=True)
        for path in package.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def selected_backend() -> str | None:
    """Return the module of the backend `native()` selects here, or None if it selects none."""
    try:
        return type(native()).__module__
    except UnsupportedPlatformError:
        return None


def test_every_module_imports() -> None:
    backend = selected_backend()
    skipped = set() if sys.platform == "win32" else WINDOWS_ONLY
    names = [
        name
        for name in modules()
        if (not name.startswith(BACKEND_PREFIX) or name == backend) and name not in skipped
    ]
    expected = ["poser.adapters.filesystem", "poser.compose", "poser_scsynth.instrument"]
    if sys.platform == "win32":
        expected += sorted(WINDOWS_ONLY)
    for name in [*expected, *([backend] if backend else [])]:
        assert name in names, name
    result = subprocess.run(
        [sys.executable, "-c", IMPORTER, *names],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == {}
