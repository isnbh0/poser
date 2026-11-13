"""Checking an instrument's default sound, on fakes."""

import pytest
from fakes import BadOutput, FakeInstrument, FakeSound

from poser.core.checking import CheckResult, check_instrument
from poser.core.checks import CHECK_NOTES, CheckStatus, check_pattern, check_status
from poser.core.names import Identifier
from poser.core.ports import RenderFailure, SoundFailure
from poser.core.statuses import CORE_RENDER_FAILURES, RenderFailureStatus, SoundFailureStatus

NAME = Identifier("synth")


def check(sound: FakeSound | None) -> tuple[FakeInstrument, CheckResult]:
    instrument = FakeInstrument(NAME, {} if sound is None else {None: sound})
    return instrument, check_instrument(instrument, NAME, timeout_s=10.0)


def test_passing_sound_passes() -> None:
    instrument, result = check(FakeSound(amplitude=0.5))

    assert result.status is CheckStatus.PASS
    assert result.name == NAME
    assert result.adapter == "fake"
    assert result.repeatable is True
    assert result.clipped is False
    assert result.note_peaks_dbfs is not None
    assert len(result.note_peaks_dbfs) == len(CHECK_NOTES)
    assert result.detail is None
    assert [request.performance for request in instrument.renders] == [check_pattern()] * 2
    assert len({request.audio_path for request in instrument.renders}) == 2
    assert not [request.audio_path for request in instrument.renders if request.audio_path.exists()]


def test_silent_sound_is_silent() -> None:
    _, result = check(FakeSound(amplitude=0.0))
    assert result.status is CheckStatus.SILENT


def test_sound_that_differs_on_the_second_render_is_not_repeatable() -> None:
    _, result = check(FakeSound(amplitude=0.5, drift=0.01))

    assert result.status is CheckStatus.PASS
    assert result.repeatable is False
    assert result.max_difference_dbfs == pytest.approx(-40.0, abs=0.01)


def test_nan_output_is_non_finite() -> None:
    _, result = check(FakeSound(bad_output="nan"))

    assert result.status is CheckStatus.NON_FINITE
    assert result.detail == "audio contains NaN or infinity"


@pytest.mark.parametrize("status", sorted(set(RenderFailureStatus) - CORE_RENDER_FAILURES))
def test_render_failure_gives_its_check_status(status: RenderFailureStatus) -> None:
    _, result = check(FakeSound(render_failure=RenderFailure(status, "the worker failed")))

    assert result.status is check_status(status)
    assert result.detail == "the worker failed"
    assert result.peak_dbfs is None


@pytest.mark.parametrize("status", list(SoundFailureStatus))
def test_sound_failure_gives_its_check_status(status: SoundFailureStatus) -> None:
    instrument, result = check(FakeSound(sound_failure=SoundFailure(status, "no state")))

    assert result.status is check_status(status)
    assert result.detail == "default sound did not resolve: no state"
    assert instrument.renders == []


def test_missing_default_sound_is_load_failed() -> None:
    _, result = check(None)

    assert result.status is CheckStatus.LOAD_FAILED
    assert result.detail == "default sound did not resolve: no default sound"


@pytest.mark.parametrize("bad", ["wrong_length", "no_file"])
def test_unusable_output_is_crashed(bad: BadOutput) -> None:
    _, result = check(FakeSound(bad_output=bad))

    assert result.status is CheckStatus.CRASHED
    assert result.detail is not None
    assert result.detail.startswith("instrument wrote unusable audio: ")


def test_unsupported_pitch_is_a_crash_of_the_check() -> None:
    assert check_status(RenderFailureStatus.UNSUPPORTED_PITCH) is CheckStatus.CRASHED
