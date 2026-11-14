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
    ignore = project / ".gitignore"
    original = outside.stat()
    command = [str(project), "--setup", "surge", "--plugin-path", str(plugin)]
    assert main(command) == 2
    assert outside.read_text() == "untouched\n"
    assert ignore.read_text() == "untouched\n"
    assert ignore.stat().st_ino == original.st_ino == outside.stat().st_ino
    assert ignore.stat().st_nlink == outside.stat().st_nlink == 2
    assert ignore.stat().st_mode == original.st_mode
    assert ignore.stat().st_mtime_ns == original.st_mtime_ns
    assert sorted(p.name for p in project.iterdir()) == [".gitignore"]
    assert main(command) == 2
    assert outside.read_text() == ignore.read_text() == "untouched\n"
    assert ignore.stat().st_ino == outside.stat().st_ino == original.st_ino
    assert ignore.stat().st_mode == original.st_mode
    assert ignore.stat().st_mtime_ns == original.st_mtime_ns
    assert ignore.stat().st_nlink == outside.stat().st_nlink == 2
    assert sorted(p.name for p in project.iterdir()) == [".gitignore"]
