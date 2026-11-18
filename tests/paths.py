"""Paths of an exact length, and the longest path a system lets this process create."""

from collections.abc import Callable
from pathlib import Path


def path_of_length(base: Path, total: int) -> Path:
    """Return a path under `base`, absolute, of exactly `total` characters. Each added part is a
    run of `d`s of at most 200 characters, within every file system's limit on a name, and the
    last is at least 100 when there is more than one."""
    path = base.absolute()
    while (room := total - len(str(path)) - 1) > 200:
        path /= "d" * 100
    assert room > 0, f"{base} is too long to reach {total} characters"
    path /= "d" * room
    assert len(str(path)) == total
    return path


def make_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def make_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def longest_created(base: Path, create: Callable[[Path], None], below: int) -> int:
    """The length of the longest path under `base`, shorter than `below`, that `create` makes
    without an OSError, found by bisection; a system that makes one makes every shorter."""
    low, high = len(str(base.absolute())) + 2, below
    path = path_of_length(base, low)
    create(path)
    while high - low > 1:
        middle = (low + high) // 2
        try:
            create(path_of_length(base, middle))
        except OSError:
            high = middle
        else:
            low = middle
    return low
