"""Initialization must not change an external file through a hard link."""

import os
from pathlib import Path

from poser.cli.init_workspace import main


def test_gitignore_hardlink_refused(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin.vst3"
    plugin.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched\n")
    project = tmp_path / "project"
    project.mkdir()
    os.link(outside, project / ".gitignore")
    assert main([str(project), "--setup", "surge", "--plugin-path", str(plugin)]) == 2
    assert outside.read_text() == "untouched\n"
    assert sorted(p.name for p in project.iterdir()) == [".gitignore"]
