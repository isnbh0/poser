"""Runs the instrument check on the instruments in the root poser.toml. Deselected by default."""

import math
from pathlib import Path

import pytest
from helpers import real_composition

from poser.cli.common import RENDER_TIMEOUT_S
from poser.core.checking import check_instrument
from poser.core.checks import CheckAnalysis, CheckFailureStatus, MeasuredStatus


@pytest.mark.real_plugins
def test_each_instrument_returns_a_known_status_with_finite_numbers(tmp_path: Path) -> None:
    instruments = real_composition(tmp_path).instruments()

    results = [
        check_instrument(instrument, name, RENDER_TIMEOUT_S)
        for name, instrument in instruments.items()
    ]

    assert [result.name for result in results] == list(instruments)
    for result in results:
        outcome = result.outcome
        if isinstance(outcome, CheckAnalysis):
            assert outcome.status in MeasuredStatus
            numbers = [outcome.peak_dbfs, outcome.max_difference_dbfs, *outcome.note_peaks_dbfs]
            assert all(math.isfinite(number) for number in numbers)
        else:
            assert outcome.status in CheckFailureStatus
