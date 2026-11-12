"""Reference setup preparation does not require a real plugin."""

import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from poser.cli.init_workspace import main
from poser.compose import parse_config
from poser.core.ports import ConfigError
from poser.reference_setups.surge import prepare as surge


def test_explicit_paths_and_optional_presets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / 'plugin "quoted"\x7f🎹.vst3'
    plugin.mkdir()
    monkeypatch.setattr(surge, "PRESET_PATHS", ())
    setup = surge.prepare(plugin, None)
    config = parse_config(setup.config, tmp_path / "future" / "poser.toml")
    assert config.instruments[0].spec.options["path"] == str(plugin)
    assert "sources" not in config.instruments[0].spec.options
    template = tomllib.loads(
        files("poser.reference_setups.surge").joinpath("poser.toml").read_text()
    )
    assert config.instruments[0].guide == template["instruments"]["surge"]["guide"]
    presets = tmp_path / "factory patches"
    presets.mkdir()
    setup = surge.prepare(plugin, presets)
    config = parse_config(setup.config, tmp_path / "poser.toml")
    assert config.instruments[0].spec.options["sources"] == {"presets": str(presets)}
    assert setup.demo == files("poser.reference_setups.surge").joinpath("demo.yaml").read_text(
        encoding="utf-8"
    )


def test_discovery_and_invalid_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = tmp_path / "first.vst3", tmp_path / "second.vst3"
    second.mkdir()
    monkeypatch.setattr(surge, "PLUGIN_PATHS", (first, second))
    monkeypatch.setattr(surge, "PRESET_PATHS", ())
    assert surge.prepare(None, None).plugin == second
    first.mkdir()
    assert surge.prepare(None, None).plugin == first
    with pytest.raises(ConfigError, match="plugin path"):
        surge.prepare(tmp_path / "bad", None)
    with pytest.raises(ConfigError, match="presets path"):
        surge.prepare(first, tmp_path / "bad")


def test_failures_do_not_mutate_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surge, "PLUGIN_PATHS", ())
    ignore = tmp_path / ".gitignore"
    ignore.write_text("existing\n")
    assert main([str(tmp_path), "--setup", "surge"]) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [".gitignore", ".poser-init"]
    namespace = tmp_path / ".poser-init"
    assert sorted(p.name for p in namespace.iterdir()) == ["identity", "lock"]
    lock_inode = (namespace / "lock").stat().st_ino
    assert ignore.read_text() == "existing\n"
    plugin = tmp_path / "plugin.vst3"
    plugin.mkdir()
    (tmp_path / "AGENTS.md").write_text("owned")
    assert main([str(tmp_path), "--setup", "surge", "--plugin-path", str(plugin)]) == 2
    assert ignore.read_text() == "existing\n"
    assert (tmp_path / "AGENTS.md").read_text() == "owned"
    assert (namespace / "lock").stat().st_ino == lock_inode
    assert sorted(p.name for p in namespace.iterdir()) == ["identity", "lock"]


def test_gitignore_symlink_refused(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin.vst3"
    plugin.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").symlink_to(outside)
    assert main([str(project), "--setup", "surge", "--plugin-path", str(plugin)]) == 2
    assert outside.read_text() == "untouched"
    assert sorted(p.name for p in project.iterdir()) == [".gitignore", ".poser-init"]
    assert (project / ".gitignore").is_symlink()
    assert (project / ".gitignore").resolve() == outside
    namespace = project / ".poser-init"
    assert sorted(p.name for p in namespace.iterdir()) == ["identity", "lock"]
