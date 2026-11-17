"""What a command does on a real platform the file-system facility has no backend for.

`tests/test_init_workspace.py` shows the same refusal by simulating `win32` on macOS. This file
runs without simulation, so it skips where a backend exists and runs in CI's Windows job.
"""

import sys
from pathlib import Path

import pytest

from poser.adapters.filesystem import native
from poser.cli import init_workspace
from poser.core.ports import UnsupportedPlatformError

try:
    native()
except UnsupportedPlatformError:
    pass
else:
    pytest.skip("this platform has a file-system backend", allow_module_level=True)


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
    assert sys.platform in diagnostic
    assert "docs/setup.md#prerequisites" in diagnostic
    assert "poser.toml:" not in diagnostic
    for name in ("AGENTS.md", "POSER.md", "CLAUDE.md", "poser.toml", "songs/demo.yaml"):
        assert not (target / name).exists(), name
