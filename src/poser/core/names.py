"""Names: text values whose constructors check their form."""

import re
from typing import ClassVar, Self


class TrackName(str):
    """A track's name, safe as a file name."""

    __slots__ = ()

    PATTERN: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]+")

    def __new__(cls, value: str) -> Self:
        if not (isinstance(value, str) and cls.PATTERN.fullmatch(value)):
            raise ValueError(f"{cls.__name__} must match {cls.PATTERN.pattern}, got {value!r}")
        return super().__new__(cls, value)


class Identifier(str):
    """An instrument, preset source, saved sound, section, or defect name."""

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
    """A song's relative POSIX path, such as `songs/demo.yaml`."""

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
        return super().__new__(cls, value)
