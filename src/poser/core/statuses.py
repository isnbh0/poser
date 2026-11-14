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
    # Every render attempt's sound began late after the first note, not all by the same amount.
    LATE_START = "late_start"


class SkipStatus(StrEnum):
    """The core did not ask an instrument to render the node."""

    # The track has a fractional pitch, and its instrument plays only whole pitches.
    UNSUPPORTED_PITCH = "unsupported_pitch"
    UPSTREAM_FAILED = "upstream_failed"


# The render failures only the core decides; an adapter's `RenderFailure` cannot carry them.
CORE_RENDER_FAILURES = frozenset({RenderFailureStatus.LATE_START})


type RenderStatus = StemStatus | SoundFailureStatus | RenderFailureStatus | SkipStatus

# Every distinct render status value, in the order above.
RENDER_STATUSES: tuple[str, ...] = tuple(
    dict.fromkeys(
        str(status)
        for enum in (StemStatus, SoundFailureStatus, RenderFailureStatus, SkipStatus)
        for status in enum
    )
)


class UnrenderedStatus(StrEnum):
    """Why analysis has no audio for a node the plan keys."""

    # No stem exists under the node's current key.
    NOT_RENDERED = "not_rendered"
    # A stem exists under the key, and its audio cannot be read.
    UNREADABLE_STEM = "unreadable_stem"
