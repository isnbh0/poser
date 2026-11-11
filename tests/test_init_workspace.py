"""Workspace initialization consumes an explicitly selected reference setup."""

import subprocess
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from poser.adapters.song_formats import SONG_FORMATS
from poser.cli import guide, init_workspace, sounds
from poser.cli.workspace import Workspace, find_workspace
from poser.compose import load
from poser.core.guide import GENERATED, compose_guide
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
    song = load_song(target / "songs/demo.yaml", SONG_FORMATS)
    assert isinstance(song, Song)
    assert song.tracks
    assert {str(track.sound) for track in song.tracks} == {"surge"}
    composition = load(target / "poser.toml", target / "sounds", target / "renders")
    core = files("poser").joinpath("scaffold", "guide.md").read_text(encoding="utf-8")
    instructions = (target / "AGENTS.md").read_text()
    assert instructions == files("poser").joinpath("scaffold", "agents.md").read_text()
    assert "[POSER.md](POSER.md)" in instructions
    assert not instructions.startswith(GENERATED)
    assert (target / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    text = (target / "POSER.md").read_text()
    assert text == compose_guide(core, composition.instrument_notes(), composition.guide_parts())
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
    config.write_text(config.read_text().replace("factory patches", "custom sounds"))

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("setup was reconsulted")

    monkeypatch.setattr(init_workspace, "prepare", fail)
    assert guide.main(["--config", str(config)]) == 0
    assert "custom sounds" in (target / "POSER.md").read_text()
    assert resource.read_bytes() == original


def test_yaml_only_studio_gets_conditional_format_guidance(tmp_path: Path, plugin: Path) -> None:
    target = tmp_path / "studio"
    assert init_workspace.main(args(target, plugin)) == 0
    config = target / "poser.toml"
    config.write_text(config.read_text() + '\n[adapters]\nsong_formats = ["yaml"]\n')
    assert guide.main(["--config", str(config)]) == 0
    reference = (target / "POSER.md").read_text()
    assert "`adapters.song_formats` can select either or other formats" in reference
    assert "When the script format is enabled" in reference
    assert "uv run render-song <song>" in reference
    composition = load(config, target / "sounds", target / "renders")
    assert isinstance(load_song(target / "songs/demo.yaml", composition.song_formats()), Song)
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
    assert not target.exists()


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
    assert not target.exists()


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


def test_guide_preparation_does_not_spawn_processes(
    tmp_path: Path, plugin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected process")

    monkeypatch.setattr(subprocess, "Popen", fail)
    assert init_workspace.main(args(tmp_path / "project", plugin)) == 0
