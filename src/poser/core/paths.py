"""Facts about paths that hold on every platform: which entries are links, a walk that stays
inside its tree, and how long a path a platform lets a program create.

A link is a symbolic link or, on Windows, a directory junction, which `Path.is_symlink` does not
report. Every check in poser for "is this a link" uses `is_link`, and every walk that must not
leave its tree uses `walk`. To ask whether anything at all is at a path, link or not, use
`path.exists(follow_symlinks=False)`.
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class PathUnit(Enum):
    """What a platform's path limit counts. The value names it in a message."""

    BYTES = "bytes"
    """The path's bytes in the file-system encoding, which POSIX's `PATH_MAX` counts."""
    UTF16_UNITS = "characters"
    """The path's UTF-16 code units, which Windows counts and calls characters: one for most
    characters, two for one past U+FFFF."""

    def length(self, path: str) -> int:
        """How many of this unit `path` holds."""
        if self is PathUnit.BYTES:
            return len(os.fsencode(path))
        return len(path.encode("utf-16-le", "surrogatepass")) // 2


@dataclass(frozen=True)
class PathLimit:
    """The most a platform lets a program's paths hold, counted in `unit` on the absolute path:
    a directory's and a file's, and what lifts the limit, if anything does, phrased as an
    instruction.

    An exact limit is where the system refuses one unit more. Where the system's limit varies,
    such as by volume, the limit is not exact: it is the least the system allows on any volume,
    so every path within it can be made and a path past it may be refused though the system would
    have made it."""

    directory: int
    file: int
    unit: PathUnit
    lift: str | None = None
    exact: bool = True

    def problem(self, directory: Path, file_name: str, shorten: str) -> str | None:
        """Why the directory `directory`, or the file `file_name` in it, would pass the limit, or
        None when both fit. `shorten` says which path to shorten, phrased as an instruction."""
        absolute = directory.absolute()
        for kind, path, limit in (
            ("directory", absolute, self.directory),
            ("file", absolute / file_name, self.file),
        ):
            if (length := self.unit.length(str(path))) > limit:
                remedy = f"{self.lift}, or {shorten}" if self.lift else shorten
                allows = "this system allows" if self.exact else "this system allows everywhere"
                return (
                    f"its {kind} {path} would be {length} {self.unit.value}, past the {limit} "
                    f"{allows}; to fix it, {remedy}"
                )
        return None


def is_link(path: Path) -> bool:
    """Whether `path` is a symbolic link or a directory junction, without following it. False
    for a missing path."""
    return path.is_symlink() or path.is_junction()


def walk(directory: Path) -> Iterator[Path]:
    """Yield every entry under `directory`, at any depth, entering no link.

    A link is yielded, but not what it leads to. A directory that disappears during the walk is
    skipped; any other error reading a directory is raised. The order is unspecified.
    """
    pending = [directory]
    while pending:
        try:
            with os.scandir(pending.pop()) as listing:
                entries = list(listing)
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            path = Path(entry.path)
            yield path
            if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                pending.append(path)
