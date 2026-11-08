"""Fixtures shared by every test file."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _work_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test in its own directory, so no command finds the repository's plugin list."""
    monkeypatch.chdir(tmp_path)
