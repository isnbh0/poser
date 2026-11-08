"""Tests for `init-workspace`, which writes the scaffold into a new workspace."""

from importlib.resources import files
from pathlib import Path

import pytest

from poser import init_workspace
from poser.adapters.song_formats import SONG_FORMATS
from poser.core.loading import load_song
from poser.init_workspace import GITIGNORE_RESOURCE, SCAFFOLD_FILES
from poser.workspace import Workspace, find_workspace


def resource(name: str) -> bytes:
    return (files("poser") / "scaffold" / name).read_bytes()


def test_every_scaffold_resource_exists() -> None:
    for name in (*SCAFFOLD_FILES, GITIGNORE_RESOURCE):
        assert (files("poser") / "scaffold" / name).is_file(), name


def test_init_creates_the_directory_and_writes_the_scaffold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "new"

    assert init_workspace.main([str(target)]) == init_workspace.EXIT_OK

    for name, path in SCAFFOLD_FILES.items():
        assert (target / path).read_bytes() == resource(name), path
    assert (target / ".gitignore").read_bytes() == resource(GITIGNORE_RESOURCE)
    assert capsys.readouterr().out.splitlines() == [
        "AGENTS.md",
        "CLAUDE.md",
        "plugins.toml",
        "songs/demo.yaml",
        "Edit plugins.toml to match this machine's plugins, then run: uv run check-plugins",
    ]
    assert not (target / "sounds").exists()
    assert not (target / "renders").exists()


def test_init_defaults_to_the_working_directory(tmp_path: Path) -> None:
    assert init_workspace.main([]) == init_workspace.EXIT_OK

    assert (tmp_path / "songs" / "demo.yaml").is_file()


def test_init_merges_an_existing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".venv/\nnode_modules")

    init_workspace.main([str(tmp_path)])

    assert (tmp_path / ".gitignore").read_text() == (
        ".venv/\nnode_modules\nrenders/\n__pycache__/\n.DS_Store\n"
    )


@pytest.mark.parametrize("existing", list(SCAFFOLD_FILES.values()))
def test_existing_target_refuses_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], existing: str
) -> None:
    (tmp_path / existing).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / existing).write_text("mine")

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_CONFLICT

    assert capsys.readouterr().err == f"init-workspace: {tmp_path / existing} exists\n"
    assert (tmp_path / existing).read_text() == "mine"
    for path in SCAFFOLD_FILES.values():
        if path != existing:
            assert not (tmp_path / path).exists(), path
    assert not (tmp_path / ".gitignore").exists()


def test_initialized_directory_is_a_workspace_with_a_loadable_demo(tmp_path: Path) -> None:
    init_workspace.main([str(tmp_path)])

    assert find_workspace(tmp_path / "songs") == Workspace(tmp_path.resolve())
    assert load_song(tmp_path / "songs" / "demo.yaml", SONG_FORMATS).tracks


def test_a_file_where_the_workspace_goes_refuses(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "taken"
    target.write_text("mine")

    assert init_workspace.main([str(target)]) == init_workspace.EXIT_CONFLICT

    assert capsys.readouterr().err == f"init-workspace: {target} is not a directory\n"
    assert target.read_text() == "mine"


def test_a_file_where_songs_goes_refuses_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "songs").write_text("mine")

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_CONFLICT

    assert capsys.readouterr().err == f"init-workspace: {tmp_path / 'songs'} is not a directory\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["songs"]
