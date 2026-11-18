"""Names: text values whose constructors check their form."""

import re
from pathlib import PurePath
from typing import ClassVar, Self

# The device names Windows reserves in every folder, alone or before an extension, compared
# without regard to case: https://learn.microsoft.com/windows/win32/fileio/naming-a-file
WINDOWS_DEVICE_NAMES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"{device}{digit}" for device in ("com", "lpt") for digit in "123456789\u00b9\u00b2\u00b3"]
)
WINDOWS_FORBIDDEN = frozenset('<>:"/\\|?*') | {chr(code) for code in range(32)}


def name_problem(name: str) -> str | None:
    """Why `name`, one path component, would not name the same file on Windows as on macOS, or
    None when it would. Windows forbids some characters, reserves device names such as `nul`, even
    as `nul.txt`, and trims a trailing dot or space, so the name would fail or name another file.
    """
    if forbidden := sorted(set(name) & WINDOWS_FORBIDDEN):
        return f"Windows forbids {', '.join(map(repr, forbidden))} in a name"
    if name.split(".", 1)[0].rstrip(" ").lower() in WINDOWS_DEVICE_NAMES:
        return "Windows reserves it as a device name"
    if name.endswith((".", " ")):
        return "Windows drops a trailing dot or space"
    return None


def inner_relative_problem(path: PurePath) -> str | None:
    """Why `path`, joined to a directory, might not name the same thing inside it on every
    platform, or None when it does: it needs parts, no anchor (a drive, a root, or both), no `..`
    part, and no part with a `name_problem`.

    `is_absolute()` is not enough: on Windows, a rooted path such as `/etc/passwd` and a
    drive-relative one such as `C:x` are not absolute, and joining either leaves the directory.
    """
    if not path.parts:
        return "it names the directory itself"
    if path.anchor:
        return f"it starts with {path.anchor!r}"
    if ".." in path.parts:
        return "it has a '..' part"
    for part in path.parts:
        if problem := name_problem(part):
            return f"{part!r}: {problem}"
    return None


def is_inner_relative(path: PurePath) -> bool:
    """Whether `path` has no `inner_relative_problem`."""
    return inner_relative_problem(path) is None


class _FileName(str):
    """A name poser also uses as a file or folder name, so it must name the same file on every
    platform. `problem` is the one check; the constructor and every validator ask it."""

    __slots__ = ()

    _PATTERN: ClassVar[re.Pattern[str]]
    FORM: ClassVar[str]
    """What the name may hold, phrased to follow "must"."""

    @classmethod
    def problem(cls, value: object) -> str | None:
        """Why `value` is not such a name, phrased to follow its subject, or None when it is."""
        if not (isinstance(value, str) and cls._PATTERN.fullmatch(value)):
            return f"must {cls.FORM}, got {value!r}"
        if problem := name_problem(value):
            return f"must not be {value!r}: {problem}"
        return None

    def __new__(cls, value: str) -> Self:
        if (problem := cls.problem(value)) is not None:
            raise ValueError(f"{cls.__name__} {problem}")
        return super().__new__(cls, value)


class NodeName(_FileName):
    """A graph node's name: a track's name, or a chain instance's, which names its track and
    recipe with dots between, such as `bass.drive` or `bass.drive.2`."""

    __slots__ = ()

    _PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*")
    FORM: ClassVar[str] = "contain only letters, digits, '_', '-', and '.'"


class TrackName(NodeName):
    """A track's name. A track's own name never holds a dot; only compiled chain nodes do, so a
    track's name can never collide with a chain instance's."""

    __slots__ = ()

    _PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+")
    FORM: ClassVar[str] = "contain only letters, digits, '_', and '-'"


class Identifier(_FileName):
    """An instrument, preset source, saved sound, effect recipe, or defect name."""

    __slots__ = ()

    _PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
    FORM: ClassVar[str] = (
        "start with a letter or digit and contain only letters, digits, '_', and '-'"
    )


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
        if value.startswith("/"):
            raise ValueError(f"{cls.__name__} must be a relative POSIX path, got {value!r}")
        # The parts' rules leave no Windows anchor: a drive needs ':', and a root '\\'.
        for part in value.split("/"):
            if part in {"", ".", ".."}:
                raise ValueError(
                    f"{cls.__name__} must not have an empty, '.', or '..' part, got {value!r}"
                )
            if problem := name_problem(part):
                raise ValueError(f"{cls.__name__} {value!r} names {part!r}: {problem}")
        return super().__new__(cls, value)
