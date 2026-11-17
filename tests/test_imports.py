"""Every module of poser and poser-scsynth imports on the platform running the tests.

CI runs this file on Windows, where a module that needs a POSIX-only library when loaded would
fail here instead of in a user's first command.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOTS = (ROOT / "src", ROOT / "packages" / "poser-scsynth" / "src")
PACKAGES = (ROOT / "src" / "poser", ROOT / "packages" / "poser-scsynth" / "src" / "poser_scsynth")

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


def test_every_module_imports() -> None:
    names = modules()
    for expected in ("poser.adapters.filesystem", "poser.compose", "poser_scsynth.instrument"):
        assert expected in names, expected
    result = subprocess.run(
        [sys.executable, "-c", IMPORTER, *names], check=True, capture_output=True, text=True
    )
    assert json.loads(result.stdout) == {}
