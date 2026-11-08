"""Fixtures shared by every test file."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _work_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test in its own directory, so no command uses the instruments in the repository's
    poser.toml."""
    monkeypatch.chdir(tmp_path)
