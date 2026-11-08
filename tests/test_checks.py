"""Tests of the instrument check: its pattern and how its renders are judged."""

import numpy as np
import pytest

from poser.core.audio import FLOOR_DBFS, NUM_CHANNELS, Audio
from poser.core.checks import (
    CHECK_NOTES,
    CHECK_SAMPLES,
    CHECK_VELOCITY,
    CheckAnalysis,
    CheckStatus,
    analyze_check,
    check_pattern,
)
from poser.core.performance import NoteOff, NoteOn

NOTE_ONS = (24000, 72000, 120000, 168000)
NOTE_LENGTH = 24000


def note_window(index: int) -> slice:
    return slice(NOTE_ONS[index], NOTE_ONS[index] + NOTE_LENGTH)


def tone_render(level: float = 0.5, skip_note: int | None = None) -> np.ndarray:
    render = np.zeros((NUM_CHANNELS, CHECK_SAMPLES), dtype=np.float32)
    for index in range(len(CHECK_NOTES)):
        if index != skip_note:
            render[..., note_window(index)] = level
    return render


def check(first: np.ndarray, second: np.ndarray) -> CheckAnalysis:
    return analyze_check(Audio(first), Audio(second))


def test_check_pattern_plays_four_half_second_notes() -> None:
    pattern = check_pattern()
    assert pattern.samples == 240000
    note_ons = [event for event in pattern.events if isinstance(event, NoteOn)]
    note_offs = [event for event in pattern.events if isinstance(event, NoteOff)]
    assert [(e.sample, e.pitch, e.velocity) for e in note_ons] == [
        (sample, pitch, CHECK_VELOCITY) for sample, pitch in zip(NOTE_ONS, CHECK_NOTES, strict=True)
    ]
    assert [(e.sample, e.pitch) for e in note_offs] == [
        (sample + NOTE_LENGTH, pitch) for sample, pitch in zip(NOTE_ONS, CHECK_NOTES, strict=True)
    ]


def test_tone_in_all_note_windows_passes() -> None:
    result = check(tone_render(), tone_render())
    assert result.status is CheckStatus.PASS
    assert result.peak_dbfs == pytest.approx(-6.02, abs=0.01)
    assert result.note_peaks_dbfs == pytest.approx((-6.02,) * 4, abs=0.01)
    assert isinstance(result.note_peaks_dbfs, tuple)


def test_all_zero_render_is_silent() -> None:
    silence = np.zeros((NUM_CHANNELS, CHECK_SAMPLES), dtype=np.float32)
    result = check(silence, silence)
    assert result.status is CheckStatus.SILENT
    assert result.peak_dbfs == FLOOR_DBFS


def test_silent_second_render_is_silent() -> None:
    silence = np.zeros((NUM_CHANNELS, CHECK_SAMPLES), dtype=np.float32)
    assert check(tone_render(), silence).status is CheckStatus.SILENT


def test_empty_note_window_is_missing_notes() -> None:
    render = tone_render(skip_note=2)
    result = check(render, render)
    assert result.status is CheckStatus.MISSING_NOTES
    assert result.note_peaks_dbfs[2] == FLOOR_DBFS
    assert result.note_peaks_dbfs[1] == pytest.approx(-6.02, abs=0.01)


def test_identical_renders_are_repeatable() -> None:
    result = check(tone_render(), tone_render())
    assert result.repeatable is True
    assert result.max_difference_dbfs == FLOOR_DBFS


def test_differing_renders_are_reported_without_failing() -> None:
    second = tone_render()
    second[0, note_window(0).start] += 0.1
    result = check(tone_render(), second)
    assert result.repeatable is False
    assert result.max_difference_dbfs == pytest.approx(-20.0, abs=0.01)
    assert result.status is CheckStatus.PASS


def test_full_scale_sample_is_clipped_without_failing() -> None:
    render = tone_render()
    render[0, note_window(1).start] = 1.0
    result = check(render, render)
    assert result.clipped is True
    assert result.status is CheckStatus.PASS


def test_clean_render_is_not_clipped() -> None:
    assert check(tone_render(), tone_render()).clipped is False
