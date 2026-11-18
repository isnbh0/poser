"""`poser.core.paths`: link detection that knows junctions, and a walk that enters no link."""

import sys
from pathlib import Path

import pytest
from links import LINK_KINDS, make_dangling_link, make_link

from poser.core.paths import is_link, walk


def test_is_link_is_false_for_a_directory_a_file_and_a_missing_path(tmp_path: Path) -> None:
    (tmp_path / "directory").mkdir()
    (tmp_path / "file").write_text("x", encoding="utf-8", newline="\n")
    for name in ("directory", "file", "missing"):
        assert not is_link(tmp_path / name), name


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_is_link_is_true_for_a_link_and_a_dangling_link(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "target"
    target.mkdir()
    make_link(kind, tmp_path / "link", target)
    make_dangling_link(kind, tmp_path / "dangling")
    assert is_link(tmp_path / "link")
    assert is_link(tmp_path / "dangling")
    assert (tmp_path / "dangling").exists(follow_symlinks=False)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows'")
def test_a_junction_resolves_to_its_target_s_plain_path(tmp_path: Path) -> None:
    """A junction's stored target, which `readlink` returns, carries the `\\\\?\\` prefix;
    `resolve()` drops it, so a resolved junction equals the plain paths poser holds."""
    target = tmp_path / "target"
    target.mkdir()
    make_link("junction", tmp_path / "link", target)
    assert str((tmp_path / "link").readlink()).startswith("\\\\?\\")
    resolved = (tmp_path / "link").resolve()
    assert resolved == target.resolve()
    assert not str(resolved).startswith("\\\\?\\")
    assert (tmp_path / "link" / "inner").resolve() == target.resolve() / "inner"


def test_walk_yields_every_entry_at_every_depth(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "file").write_bytes(b"")
    (tmp_path / "top").write_bytes(b"")
    found = {path.relative_to(tmp_path).as_posix() for path in walk(tmp_path)}
    assert found == {"a", "a/b", "a/b/file", "top"}


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_walk_yields_a_link_but_not_what_it_leads_to(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "target"
    (target / "inner").mkdir(parents=True)
    root = tmp_path / "root"
    root.mkdir()
    make_link(kind, root / "link", target)
    make_dangling_link(kind, root / "dangling")
    found = {path.relative_to(root).as_posix() for path in walk(root)}
    assert found == {"link", "dangling"}


def test_walk_of_a_missing_directory_yields_nothing(tmp_path: Path) -> None:
    assert list(walk(tmp_path / "missing")) == []
