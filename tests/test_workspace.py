"""Tests for finding a workspace and filling command path options from it."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from poser import analyze, plugin_check, render, sounds
from poser.sounds import SavedSound, write_sound
from poser.workspace import (
    SongOutsideWorkspaceError,
    Workspace,
    WorkspaceError,
    find_workspace,
    song_render_dir,
)


def make_workspace(root: Path) -> Path:
    """Make `root` a workspace whose plugin list names one plugin at a missing path."""
    root.mkdir(parents=True, exist_ok=True)
    missing = root / "Missing.vst3"
    (root / "plugins.toml").write_text(f'[[plugin]]\nname = "Serum"\npath = "{missing}"\n')
    return root


def write_song_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    note = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}
    track = {"name": "lead", "engine": "pedalboard", "sound": "Nope", "notes": [note]}
    path.write_text(yaml.safe_dump({"bpm": 120, "tracks": [track]}))
    return path


def test_workspace_is_the_directory_with_a_plugin_list(tmp_path: Path) -> None:
    make_workspace(tmp_path)

    assert find_workspace(tmp_path) == Workspace(tmp_path.resolve())


def test_search_walks_up_to_the_nearest_plugin_list(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    start = tmp_path / "a" / "b"
    start.mkdir(parents=True)

    assert find_workspace(start) == Workspace(tmp_path.resolve())


def test_search_starts_in_the_working_directory(tmp_path: Path) -> None:
    make_workspace(tmp_path)

    assert find_workspace() == Workspace(tmp_path.resolve())


def test_directory_named_plugins_toml_is_not_a_workspace(tmp_path: Path) -> None:
    (tmp_path / "plugins.toml").mkdir()

    with pytest.raises(WorkspaceError):
        find_workspace(tmp_path)


def test_missing_workspace_names_the_start_and_the_fix(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError) as error:
        find_workspace(tmp_path)

    assert str(error.value) == (
        f"no plugins.toml in {tmp_path.resolve()} or any parent; "
        "run `uv run init-workspace` to create a workspace"
    )


def test_workspace_layout(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    assert workspace.config == tmp_path / "plugins.toml"
    assert workspace.sounds_dir == tmp_path / "sounds"
    assert workspace.renders_dir == tmp_path / "renders"


COMMANDS: list[tuple[str, Callable[[list[str]], int], list[str]]] = [
    ("check-plugins", plugin_check.main, []),
    ("render-song", render.main, ["song.yaml"]),
    ("analyze-song", analyze.main, ["song.yaml"]),
    ("list-sounds", sounds.list_main, []),
    ("save-sound", sounds.save_main, ["x", "--plugin", "Serum", "--description", "d"]),
]


@pytest.mark.parametrize(("prog", "main", "argv"), COMMANDS, ids=[prog for prog, _, _ in COMMANDS])
def test_command_outside_a_workspace_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    prog: str,
    main: Callable[[list[str]], int],
    argv: list[str],
) -> None:
    write_song_file(tmp_path / "song.yaml")

    assert main(argv) == plugin_check.EXIT_BAD_CONFIG

    assert capsys.readouterr().err.startswith(f"{prog}: no plugins.toml in ")


def test_explicit_paths_need_no_workspace(tmp_path: Path) -> None:
    config = make_workspace(tmp_path / "elsewhere") / "plugins.toml"
    (tmp_path / "plugins.toml").unlink(missing_ok=True)

    argv = ["--config", str(config), "--sounds-dir", str(tmp_path / "sounds")]

    assert sounds.list_main(argv) == plugin_check.EXIT_ALL_PASS


def test_render_from_a_subdirectory_writes_at_the_workspace_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_workspace(tmp_path)
    song = write_song_file(tmp_path / "songs" / "tune.yaml")
    monkeypatch.chdir(tmp_path / "songs")

    render.main([str(song)])

    assert (tmp_path / "renders" / "songs" / "tune.yaml").is_dir()
    assert not (tmp_path / "songs" / "renders").exists()


def test_list_sounds_reads_the_workspace_sounds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_workspace(tmp_path)
    write_sound(tmp_path / "sounds", SavedSound("warm", "Serum", "a warm pad", "c3RhdGU="))

    sounds.list_main(["--json"])

    names = [entry["name"] for entry in json.loads(capsys.readouterr().out)]
    assert "warm" in names


@pytest.mark.parametrize("name", ["x.py", "x.yaml", "x.yml"])
def test_render_dir_is_the_song_path_with_its_suffix(tmp_path: Path, name: str) -> None:
    base, renders = tmp_path / "base", tmp_path / "r"

    assert song_render_dir(renders, base, base / "songs" / name) == renders / "songs" / name


def test_song_in_a_directory_named_like_a_song_gets_its_own_render_dir(tmp_path: Path) -> None:
    base, renders = tmp_path / "base", tmp_path / "r"

    nested = song_render_dir(renders, base, base / "songs" / "x" / "mix.py")

    assert nested == renders / "songs" / "x" / "mix.py"
    assert nested != song_render_dir(renders, base, base / "songs" / "x.py")


def test_relative_song_path_resolves_from_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, renders = tmp_path / "base", tmp_path / "r"
    (base / "songs").mkdir(parents=True)
    monkeypatch.chdir(base / "songs")

    assert song_render_dir(renders, base, Path("../drafts/x.py")) == renders / "drafts" / "x.py"


def test_render_dir_does_not_need_the_song_file(tmp_path: Path) -> None:
    song = tmp_path / "songs" / "missing.yaml"

    assert (
        song_render_dir(tmp_path / "r", tmp_path, song) == tmp_path / "r" / "songs" / "missing.yaml"
    )
    assert not song.exists()


def test_song_outside_the_base_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "base"
    song = tmp_path / "elsewhere" / "x.py"

    with pytest.raises(SongOutsideWorkspaceError) as error:
        song_render_dir(tmp_path / "r", base, song)

    assert isinstance(error.value, WorkspaceError)
    assert str(error.value) == f"{song.resolve()} is outside the workspace at {base.resolve()}"
