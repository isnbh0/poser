"""Initialization must not change an external file through a hard link."""

import os
from pathlib import Path

from poser.adapters.workspace_init import IDENTITY
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
    assert sorted(p.name for p in project.iterdir()) == [".gitignore", ".poser-init"]
    namespace = project / ".poser-init"
    assert sorted(p.name for p in namespace.iterdir()) == ["identity", "lock"]
    assert (namespace / "identity").read_bytes() == IDENTITY
    lock = (namespace / "lock").stat()
    namespace_inode = namespace.stat().st_ino
    assert main(command) == 2
    assert outside.read_text() == ignore.read_text() == "untouched\n"
    assert ignore.stat().st_ino == outside.stat().st_ino == original.st_ino
    assert ignore.stat().st_mode == original.st_mode
    assert ignore.stat().st_mtime_ns == original.st_mtime_ns
    assert ignore.stat().st_nlink == outside.stat().st_nlink == 2
    assert sorted(p.name for p in project.iterdir()) == [".gitignore", ".poser-init"]
    assert sorted(p.name for p in namespace.iterdir()) == ["identity", "lock"]
    assert (namespace / "identity").read_bytes() == IDENTITY
    assert namespace.stat().st_ino == namespace_inode
    retried_lock = (namespace / "lock").stat()
    assert (retried_lock.st_dev, retried_lock.st_ino) == (lock.st_dev, lock.st_ino)
