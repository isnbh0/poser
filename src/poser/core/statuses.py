"""Statuses: one exact set for each place a track's render status can come from."""

from enum import StrEnum


class StemStatus(StrEnum):
    """The core wrote a stem."""

    RENDERED = "rendered"
    SILENT = "silent"


class SoundFailureStatus(StrEnum):
    """An instrument could not resolve a sound."""

    UNKNOWN_SOUND = "unknown_sound"
    LOAD_FAILED = "load_failed"


class RenderFailureStatus(StrEnum):
    """An instrument could not render."""

    LOAD_FAILED = "load_failed"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"


type RenderStatus = StemStatus | SoundFailureStatus | RenderFailureStatus

# Every distinct render status value, in the order above.
RENDER_STATUSES: tuple[str, ...] = tuple(
    dict.fromkeys(
        str(status)
        for enum in (StemStatus, SoundFailureStatus, RenderFailureStatus)
        for status in enum
    )
)
