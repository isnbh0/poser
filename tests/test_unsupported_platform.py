"""What a command does on a real platform that lacks a facility poser needs: a file-system backend
or plugin worker isolation.

`tests/test_init_workspace.py` shows the same refusal by simulating a platform on macOS. This file
runs without simulation, so it skips where every facility exists and runs in CI's Windows job.
"""

import sys
from pathlib import Path

import pytest

from poser.adapters.filesystem import native
from poser.adapters.isolation import check_platform
from poser.cli import init_workspace
from poser.core.ports import UnsupportedPlatformError


def missing_facility() -> str | None:
    """Return the wording that names the first missing facility, or None if none is missing."""
    try:
        native()
    except UnsupportedPlatformError:
        return "file-system support"
    try:
        check_platform()
    except UnsupportedPlatformError:
        return "worker isolation"
    return None


MISSING = missing_facility()
if MISSING is None:
    pytest.skip("this platform has every facility poser needs", allow_module_level=True)


def test_init_workspace_names_the_platform_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, plugin, presets = tmp_path / "studio", tmp_path / "Surge XT.vst3", tmp_path / "presets"
    plugin.mkdir()
    presets.mkdir()
    argv = [str(target), "--setup", "surge", "--plugin-path", str(plugin)]
    assert init_workspace.main([*argv, "--presets-path", str(presets)]) == 2
    diagnostic = capsys.readouterr().err
    assert diagnostic.startswith("init-workspace: ")
    assert f"({sys.platform})" in diagnostic
    assert str(MISSING) in diagnostic
    assert "docs/setup.md#prerequisites" in diagnostic
    assert "poser.toml:" not in diagnostic
    for name in ("AGENTS.md", "POSER.md", "CLAUDE.md", "poser.toml", "songs/demo.yaml"):
        assert not (target / name).exists(), name
