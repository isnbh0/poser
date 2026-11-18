"""The machine a reference setup searches for its instrument."""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Host:
    """What a reference setup's search reads from the machine: the platform (`sys.platform`), the
    environment, the user's home folder, and the system-wide library folder (`/Library` on macOS).
    A search is a function of this value; only a command reads the real machine, through
    `current`."""

    platform: str
    environ: Mapping[str, str]
    home: Path
    library: Path

    def path(self, name: str) -> Path | None:
        """Return the path the environment variable `name` holds, or None when it is unset or
        empty."""
        value = self.environ.get(name)
        return Path(value) if value else None

    @staticmethod
    def current() -> "Host":
        """Return this machine. The environment is `os.environ` itself, whose names Windows
        matches without regard to case."""
        return Host(sys.platform, os.environ, Path.home(), Path("/Library"))
