"""Workspace initialization consumes an explicitly selected reference setup."""

import os
import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest
from helpers import set_instrument_guide

from poser.adapters.song_formats import SONG_FORMATS
from poser.cli import guide, init_workspace, sounds
from poser.cli.workspace import Workspace, find_workspace
from poser.compose import load
from poser.core.guide import GENERATED
from poser.core.loading import load_song
from poser.core.ports import ConfigError
from poser.core.song import Song, SongError
from poser.reference_setups.surge import prepare as surge


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    path = tmp_path / 'custom "Surge XT".vst3'
    path.mkdir()
    return path


def args(target: Path, plugin: Path) -> list[str]:
    return [str(target), "--setup", "surge", "--plugin-path", str(plugin)]


def snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    return {
        str(path.relative_to(root)): (
            "link" if path.is_symlink() else "dir" if path.is_dir() else "file",
            str(path.readlink()).encode()
            if path.is_symlink()
            else b""
            if path.is_dir()
            else path.read_bytes(),
        )
        for path in root.rglob("*")
    }


def test_success_writes_current_guide_and_loadable_demo(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "studio"
    presets = tmp_path / 'factory "patches"'
    presets.mkdir()
    demo = files("poser.reference_setups.surge").joinpath("demo.yaml").read_bytes()
    assert init_workspace.main([*args(target, plugin), "--presets-path", str(presets)]) == 0
    config = tomllib.loads((target / "poser.toml").read_text())
    surge = config["instruments"]["surge"]
    assert surge["path"] == str(plugin.resolve())
    assert surge["sources"]["presets"] == str(presets.resolve())
    for name in ("poser.toml", "songs/demo.yaml", "AGENTS.md", "CLAUDE.md", "POSER.md"):
        assert (target / name).is_file()
    assert (target / "songs/demo.yaml").read_bytes() == demo
    assert files("poser.reference_setups.surge").joinpath("demo.yaml").read_bytes() == demo
    assert find_workspace(target / "songs") == Workspace(target.resolve())
    song = load_song(target / "songs/demo.yaml", SONG_FORMATS).source
    assert isinstance(song, Song)
    assert song.tracks
    assert {str(track.sound) for track in song.tracks} == {"surge"}
    composition = load(target / "poser.toml", target / "sounds", target / "renders")
    instructions = (target / "AGENTS.md").read_text()
    assert instructions == files("poser").joinpath("scaffold", "agents.md").read_text()
    assert "[POSER.md](POSER.md)" in instructions
    assert not instructions.startswith(GENERATED)
    assert (target / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    text = (target / "POSER.md").read_text()
    assert text == guide.workspace_guide(composition)
    assert text.startswith(GENERATED + "\n")
    assert guide.main(["--config", str(target / "poser.toml"), "--check"]) == 0
    assert sounds.list_main(["--config", str(target / "poser.toml")]) == 0
    output = capsys.readouterr().out
    assert "check-instruments" in output
    assert str(plugin) in output
    assert not (target / "sounds").exists()
    assert not (target / "renders").exists()


def test_complete_read_only_gitignore_is_not_rewritten(
    tmp_path: Path, plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "studio"
    target.mkdir()
    ignore = target / ".gitignore"
    original = files("poser").joinpath("scaffold", "gitignore").read_bytes()
    ignore.write_bytes(original)
    ignore.chmod(0o444)
    write_text = Path.write_text

    def reject_ignore_write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path == ignore:
            raise AssertionError("unchanged .gitignore was rewritten")
        return write_text(path, data, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, "write_text", reject_ignore_write)
    assert init_workspace.main(args(target, plugin)) == 0
    assert ignore.read_bytes() == original
    assert (ignore.stat().st_mode & 0o777) == 0o444
    assert (target / "poser.toml").is_file()


def test_later_guide_updates_use_studio_config_not_setup_resource(
    tmp_path: Path, plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "studio"
    assert init_workspace.main(args(target, plugin)) == 0
    resource = files("poser.reference_setups.surge").joinpath("poser.toml")
    original = resource.read_bytes()
    config = target / "poser.toml"
    set_instrument_guide(config, "surge", "SENTINEL-NOTE")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("setup was reconsulted")

    monkeypatch.setattr(init_workspace, "prepare", fail)
    assert guide.main(["--config", str(config)]) == 0
    assert "SENTINEL-NOTE" in (target / "POSER.md").read_text()
    assert resource.read_bytes() == original


def test_yaml_only_studio_reads_yaml_and_refuses_scripts(tmp_path: Path, plugin: Path) -> None:
    target = tmp_path / "studio"
    assert init_workspace.main(args(target, plugin)) == 0
    config = target / "poser.toml"
    config.write_text(config.read_text() + '\n[adapters]\nsong_formats = ["yaml"]\n')
    composition = load(config, target / "sounds", target / "renders")
    assert isinstance(
        load_song(target / "songs/demo.yaml", composition.song_formats()).source, Song
    )
    with pytest.raises(SongError, match="cannot read"):
        load_song(target / "songs/example.py", composition.song_formats())


def test_missing_selection_and_unknown_setup_do_not_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "new"
    for command in ([str(target)], [str(target), "--setup", "unknown"]):
        with pytest.raises(SystemExit) as error:
            init_workspace.main(command)
        assert error.value.code == 2
        assert "uv run init-workspace --setup surge" in capsys.readouterr().err
        assert not target.exists()


def test_help_links_to_current_documents(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        init_workspace.main(["--help"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert init_workspace.SETUP_GUIDE in output
    assert init_workspace.SETUP_README in "".join(output.split())
    for option in ("--setup", "--plugin-path", "--presets-path"):
        assert option in output
    assert files("poser.reference_setups.surge").joinpath("README.md").is_file()


def test_missing_plugin_diagnostic_links_to_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(surge, "PLUGIN_PATHS", ())
    target = tmp_path / "studio"
    assert init_workspace.main([str(target), "--setup", "surge"]) == 2
    diagnostic = capsys.readouterr().err
    assert surge.DOC_URL in diagnostic
    assert "--plugin-path" in diagnostic
    assert snapshot(target) == {}


def test_bad_overrides_do_not_mutate_existing_project(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / "pyproject.toml").write_text("[project]\nname = 'mine'\n")
    (target / ".gitignore").write_text(".venv/\n")
    before = snapshot(target)
    for command in (
        args(target, tmp_path / "absent.vst3"),
        [*args(target, plugin), "--presets-path", str(tmp_path / "absent")],
    ):
        assert init_workspace.main(command) == 2
        assert capsys.readouterr().err
        assert snapshot(target) == before


def test_regular_file_is_not_a_vst_bundle(tmp_path: Path) -> None:
    plugin = tmp_path / "file.vst3"
    plugin.write_text("not a bundle")
    target = tmp_path / "new"
    assert init_workspace.main(args(target, plugin)) == 2
    assert snapshot(target) == {}


def test_merges_existing_ignore(tmp_path: Path, plugin: Path) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / ".gitignore").write_text(".venv/\nnode_modules")
    assert init_workspace.main(args(target, plugin)) == 0
    text = (target / ".gitignore").read_text()
    assert text.startswith(".venv/\nnode_modules\n")
    assert "renders/" in text


@pytest.mark.parametrize(
    "existing", ["AGENTS.md", "POSER.md", "CLAUDE.md", "poser.toml", "songs/demo.yaml"]
)
def test_existing_owned_output_refuses_without_changes(
    tmp_path: Path, plugin: Path, existing: str
) -> None:
    target = tmp_path / "project"
    (target / existing).parent.mkdir(parents=True, exist_ok=True)
    (target / existing).write_text("mine")
    (target / ".gitignore").write_text("mine\n")
    before = snapshot(target)
    assert init_workspace.main(args(target, plugin)) == 2
    assert snapshot(target) == before


@pytest.mark.parametrize("kind", ["parent-file", "ignore-directory", "dangling", "ignore-symlink"])
def test_incompatible_targets_refuse_without_changes(
    tmp_path: Path, plugin: Path, kind: str
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    external = tmp_path / "external"
    external.write_text("untouched")
    if kind == "parent-file":
        (target / "songs").write_text("mine")
    elif kind == "ignore-directory":
        (target / ".gitignore").mkdir()
    elif kind == "dangling":
        (target / "songs").symlink_to(target / "missing")
    else:
        (target / ".gitignore").symlink_to(external)
    before = snapshot(target)
    assert init_workspace.main(args(target, plugin)) == 2
    assert snapshot(target) == before
    assert external.read_text() == "untouched"


def test_composition_failure_does_not_write(
    tmp_path: Path, plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "project"
    target.mkdir()
    (target / ".gitignore").write_text("keep\n")
    before = snapshot(target)

    def fail(*_args: object) -> str:
        raise ConfigError("composition failed")

    monkeypatch.setattr(init_workspace, "workspace_guide", fail)
    assert init_workspace.main(args(target, plugin)) == 2
    assert snapshot(target) == before


@pytest.mark.parametrize("advice", ["on", "off"])
def test_persists_advice(tmp_path: Path, plugin: Path, advice: str) -> None:
    target = tmp_path / "studio"
    assert init_workspace.main([*args(target, plugin), "--advice", advice]) == 0
    config = tomllib.loads((target / "poser.toml").read_text())
    assert config["guidance"]["advice"] is (advice == "on")
    assert guide.main(["--config", str(target / "poser.toml"), "--check"]) == 0


def test_hardlinked_ignore_refused(tmp_path: Path, plugin: Path) -> None:
    target = tmp_path / "studio"
    target.mkdir()
    original = tmp_path / "original"
    original.write_bytes(b"owner")
    (target / ".gitignore").hardlink_to(original)
    assert init_workspace.main(args(target, plugin)) == 2
    assert original.read_bytes() == b"owner"
    assert (target / ".gitignore").stat().st_ino == original.stat().st_ino


def test_guide_preparation_does_not_spawn_processes(
    tmp_path: Path, plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected process")

    monkeypatch.setattr(subprocess, "Popen", fail)
    assert init_workspace.main(args(tmp_path / "project", plugin)) == 0


def test_second_init_refuses_naming_an_existing_target(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "studio"
    assert init_workspace.main(args(target, plugin)) == 0
    before = snapshot(target)
    capsys.readouterr()
    assert init_workspace.main(args(target, plugin)) == 2
    assert str(target / "AGENTS.md") in capsys.readouterr().err
    assert snapshot(target) == before


def interrupted(tmp_path: Path, plugin: Path) -> tuple[Path, dict[str, tuple[str, bytes]]]:
    """A clean studio's snapshot, and a directory holding what a killed run left."""
    clean = tmp_path / "clean"
    assert init_workspace.main(args(clean, plugin)) == 0
    target = tmp_path / "studio"
    (target / "songs").mkdir(parents=True)
    for name in ("AGENTS.md", "songs/demo.yaml"):
        (target / name).write_bytes((clean / name).read_bytes())
    (target / ".poser-tmp-x").write_bytes(b"partial")
    return target, snapshot(clean)


def test_leftovers_refuse_without_the_flag(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _clean = interrupted(tmp_path, plugin)
    before = snapshot(target)
    capsys.readouterr()
    assert init_workspace.main(args(target, plugin)) == 2
    err = capsys.readouterr().err
    assert str(target / "AGENTS.md") in err
    assert str(target / "songs/demo.yaml") in err
    assert str(target / ".poser-tmp-x") in err
    assert "already hold what init-workspace writes" in err
    assert "--remove-leftovers" in err
    assert snapshot(target) == before


def test_remove_leftovers_initializes_like_a_clean_run(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, clean = interrupted(tmp_path, plugin)
    capsys.readouterr()
    assert init_workspace.main([*args(target, plugin), "--remove-leftovers"]) == 0
    out = capsys.readouterr().out
    assert "removed AGENTS.md\n" in out
    assert "removed songs/demo.yaml\n" in out
    assert not list(target.rglob(".poser-tmp-*"))
    assert snapshot(target) == clean


def test_a_lone_temp_file_is_a_leftover(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run killed before linking any target leaves only its temp file."""
    target, clean = interrupted(tmp_path, plugin)
    for name in ("AGENTS.md", "songs/demo.yaml"):
        (target / name).unlink()
    capsys.readouterr()
    assert init_workspace.main(args(target, plugin)) == 2
    assert str(target / ".poser-tmp-x") in capsys.readouterr().err
    assert init_workspace.main([*args(target, plugin), "--remove-leftovers"]) == 0
    assert not list(target.rglob(".poser-tmp-*"))
    assert snapshot(target) == clean


@pytest.mark.parametrize("flag", [[], ["--remove-leftovers"]])
def test_edited_target_refuses_and_deletes_nothing(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str], flag: list[str]
) -> None:
    target, _clean = interrupted(tmp_path, plugin)
    (target / "songs/demo.yaml").write_bytes(b"edited")
    before = snapshot(target)
    capsys.readouterr()
    assert init_workspace.main([*args(target, plugin), *flag]) == 2
    assert f"{target / 'songs/demo.yaml'} exists" in capsys.readouterr().err
    assert snapshot(target) == before


def test_merged_gitignore_is_not_a_leftover(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, clean = interrupted(tmp_path, plugin)
    merged = b"mine\n" + files("poser").joinpath("scaffold", "gitignore").read_bytes()
    (target / ".gitignore").write_bytes(merged)
    capsys.readouterr()
    assert init_workspace.main(args(target, plugin)) == 2
    assert ".gitignore" not in capsys.readouterr().err
    assert init_workspace.main([*args(target, plugin), "--remove-leftovers"]) == 0
    assert (target / ".gitignore").read_bytes() == merged
    assert ".gitignore" not in capsys.readouterr().out
    assert set(snapshot(target)) == set(clean)


def test_target_still_linked_from_its_temp_file_is_a_leftover(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run killed between linking a target and unlinking its temp file left both."""
    target, clean = interrupted(tmp_path, plugin)
    os.link(target / "AGENTS.md", target / ".poser-tmp-linked")
    capsys.readouterr()
    assert init_workspace.main(args(target, plugin)) == 2
    assert "already hold what init-workspace writes" in capsys.readouterr().err
    assert init_workspace.main([*args(target, plugin), "--remove-leftovers"]) == 0
    assert "removed AGENTS.md\n" in capsys.readouterr().out
    assert not list(target.rglob(".poser-tmp-*"))
    assert snapshot(target) == clean


def test_target_linked_from_another_file_is_a_conflict(
    tmp_path: Path, plugin: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _clean = interrupted(tmp_path, plugin)
    os.link(target / "AGENTS.md", target / "mine.md")
    before = snapshot(target)
    capsys.readouterr()
    assert init_workspace.main([*args(target, plugin), "--remove-leftovers"]) == 2
    assert f"{target / 'AGENTS.md'} exists" in capsys.readouterr().err
    assert snapshot(target) == before
