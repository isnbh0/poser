"""Names: text values whose constructors check their form."""

import re
from pathlib import PurePath, PureWindowsPath
from typing import ClassVar, Self


def is_inner_relative(path: PurePath) -> bool:
    """Whether `path`, joined to a directory, names something inside it: it has parts, no anchor
    (a drive, a root, or both), and no `..` part.

    `is_absolute()` is not enough: on Windows, a rooted path such as `/etc/passwd` and a
    drive-relative one such as `C:x` are not absolute, and joining either leaves the directory.
    """
    return bool(path.parts) and not path.anchor and ".." not in path.parts


class NodeName(str):
    """A graph node's name, safe as a file name: a track's name, or a chain instance's, which
    names its track and recipe with dots between, such as `bass.drive` or `bass.drive.2`."""

    __slots__ = ()

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*")

    def __new__(cls, value: str) -> Self:
        if not (isinstance(value, str) and cls.PATTERN.fullmatch(value)):
            raise ValueError(f"{cls.__name__} must match {cls.PATTERN.pattern}, got {value!r}")
        return super().__new__(cls, value)


class TrackName(NodeName):
    """A track's name, safe as a file name. A track's own name never holds a dot; only compiled
    chain nodes do, so a track's name can never collide with a chain instance's."""

    __slots__ = ()

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+")


class Identifier(str):
    """An instrument, preset source, saved sound, or defect name."""

    __slots__ = ()

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")

    def __new__(cls, value: str) -> Self:
        if not (isinstance(value, str) and cls.PATTERN.fullmatch(value)):
            raise ValueError(f"{cls.__name__} must match {cls.PATTERN.pattern}, got {value!r}")
        return super().__new__(cls, value)


class Suffix(str):
    """A file suffix with its dot, in lowercase, such as `.yaml`."""

    __slots__ = ()

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"\.[a-z0-9]+")

    def __new__(cls, value: str) -> Self:
        if not (isinstance(value, str) and cls.PATTERN.fullmatch(value)):
            raise ValueError(f"{cls.__name__} must match {cls.PATTERN.pattern}, got {value!r}")
        return super().__new__(cls, value)


class SongId(str):
    """A song's relative POSIX path, such as `songs/demo.yaml`. It names a path inside the
    workspace under both POSIX and Windows rules, so the same id is valid on every platform."""

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        if not (isinstance(value, str) and value):
            raise ValueError(f"{cls.__name__} must be a non-empty path, got {value!r}")
        if value.startswith("/") or "\\" in value:
            raise ValueError(
                f"{cls.__name__} must be a relative POSIX path without '\\', got {value!r}"
            )
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError(
                f"{cls.__name__} must not have an empty, '.', or '..' part, got {value!r}"
            )
        # What remains is a drive, such as `C:x`, which Windows reads as outside the workspace.
        if not is_inner_relative(PureWindowsPath(value)):
            raise ValueError(f"{cls.__name__} must not start with a drive, got {value!r}")
        return super().__new__(cls, value)
