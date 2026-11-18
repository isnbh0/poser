"""Fixtures shared by every test file."""

from pathlib import Path

import pytest
from hosts import isolated_host

from poser.reference_setups.host import Host


@pytest.fixture(autouse=True)
def _work_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each test in its own directory, so no command uses the instruments in the repository's
    poser.toml."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _search_an_isolated_host(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Point every command's reference setup search at an empty host, so an instrument installed
    on the machine changes no plugin-free test. Tests that load real plugins search the machine."""
    if request.node.get_closest_marker("real_plugins") is None:
        host = isolated_host(tmp_path_factory.mktemp("host"))
        monkeypatch.setattr(Host, "current", staticmethod(lambda: host))
