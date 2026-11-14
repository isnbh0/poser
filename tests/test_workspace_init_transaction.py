"""No-clobber publication and its in-process rollback, on real temporary files."""

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from poser.adapters.workspace_init import WorkspaceInitializer
from poser.core.ports import ConfigError

OUTPUTS = {
    "AGENTS.md": b"agents\n",
    "POSER.md": b"guide\n",
    "songs/demo.yaml": b"demo\n",
    "poser.toml": b"config\n",
}


def tree(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


def temps(root: Path) -> list[Path]:
    return list(root.rglob(".poser-tmp-*"))


def failing_link(calls_before_failure: int, error: BaseException) -> Callable[[Path, Path], None]:
    link = os.link
    calls = 0

    def fail(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls > calls_before_failure:
            raise error
        link(source, target)

    return fail


def test_publish_creates_targets_with_bytes_and_mode(tmp_path: Path) -> None:
    WorkspaceInitializer(tmp_path).publish(
        {**OUTPUTS, ".gitignore": b"renders/\n"}, expected_ignore=None
    )
    for name, data in {**OUTPUTS, ".gitignore": b"renders/\n"}.items():
        path = tmp_path / name
        assert path.read_bytes() == data
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert path.stat().st_nlink == 1
    assert temps(tmp_path) == []
    # Only the targets exist: no journal, lock, or other initialization namespace.
    assert sorted(tree(tmp_path)) == sorted([*OUTPUTS, ".gitignore", "songs"])


def test_publish_creates_missing_directory_and_failure_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "new" / "studio"
    WorkspaceInitializer(target).publish(OUTPUTS, expected_ignore=None)
    assert (target / "songs/demo.yaml").read_bytes() == b"demo\n"
    other = tmp_path / "other"
    monkeypatch.setattr(os, "link", failing_link(1, OSError(5, "injected")))
    with pytest.raises(ConfigError, match="cannot create: injected"):
        WorkspaceInitializer(other).publish(OUTPUTS, expected_ignore=None)
    assert not other.exists()


def test_existing_target_refuses_and_removes_earlier_targets(tmp_path: Path) -> None:
    (tmp_path / "songs").mkdir()
    (tmp_path / "songs/demo.yaml").write_bytes(b"mine")
    with pytest.raises(FileExistsError):
        WorkspaceInitializer(tmp_path).publish(OUTPUTS, expected_ignore=None)
    assert tree(tmp_path) == {"songs": None, "songs/demo.yaml": b"mine"}


def test_failure_after_second_target_rolls_back_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ignore = tmp_path / ".gitignore"
    ignore.write_bytes(b"owner\n")
    ignore.chmod(0o640)
    os.utime(ignore, ns=(1_000_000_000, 2_000_000_000))
    outputs = {".gitignore": b"owner\nrenders/\n", **OUTPUTS}
    monkeypatch.setattr(os, "link", failing_link(2, OSError(5, "injected")))
    with pytest.raises(ConfigError, match=r"demo\.yaml: cannot create"):
        WorkspaceInitializer(tmp_path).publish(outputs, expected_ignore=b"owner\n")
    assert tree(tmp_path) == {".gitignore": b"owner\n"}
    assert stat.S_IMODE(ignore.stat().st_mode) == 0o640


def test_rollback_restores_a_replaced_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ignore = tmp_path / ".gitignore"
    ignore.write_bytes(b"owner\n")
    ignore.chmod(0o640)
    os.utime(ignore, ns=(1_000_000_000, 2_000_000_000))
    replace = os.replace
    calls = 0

    def fail_after_ignore(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        replace(source, target)
        if calls == 1:
            assert ignore.read_bytes() == b"owner\nrenders/\n"
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "replace", fail_after_ignore)
    with pytest.raises(KeyboardInterrupt):
        WorkspaceInitializer(tmp_path).publish(
            {**OUTPUTS, ".gitignore": b"owner\nrenders/\n"}, expected_ignore=b"owner\n"
        )
    assert tree(tmp_path) == {".gitignore": b"owner\n"}
    assert stat.S_IMODE(ignore.stat().st_mode) == 0o640
    assert ignore.stat().st_mtime_ns == 2_000_000_000


def test_replaced_gitignore_keeps_its_mode(tmp_path: Path) -> None:
    ignore = tmp_path / ".gitignore"
    ignore.write_bytes(b"owner\n")
    ignore.chmod(0o444)
    WorkspaceInitializer(tmp_path).publish(
        {".gitignore": b"owner\nrenders/\n"}, expected_ignore=b"owner\n"
    )
    assert ignore.read_bytes() == b"owner\nrenders/\n"
    assert stat.S_IMODE(ignore.stat().st_mode) == 0o444


def test_gitignore_changed_before_replace_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ignore = tmp_path / ".gitignore"
    ignore.write_bytes(b"owner\n")
    ignore.chmod(0o600)
    fchmod = os.fchmod

    def mutate(fd: int, mode: int) -> None:
        fchmod(fd, mode)
        if mode == 0o600:
            ignore.write_bytes(b"owner edited\n")

    monkeypatch.setattr(os, "fchmod", mutate)
    with pytest.raises(ConfigError, match=r"\.gitignore changed during initialization; retry"):
        WorkspaceInitializer(tmp_path).publish(
            {**OUTPUTS, ".gitignore": b"owner\nrenders/\n"}, expected_ignore=b"owner\n"
        )
    assert tree(tmp_path) == {".gitignore": b"owner edited\n"}


def test_keyboard_interrupt_from_link_rolls_back_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "studio"
    monkeypatch.setattr(os, "link", failing_link(3, KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        WorkspaceInitializer(target).publish(OUTPUTS, expected_ignore=None)
    assert not target.exists()


def test_rollback_keeps_a_target_another_run_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    link = os.link

    def race(source: Path, target: Path) -> None:
        if Path(target).name == "POSER.md":
            Path(target).write_bytes(b"guide\n")
        link(source, target)

    monkeypatch.setattr(os, "link", race)
    with pytest.raises(FileExistsError):
        WorkspaceInitializer(tmp_path).publish(OUTPUTS, expected_ignore=None)
    assert tree(tmp_path) == {"POSER.md": b"guide\n"}


def test_symlink_parent_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    studio = tmp_path / "studio"
    studio.mkdir()
    (studio / "songs").symlink_to(elsewhere)
    with pytest.raises(ConfigError, match="symlink parent"):
        WorkspaceInitializer(studio).publish(OUTPUTS, expected_ignore=None)
    assert list(elsewhere.iterdir()) == []
    assert sorted(p.name for p in studio.iterdir()) == ["songs"]


def test_remove_leftovers_unlinks_matching_targets_and_temps(tmp_path: Path) -> None:
    (tmp_path / "songs").mkdir()
    (tmp_path / "AGENTS.md").write_bytes(OUTPUTS["AGENTS.md"])
    (tmp_path / "songs/demo.yaml").write_bytes(OUTPUTS["songs/demo.yaml"])
    (tmp_path / ".poser-tmp-x").write_bytes(b"partial")
    (tmp_path / "songs/.poser-tmp-y").write_bytes(b"partial")
    removed = WorkspaceInitializer(tmp_path).remove_leftovers(OUTPUTS)
    assert removed == ("AGENTS.md", "songs/demo.yaml")
    assert tree(tmp_path) == {"songs": None}


def test_remove_leftovers_refuses_a_changed_target(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_bytes(OUTPUTS["AGENTS.md"])
    (tmp_path / "POSER.md").write_bytes(b"edited\n")
    with pytest.raises(ConfigError, match=r"POSER\.md: changed; not removed"):
        WorkspaceInitializer(tmp_path).remove_leftovers(OUTPUTS)
    assert tree(tmp_path) == {"POSER.md": b"edited\n"}
