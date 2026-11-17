"""Facts about paths that hold on every platform: which entries are links, and a walk that stays
inside its tree.

A link is a symbolic link or, on Windows, a directory junction, which `Path.is_symlink` does not
report. Every check in poser for "is this a link" uses `is_link`, and every walk that must not
leave its tree uses `walk`. To ask whether anything at all is at a path, link or not, use
`path.exists(follow_symlinks=False)`.
"""

import os
from collections.abc import Iterator
from pathlib import Path


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
