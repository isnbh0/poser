"""Runs the check on the plugins installed on this machine. Deselected by default."""

import math

import pytest

from poser import plugin_check
from poser.plugin_check import Status


@pytest.mark.real_plugins
def test_each_listed_plugin_returns_a_known_status_with_finite_numbers() -> None:
    specs = plugin_check.load_config(plugin_check.DEFAULT_CONFIG)

    results = plugin_check.run_check(specs)

    assert [result.name for result in results] == [spec.name for spec in specs]
    for result in results:
        assert result.status in Status
        numbers = [result.peak_dbfs, result.max_difference_dbfs, *result.note_peaks_dbfs]
        assert all(math.isfinite(number) for number in numbers if number is not None)
