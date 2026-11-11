"""Failure windows use real temporary files and OS locks, never a studio."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from poser.adapters import workspace_init as module
from poser.adapters.workspace_init import WorkspaceInitializer
from poser.core.ports import ConfigError


@pytest.mark.parametrize("point", [1, 3, 4, 5, 6])
def test_failed_publication_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: int
) -> None:
    owner = WorkspaceInitializer(tmp_path)
    ignore = tmp_path / ".gitignore"
    ignore.write_bytes(b"owner\n")
    ignore.chmod(0o640)
    save = owner._save
    calls = 0

    def fail(state: module.Journal) -> None:
        nonlocal calls
        calls += 1
        if calls == point:
            raise OSError("injected transition failure")
        save(state)

    with owner.session():
        inode = (owner.namespace / "lock").stat().st_ino
        monkeypatch.setattr(owner, "_save", fail)
        with pytest.raises(OSError, match="injected transition failure"):
            owner.publish(
                {"AGENTS.md": b"new", "songs/demo.yaml": b"demo", ".gitignore": b"merged"},
                expected_ignore=b"owner\n",
            )
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "songs").exists()
    assert ignore.read_bytes() == b"owner\n"
    assert ignore.stat().st_mode & 0o777 == 0o640
    assert (owner.namespace / "lock").stat().st_ino == inode


def test_owner_edit_preserved_and_journal_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = WorkspaceInitializer(tmp_path)
    link = os.link

    def change(source: Path, target: Path) -> None:
        if target.name == "second":
            (tmp_path / "first").write_bytes(b"owner edit")
            raise OSError("failed")
        link(source, target)

    monkeypatch.setattr(module.os, "link", change)
    with owner.session(), pytest.raises(ConfigError, match="first"):
        owner.publish({"first": b"generated", "second": b"generated"})
    assert (tmp_path / "first").read_bytes() == b"owner edit"
    assert owner.journal.exists()
    with pytest.raises(ConfigError, match="first"), owner.session():
        pass


@pytest.mark.parametrize("target", ["first", ".gitignore"])
@pytest.mark.parametrize("metadata", ["mode", "mtime"])
def test_metadata_edit_preserved_on_failed_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, metadata: str
) -> None:
    owner = WorkspaceInitializer(tmp_path)
    output = tmp_path / target
    if target == ".gitignore":
        output.write_bytes(b"original")
    link = os.link
    edited_mtime = 1234567890000000000

    def change(source: Path, destination: Path) -> None:
        if destination.name == "second":
            if metadata == "mode":
                output.chmod(0o600)
            else:
                os.utime(output, ns=(output.stat().st_atime_ns, edited_mtime))
            raise OSError("publication failure after owner metadata edit")
        link(source, destination)

    monkeypatch.setattr(module.os, "link", change)
    with owner.session(), pytest.raises(ConfigError, match=target):
        owner.publish(
            {target: b"published", "second": b"new"},
            expected_ignore=b"original" if target == ".gitignore" else None,
        )
    assert output.read_bytes() == b"published"
    assert owner.journal.exists()
    if metadata == "mode":
        assert output.stat().st_mode & 0o777 == 0o600
    else:
        assert output.stat().st_mtime_ns == edited_mtime
    journal = owner.journal.read_bytes()
    with pytest.raises(ConfigError, match=target), owner.session():
        pass
    assert output.read_bytes() == b"published"
    assert owner.journal.read_bytes() == journal


@pytest.mark.parametrize("metadata", ["mode", "mtime"])
def test_metadata_edit_preserved_after_process_death(tmp_path: Path, metadata: str) -> None:
    script = """
import os, sys
from pathlib import Path
from poser.adapters.workspace_init import WorkspaceInitializer
owner = WorkspaceInitializer(Path(sys.argv[1]))
link = os.link
def die(source, target):
    if target.name == "second":
        first = owner.directory / "first"
        if sys.argv[2] == "mode":
            first.chmod(0o600)
        else:
            os.utime(first, ns=(first.stat().st_atime_ns, 1234567890000000000))
        os._exit(19)
    link(source, target)
os.link = die
with owner.session():
    owner.publish({"first": b"published", "second": b"new"})
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path), metadata], check=False)
    assert result.returncode == 19
    owner = WorkspaceInitializer(tmp_path)
    output = tmp_path / "first"
    journal = owner.journal.read_bytes()
    lock = (owner.namespace / "lock").stat().st_ino
    with pytest.raises(ConfigError, match="first"), owner.session():
        pass
    assert output.read_bytes() == b"published"
    assert owner.journal.read_bytes() == journal
    assert (owner.namespace / "lock").stat().st_ino == lock
    if metadata == "mode":
        assert output.stat().st_mode & 0o777 == 0o600
    else:
        assert output.stat().st_mtime_ns == 1234567890000000000


def test_process_death_retry_and_three_contenders(tmp_path: Path) -> None:
    script = """
import os, sys
from pathlib import Path
from poser.adapters.workspace_init import WorkspaceInitializer
owner = WorkspaceInitializer(Path(sys.argv[1]))
link = os.link
def die(source, target):
    link(source, target)
    os._exit(19)
os.link = die
with owner.session():
    owner.publish({"first": b"new", "songs/second": b"new"})
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=False)
    assert result.returncode == 19
    owner = WorkspaceInitializer(tmp_path)
    inode = (owner.namespace / "lock").stat().st_ino
    assert (tmp_path / "first").read_bytes() == b"new"
    with owner.session():
        assert not (tmp_path / "first").exists()
        assert not (tmp_path / "songs").exists()
        for _ in range(2):
            with pytest.raises(ConfigError, match="busy"), WorkspaceInitializer(tmp_path).session():
                pass
        owner.publish({"first": b"retry"})
        with pytest.raises(ConfigError, match="busy"), WorkspaceInitializer(tmp_path).session():
            pass
    assert (owner.namespace / "lock").stat().st_ino == inode
    assert (tmp_path / "first").read_bytes() == b"retry"


def test_committed_cleanup_failure_never_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = WorkspaceInitializer(tmp_path)

    def fail() -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(owner, "_cleanup", fail)
    with owner.session(), pytest.raises(OSError, match="cleanup failed"):
        owner.publish({"first": b"committed"})
    with (
        pytest.raises(ConfigError, match="already exists"),
        WorkspaceInitializer(tmp_path).session(),
    ):
        pass
    assert (tmp_path / "first").read_bytes() == b"committed"
    assert not owner.journal.exists()


def test_ignore_changed_since_preparation_is_preserved(tmp_path: Path) -> None:
    owner = WorkspaceInitializer(tmp_path)
    (tmp_path / ".gitignore").write_bytes(b"new owner content")
    with owner.session(), pytest.raises(ConfigError, match="during preparation"):
        owner.publish({".gitignore": b"stale merge"}, expected_ignore=b"old content")
    assert (tmp_path / ".gitignore").read_bytes() == b"new owner content"


def test_directory_creation_window_is_non_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = WorkspaceInitializer(tmp_path)
    save = owner._save
    calls = 0

    def fail(state: module.Journal) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("death after mkdir before recording ownership")
        save(state)

    monkeypatch.setattr(owner, "_save", fail)
    with owner.session(), pytest.raises(ConfigError, match="directory ownership ambiguous"):
        owner.publish({"songs/demo": b"demo"})
    assert (tmp_path / "songs").is_dir()
    assert owner.journal.exists()


def test_refuses_unrelated_namespace(tmp_path: Path) -> None:
    (tmp_path / ".poser-init").mkdir()
    with pytest.raises(ConfigError, match="unrecognized"), WorkspaceInitializer(tmp_path).session():
        pass


def test_publication_requires_lock(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="active locked session"):
        WorkspaceInitializer(tmp_path).publish({"first": b"new"})
    assert list(tmp_path.iterdir()) == []


def test_rollback_failure_retains_journal_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = WorkspaceInitializer(tmp_path)
    unlink = Path.unlink
    link = os.link

    def fail_link(source: Path, target: Path) -> None:
        if target.name == "second":
            raise OSError("publication failure")
        link(source, target)

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == tmp_path / "first":
            raise OSError("rollback failure")
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "link", fail_link)
        patch.setattr(Path, "unlink", fail_unlink)
        with owner.session(), pytest.raises(ConfigError, match=r"first.*rollback failure"):
            owner.publish({"first": b"new", "second": b"new"})
    assert owner.journal.exists()
    assert (tmp_path / "first").read_bytes() == b"new"
    with owner.session():
        assert not (tmp_path / "first").exists()
        assert not owner.journal.exists()


def test_intervening_directory_is_not_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = WorkspaceInitializer(tmp_path)
    mkdir = Path.mkdir

    def intervene(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path == tmp_path / "songs":
            mkdir(path)
        mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    with owner.session():
        monkeypatch.setattr(Path, "mkdir", intervene)
        with pytest.raises(ConfigError, match="directory ownership ambiguous"):
            owner.publish({"songs/demo": b"new"})
    assert (tmp_path / "songs").is_dir()
    assert owner.journal.exists()


def test_no_clobber_intervening_owner_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = WorkspaceInitializer(tmp_path)
    link = os.link

    def intervene(source: Path, target: Path) -> None:
        target.write_bytes(b"owner")
        link(source, target)

    monkeypatch.setattr(module.os, "link", intervene)
    with owner.session(), pytest.raises(ConfigError, match="first"):
        owner.publish({"first": b"generated"})
    assert (tmp_path / "first").read_bytes() == b"owner"
