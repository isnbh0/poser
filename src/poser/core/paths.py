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
from pathlib import Path


@dataclass(frozen=True)
class PathLimit:
    """The most characters a platform lets a program's paths hold, counted on the absolute path:
    a directory's and a file's, and what lifts the limit, if anything does, phrased as an
    instruction."""

    directory: int
    file: int
    lift: str | None = None

    def problem(self, directory: Path, file_name: str, shorten: str) -> str | None:
        """Why the directory `directory`, or the file `file_name` in it, would pass the limit, or
        None when both fit. `shorten` says which path to shorten, phrased as an instruction."""
        absolute = directory.absolute()
        for kind, path, limit in (
            ("directory", absolute, self.directory),
            ("file", absolute / file_name, self.file),
        ):
            if len(str(path)) > limit:
                remedy = f"{self.lift}, or {shorten}" if self.lift else shorten
                return (
                    f"its {kind} {path} would be {len(str(path))} characters, past the {limit} "
                    f"this system allows; to fix it, {remedy}"
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
