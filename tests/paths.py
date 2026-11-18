"""Paths of an exact length, and the longest path a system lets this process create."""

from collections.abc import Callable
from pathlib import Path


def path_of_length(base: Path, total: int, fill: str = "d") -> Path:
    """Return a path under `base`, absolute, of exactly `total` characters, adding parts that
    repeat the character `fill`. Each part holds at most 200 bytes in UTF-8, and so at most 200
    UTF-16 code units, within every file system's limit on a name; the last holds at least 100
    when there is more than one."""
    path = base.absolute()
    part = 100 // len(fill.encode("utf-8"))
    while (room := total - len(str(path)) - 1) > 2 * part:
        path /= fill * part
    assert room > 0, f"{base} is too long to reach {total} characters"
    path /= fill * room
    assert len(str(path)) == total
    return path


def make_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def make_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def longest_created(base: Path, create: Callable[[Path], None], below: int, fill: str = "d") -> int:
    """The length, in characters, of the longest `path_of_length(base, length, fill)` shorter
    than `below` that `create` makes, found by bisection; a system that makes one makes every
    shorter. The system refuses with an OSError, or, on Windows past 32,767 characters, Python
    refuses with a ValueError."""
    low, high = len(str(base.absolute())) + 2, below
    create(path_of_length(base, low, fill))
    while high - low > 1:
        middle = (low + high) // 2
        try:
            create(path_of_length(base, middle, fill))
        except (OSError, ValueError):
            high = middle
        else:
            low = middle
    return low
