"""Hosts for the reference setup search, each with every location under a test's folder."""

import sys
from pathlib import Path

from poser.reference_setups.host import Host


def isolated_host(root: Path) -> Host:
    """A host for this platform whose every searched location is under `root`, so a search
    finds only what a test puts there."""
    environ = {
        "CommonProgramFiles": str(root / "Common Files"),
        "LOCALAPPDATA": str(root / "Local"),
        "ProgramData": str(root / "ProgramData"),
    }
    return Host(sys.platform, environ, root / "home", root / "Library")


def windows_host(root: Path, **variables: Path) -> Host:
    """A Windows host with exactly the environment variables given."""
    environ = {name: str(value) for name, value in variables.items()}
    return Host("win32", environ, root / "home", root / "Library")


def mac_host(root: Path, **variables: Path) -> Host:
    """A macOS host with exactly the environment variables given, its home at `root/home` and
    its system-wide library at `root/Library`."""
    environ = {name: str(value) for name, value in variables.items()}
    return Host("darwin", environ, root / "home", root / "Library")
