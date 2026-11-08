"""Tests for finding a workspace and filling command path options from it."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from workspaces import make_workspace, write_song

from poser.cli import analyze, check, guide, render, sounds
from poser.cli.common import EXIT_ALL_PASS, EXIT_BAD_CONFIG
from poser.cli.workspace import (
    SongOutsideWorkspaceError,
    Workspace,
    WorkspaceError,
    find_workspace,
    song_id,
)


def mark(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "poser.toml").write_text("")
    return root


def test_workspace_is_the_directory_with_a_poser_toml(tmp_path: Path) -> None:
    mark(tmp_path)

    assert find_workspace(tmp_path) == Workspace(tmp_path.resolve())


def test_search_walks_up_to_the_nearest_poser_toml(tmp_path: Path) -> None:
    mark(tmp_path)
    start = tmp_path / "a" / "b"
    start.mkdir(parents=True)

    assert find_workspace(start) == Workspace(tmp_path.resolve())


def test_search_starts_in_the_working_directory(tmp_path: Path) -> None:
    mark(tmp_path)

    assert find_workspace() == Workspace(tmp_path.resolve())


def test_a_plugins_toml_no_longer_marks_a_workspace(tmp_path: Path) -> None:
    (tmp_path / "plugins.toml").write_text("")

    with pytest.raises(WorkspaceError):
        find_workspace(tmp_path)


def test_directory_named_poser_toml_is_not_a_workspace(tmp_path: Path) -> None:
    (tmp_path / "poser.toml").mkdir()

    with pytest.raises(WorkspaceError):
        find_workspace(tmp_path)


def test_missing_workspace_names_the_start_and_the_fix(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as error:
        find_workspace(tmp_path)

    assert str(error.value) == (
        f"no poser.toml in {tmp_path.resolve()} or any parent; "
        "run `uv run init-workspace` to create a workspace"
    )


def test_workspace_layout(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    assert workspace.config == tmp_path / "poser.toml"
    assert workspace.sounds_dir == tmp_path / "sounds"
    assert workspace.renders_dir == tmp_path / "renders"


COMMANDS: list[tuple[str, Callable[[list[str]], int], list[str]]] = [
    ("check-instruments", check.main, []),
    ("render-song", render.main, ["song.yaml"]),
    ("analyze-song", analyze.main, ["song.yaml"]),
    ("list-sounds", sounds.list_main, []),
    ("save-sound", sounds.save_main, ["synth", "x", "--description", "d"]),
    ("update-guide", guide.main, []),
]


@pytest.mark.parametrize(("prog", "main", "argv"), COMMANDS, ids=[prog for prog, _, _ in COMMANDS])
def test_command_outside_a_workspace_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    prog: str,
    main: Callable[[list[str]], int],
    argv: list[str],
) -> None:
    write_song(tmp_path / "song.yaml", ("lead", "synth"))

    assert main(argv) == EXIT_BAD_CONFIG

    assert capsys.readouterr().err.startswith(f"{prog}: no poser.toml in ")


def test_explicit_paths_need_no_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = make_workspace(tmp_path / "elsewhere", monkeypatch)
    here = tmp_path / "here"
    here.mkdir()
    monkeypatch.chdir(here)
    argv = ["--config", str(workspace.config), "--sounds-dir", str(tmp_path / "sounds")]

    assert sounds.list_main([*argv, "--json"]) == EXIT_ALL_PASS
    assert json.loads(capsys.readouterr().out)[0]["sound"] == "synth"


def test_render_from_a_subdirectory_writes_at_the_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_workspace(tmp_path, monkeypatch)
    song = write_song(tmp_path / "songs" / "tune.yaml", ("lead", "synth"))
    monkeypatch.chdir(tmp_path / "songs")

    assert render.main([song.name]) == EXIT_ALL_PASS

    assert (tmp_path / "renders" / "songs" / "tune.yaml" / "stems" / "lead.wav").is_file()
    assert not (tmp_path / "songs" / "renders").exists()


@pytest.mark.parametrize("name", ["x.py", "x.yaml", "x.yml"])
def test_song_id_is_the_song_path_with_its_suffix(tmp_path: Path, name: str) -> None:
    assert song_id(tmp_path, tmp_path / "songs" / name) == f"songs/{name}"


def test_song_in_a_directory_named_like_a_song_gets_its_own_id(tmp_path: Path) -> None:
    nested = song_id(tmp_path, tmp_path / "songs" / "x" / "mix.py")

    assert nested == "songs/x/mix.py"
    assert nested != song_id(tmp_path, tmp_path / "songs" / "x.py")


def test_relative_song_path_resolves_from_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "songs").mkdir()
    monkeypatch.chdir(tmp_path / "songs")

    assert song_id(tmp_path, Path("../drafts/x.py")) == "drafts/x.py"


def test_song_id_does_not_need_the_song_file(tmp_path: Path) -> None:
    song = tmp_path / "songs" / "missing.yaml"

    assert song_id(tmp_path, song) == "songs/missing.yaml"
    assert not song.exists()


def test_symlinked_song_is_placed_by_its_target(tmp_path: Path) -> None:
    target = write_song(tmp_path / "songs" / "real.yaml", ("lead", "synth"))
    (tmp_path / "link.yaml").symlink_to(target)

    assert song_id(tmp_path, tmp_path / "link.yaml") == "songs/real.yaml"


def test_song_outside_the_base_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "base"
    song = tmp_path / "elsewhere" / "x.py"

    with pytest.raises(SongOutsideWorkspaceError) as error:
        song_id(base, song)

    assert isinstance(error.value, WorkspaceError)
    assert str(error.value) == f"{song.resolve()} is outside the workspace at {base.resolve()}"


def test_path_that_is_no_song_id_is_refused(tmp_path: Path) -> None:
    for path in (tmp_path, tmp_path / "a\\b.yaml"):
        with pytest.raises(SongOutsideWorkspaceError):
            song_id(tmp_path, path)
