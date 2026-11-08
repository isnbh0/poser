"""The instrument check: the pattern an instrument plays, and how its renders are judged."""

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

import numpy as np

from poser.core.audio import SILENCE_DBFS, Audio, measure_peak_dbfs, to_dbfs
from poser.core.performance import NoteOff, NoteOn, Performance
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus

CHECK_SAMPLES = 240000
CHECK_NOTES = (48, 60, 72, 84)
CHECK_VELOCITY = 100
_FIRST_NOTE = 24000
_NOTE_SPACING = 48000
_NOTE_LENGTH = 24000


def check_pattern() -> Performance:
    """Return the check pattern: each note in turn, half a second long, a second apart."""
    events: list[NoteOn | NoteOff] = []
    for index, pitch in enumerate(CHECK_NOTES):
        start = _FIRST_NOTE + index * _NOTE_SPACING
        events.append(NoteOn(start, pitch, CHECK_VELOCITY))
        events.append(NoteOff(start + _NOTE_LENGTH, pitch))
    return Performance(CHECK_SAMPLES, tuple(events))


class CheckStatus(StrEnum):
    TIMED_OUT = "timed_out"
    CRASHED = "crashed"
    LOAD_FAILED = "load_failed"
    NON_FINITE = "non_finite"
    SILENT = "silent"
    MISSING_NOTES = "missing_notes"
    PASS = "pass"  # noqa: S105 -- a check status, not a password.


def check_status(status: SoundFailureStatus | RenderFailureStatus) -> CheckStatus:
    """Return the check status for an instrument that could not resolve a sound or render."""
    match status:
        case SoundFailureStatus.UNKNOWN_SOUND:
            return CheckStatus.LOAD_FAILED
        case SoundFailureStatus.LOAD_FAILED:
            return CheckStatus.LOAD_FAILED
        case RenderFailureStatus.LOAD_FAILED:
            return CheckStatus.LOAD_FAILED
        case RenderFailureStatus.CRASHED:
            return CheckStatus.CRASHED
        case RenderFailureStatus.TIMED_OUT:
            return CheckStatus.TIMED_OUT
        case _:
            assert_never(status)


@dataclass(frozen=True)
class CheckAnalysis:
    status: CheckStatus
    peak_dbfs: float
    note_peaks_dbfs: tuple[float, ...]
    clipped: bool
    repeatable: bool
    max_difference_dbfs: float


def _note_windows() -> list[tuple[int, int]]:
    """Return each note's span in samples, from its note-on to its note-off."""
    events = check_pattern().events
    windows = []
    for position, event in enumerate(events):
        if isinstance(event, NoteOn):
            end = next(
                e.sample
                for e in events[position + 1 :]
                if isinstance(e, NoteOff) and e.pitch == event.pitch
            )
            windows.append((event.sample, end))
    return windows


def _note_peaks_dbfs(render: np.ndarray) -> list[float]:
    return [measure_peak_dbfs(render[..., start:stop]) for start, stop in _note_windows()]


def analyze_check(first: Audio, second: Audio) -> CheckAnalysis:
    """Measure two renders of the check pattern.

    Peaks report the quieter of the two renders, so a plugin that goes silent on reload is caught.
    """
    first_samples = np.asarray(first.samples, dtype=np.float64)
    second_samples = np.asarray(second.samples, dtype=np.float64)
    difference = np.abs(first_samples - second_samples).max(initial=0.0)

    peak_dbfs = min(measure_peak_dbfs(first_samples), measure_peak_dbfs(second_samples))
    note_peaks_dbfs = tuple(
        min(a, b)
        for a, b in zip(
            _note_peaks_dbfs(first_samples), _note_peaks_dbfs(second_samples), strict=True
        )
    )

    if peak_dbfs < SILENCE_DBFS:
        status = CheckStatus.SILENT
    elif min(note_peaks_dbfs) < SILENCE_DBFS:
        status = CheckStatus.MISSING_NOTES
    else:
        status = CheckStatus.PASS

    return CheckAnalysis(
        status=status,
        peak_dbfs=peak_dbfs,
        note_peaks_dbfs=note_peaks_dbfs,
        clipped=bool((np.abs(first_samples) >= 1.0).any() or (np.abs(second_samples) >= 1.0).any()),
        repeatable=bool(np.array_equal(first_samples, second_samples)),
        max_difference_dbfs=to_dbfs(float(difference)),
    )
