"""Checking an instrument's default sound, on fakes."""

import pytest
from fakes import BadOutput, FakeInstrument, FakeSound

from poser.core.checking import CheckResult, check_instrument
from poser.core.checks import (
    CHECK_NOTES,
    CheckAnalysis,
    CheckFailure,
    CheckFailureStatus,
    MeasuredStatus,
    check_pattern,
    check_status,
)
from poser.core.names import Identifier
from poser.core.ports import RenderFailure, SoundFailure
from poser.core.statuses import CORE_RENDER_FAILURES, RenderFailureStatus, SoundFailureStatus

NAME = Identifier("synth")


def check(sound: FakeSound | None) -> tuple[FakeInstrument, CheckResult]:
    instrument = FakeInstrument(NAME, {} if sound is None else {None: sound})
    return instrument, check_instrument(instrument, NAME, timeout_s=10.0)


def analysis(result: CheckResult) -> CheckAnalysis:
    """The check's measurements, which it must have."""
    assert isinstance(result.outcome, CheckAnalysis), result
    return result.outcome


def failure(result: CheckResult) -> CheckFailure:
    """The check's failure, which it must have."""
    assert isinstance(result.outcome, CheckFailure), result
    return result.outcome


def test_passing_sound_passes() -> None:
    instrument, result = check(FakeSound(amplitude=0.5))

    measured = analysis(result)
    assert measured.status is MeasuredStatus.PASS
    assert result.status is MeasuredStatus.PASS
    assert result.name == NAME
    assert result.adapter == "fake"
    assert measured.repeatable is True
    assert measured.clipped is False
    assert len(measured.note_peaks_dbfs) == len(CHECK_NOTES)
    assert [request.performance for request in instrument.renders] == [check_pattern()] * 2
    assert len({request.audio_path for request in instrument.renders}) == 2
    assert not [request.audio_path for request in instrument.renders if request.audio_path.exists()]


def test_silent_sound_is_silent() -> None:
    _, result = check(FakeSound(amplitude=0.0))
    assert analysis(result).status is MeasuredStatus.SILENT


def test_sound_that_differs_on_the_second_render_is_not_repeatable() -> None:
    _, result = check(FakeSound(amplitude=0.5, drift=0.01))

    measured = analysis(result)
    assert measured.status is MeasuredStatus.PASS
    assert measured.repeatable is False
    assert measured.max_difference_dbfs == pytest.approx(-40.0, abs=0.01)


def test_nan_output_is_non_finite() -> None:
    _, result = check(FakeSound(bad_output="nan"))

    assert result.outcome == CheckFailure(
        CheckFailureStatus.NON_FINITE, "audio contains NaN or infinity"
    )


@pytest.mark.parametrize("status", sorted(set(RenderFailureStatus) - CORE_RENDER_FAILURES))
def test_render_failure_gives_its_check_status(status: RenderFailureStatus) -> None:
    _, result = check(FakeSound(render_failure=RenderFailure(status, "the worker failed")))

    assert result.outcome == CheckFailure(check_status(status), "the worker failed")


@pytest.mark.parametrize("status", list(SoundFailureStatus))
def test_sound_failure_gives_its_check_status(status: SoundFailureStatus) -> None:
    instrument, result = check(FakeSound(sound_failure=SoundFailure(status, "no state")))

    assert result.outcome == CheckFailure(
        check_status(status), "default sound did not resolve: no state"
    )
    assert instrument.renders == []


def test_missing_default_sound_is_load_failed() -> None:
    _, result = check(None)

    assert result.outcome == CheckFailure(
        CheckFailureStatus.LOAD_FAILED, "default sound did not resolve: no default sound"
    )


@pytest.mark.parametrize("bad", ["wrong_length", "no_file"])
def test_unusable_output_is_crashed(bad: BadOutput) -> None:
    _, result = check(FakeSound(bad_output=bad))

    failed = failure(result)
    assert failed.status is CheckFailureStatus.CRASHED
    assert failed.detail.startswith("instrument wrote unusable audio: ")


def test_a_late_start_is_a_crash_of_the_check() -> None:
    assert check_status(RenderFailureStatus.LATE_START) is CheckFailureStatus.CRASHED


def test_check_outcomes_refuse_the_other_path_status() -> None:
    with pytest.raises(ValueError, match="CheckFailureStatus"):
        CheckFailure(MeasuredStatus.SILENT, "no")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="non-empty"):
        CheckFailure(CheckFailureStatus.CRASHED, "")
    with pytest.raises(ValueError, match="MeasuredStatus"):
        CheckAnalysis(
            CheckFailureStatus.CRASHED,  # ty: ignore[invalid-argument-type]
            -6.0,
            (-6.0,),
            clipped=False,
            repeatable=True,
            max_difference_dbfs=-120.0,
        )
