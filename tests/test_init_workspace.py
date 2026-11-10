"""Tests for `init-workspace`, which writes the scaffold into a new workspace."""

import tomllib
from importlib.resources import files
from pathlib import Path

import pytest

from poser.adapters.pedalboard.config import from_options
from poser.adapters.song_formats import SONG_FORMATS
from poser.cli import guide, init_workspace, sounds
from poser.cli.common import EXIT_ALL_PASS
from poser.cli.init_workspace import (
    GITIGNORE_RESOURCE,
    ONBOARDING_RESOURCE,
    SCAFFOLD_FILES,
    WRITTEN_FILES,
)
from poser.cli.workspace import Workspace, find_workspace
from poser.compose import load
from poser.core.guide import GENERATED, compose_guide
from poser.core.loading import load_song
from poser.core.names import Identifier


def resource(name: str) -> bytes:
    return (files("poser") / "scaffold" / name).read_bytes()


def detect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    serum: bool = False,
    serum_presets: bool = False,
    surge_plugin: bool = False,
    surge_patches: bool = False,
) -> tuple[Path, Path]:
    """Patch every detection constant to controlled tmp_path candidates."""
    plugin = tmp_path / "serum.vst3"
    if serum:
        plugin.parent.mkdir(parents=True, exist_ok=True)
        plugin.write_bytes(b"")
    monkeypatch.setattr(init_workspace, "SCAFFOLD_PLUGIN_PATH", plugin)
    presets = tmp_path / "serum-presets"
    if serum_presets:
        presets.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(init_workspace, "SERUM_PRESET_PATH", presets)
    all_users_surge = tmp_path / "all-users-surge.vst3"
    home_surge = tmp_path / "home-surge.vst3"
    if surge_plugin:
        home_surge.parent.mkdir(parents=True, exist_ok=True)
        home_surge.write_bytes(b"")
    monkeypatch.setattr(init_workspace, "SURGE_PLUGIN_PATHS", (all_users_surge, home_surge))
    all_users_patches = tmp_path / "all-users-patches"
    home_patches = tmp_path / "home-patches"
    if surge_patches:
        home_patches.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(init_workspace, "SURGE_PATCHES_PATHS", (all_users_patches, home_patches))
    return home_surge, home_patches


def test_every_scaffold_resource_exists() -> None:
    for name in (*SCAFFOLD_FILES, GITIGNORE_RESOURCE, ONBOARDING_RESOURCE):
        assert (files("poser") / "scaffold" / name).is_file(), name


def test_init_creates_the_directory_and_writes_the_scaffold(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "new"
    detect(monkeypatch, target, serum=True, serum_presets=True)

    assert init_workspace.main([str(target)]) == init_workspace.EXIT_OK

    for name, path in SCAFFOLD_FILES.items():
        assert (target / path).read_bytes() == resource(name), path
    assert (target / ".gitignore").read_bytes() == resource(GITIGNORE_RESOURCE)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[:6] == [
        "AGENTS.md",
        "CLAUDE.md",
        "poser.toml",
        "songs/demo.yaml",
        "Edit poser.toml to match this machine's plugins, then run: uv run check-instruments",
        (
            "songs/demo.yaml names the scaffold's instrument; if you changed poser.toml, "
            "point its sound names at yours (uv run list-sounds)"
        ),
    ]
    assert "detected" not in out
    assert not (target / "sounds").exists()
    assert not (target / "renders").exists()


def test_init_defaults_to_the_working_directory(tmp_path: Path) -> None:
    assert init_workspace.main([]) == init_workspace.EXIT_OK

    assert (tmp_path / "songs" / "demo.yaml").is_file()


def test_init_prints_the_free_synth_pointer_without_the_plugin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect(monkeypatch, tmp_path)

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK

    assert capsys.readouterr().out.endswith(resource(ONBOARDING_RESOURCE).decode())


def test_init_prints_no_pointer_with_the_plugin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect(monkeypatch, tmp_path, serum=True, serum_presets=True)

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK

    out = capsys.readouterr().out
    assert len(out.splitlines()) == 6
    assert "Surge XT" not in out


def test_the_scaffold_plugin_path_matches_the_scaffold_config() -> None:
    config = tomllib.loads(resource("poser.toml").decode())

    assert Path(config["instruments"]["serum"]["path"]) == init_workspace.SCAFFOLD_PLUGIN_PATH


def test_the_scaffold_surge_tables_resolve_to_the_documented_defaults(tmp_path: Path) -> None:
    lines = resource("poser.toml").decode().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# No Serum"))
    uncommented = [
        line[2:] if line.startswith("# ") and ("=" in line or line[2:].startswith("[")) else ""
        for line in lines[start:]
    ]
    config = tomllib.loads("\n".join(uncommented))["instruments"]["surge"]
    del config["adapter"]  # `compose` strips it before the factory sees the options.
    instrument = from_options("surge", config, tmp_path)

    assert instrument.path == Path("/Library/Audio/Plug-Ins/VST3/Surge XT.vst3")
    assert instrument.sources == {
        Identifier("presets"): Path("/Library/Application Support/Surge XT/patches_factory")
    }


def test_the_surge_constants_match_the_scaffold_tables() -> None:
    lines = resource("poser.toml").decode().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# No Serum"))
    uncommented = [
        line[2:] if line.startswith("# ") and ("=" in line or line[2:].startswith("[")) else ""
        for line in lines[start:]
    ]
    surge = tomllib.loads("\n".join(uncommented))["instruments"]["surge"]
    serum = tomllib.loads(resource("poser.toml").decode())["instruments"]["serum"]

    assert init_workspace.SURGE_PLUGIN_PATHS[0] == Path(surge["path"])
    assert init_workspace.SURGE_PATCHES_PATHS[0] == Path(surge["sources"]["presets"])
    assert Path(serum["sources"]["presets"]) == init_workspace.SERUM_PRESET_PATH


def test_init_omits_the_serum_presets_when_the_folder_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect(monkeypatch, tmp_path, serum=True)

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK

    table = (
        "[instruments.serum.sources]\n"
        'presets = "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets"\n\n'
    )
    expected = resource("poser.toml").decode().replace(table, "")
    assert (tmp_path / "poser.toml").read_text() == expected
    out = capsys.readouterr().out
    assert "Wrote poser.toml from detected serum (preset source omitted)" in out


def test_init_writes_detected_surge_when_serum_is_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_surge, home_patches = detect(monkeypatch, tmp_path, surge_plugin=True, surge_patches=True)

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK

    config = tomllib.loads((tmp_path / "poser.toml").read_text())
    surge = config["instruments"]["surge"]
    assert surge["path"] == str(home_surge)
    assert surge["sources"]["presets"] == str(home_patches)
    assert "serum" not in config["instruments"]
    for track in load_song(tmp_path / "songs" / "demo.yaml", SONG_FORMATS).tracks:
        assert str(track.sound) == "surge"
    out = capsys.readouterr().out
    assert "Wrote poser.toml from detected surge" in out
    assert "Wrote songs/demo.yaml from detected surge" in out
    assert resource(ONBOARDING_RESOURCE).decode() not in out
    assert "check-instruments" in out


def test_init_omits_the_surge_presets_when_no_candidate_is_a_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect(monkeypatch, tmp_path, surge_plugin=True)

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK

    config = tomllib.loads((tmp_path / "poser.toml").read_text())
    assert "sources" not in config["instruments"]["surge"]
    out = capsys.readouterr().out
    assert "Wrote poser.toml from detected surge (preset source omitted)" in out


def test_init_prefers_the_first_existing_surge_candidates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_surge, home_patches = detect(monkeypatch, tmp_path, surge_plugin=True, surge_patches=True)

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK

    config = tomllib.loads((tmp_path / "poser.toml").read_text())
    assert config["instruments"]["surge"]["path"] == str(home_surge)
    assert config["instruments"]["surge"]["sources"]["presets"] == str(home_patches)
    assert capsys.readouterr().out


def test_init_writes_both_instruments_when_both_are_detected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_surge, home_patches = detect(
        monkeypatch, tmp_path, serum=True, serum_presets=True, surge_plugin=True, surge_patches=True
    )

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK

    config = tomllib.loads((tmp_path / "poser.toml").read_text())
    assert list(config["instruments"]) == ["serum", "surge"]
    assert config["instruments"]["surge"]["path"] == str(home_surge)
    assert config["instruments"]["surge"]["sources"]["presets"] == str(home_patches)
    assert (tmp_path / "songs" / "demo.yaml").read_text() == resource("demo.yaml").decode()
    out = capsys.readouterr().out
    assert "Wrote poser.toml from detected surge" in out
    assert "Wrote songs/demo.yaml" not in out


def test_init_composes_a_current_guide_and_lists_sounds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect(monkeypatch, tmp_path, surge_plugin=True, surge_patches=True)

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK
    capsys.readouterr()

    assert guide.main(["--config", str(tmp_path / "poser.toml"), "--check"]) == EXIT_ALL_PASS
    assert sounds.list_main(["--config", str(tmp_path / "poser.toml")]) == EXIT_ALL_PASS
    assert "surge" in capsys.readouterr().out


def test_init_ignores_surge_patches_without_a_surge_plugin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detect(monkeypatch, tmp_path, surge_patches=True)

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_OK

    for name, path in SCAFFOLD_FILES.items():
        assert (tmp_path / path).read_bytes() == resource(name), path
    assert resource(ONBOARDING_RESOURCE).decode() in capsys.readouterr().out


def test_init_merges_an_existing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".venv/\nnode_modules")

    init_workspace.main([str(tmp_path)])

    assert (tmp_path / ".gitignore").read_text() == (
        ".venv/\nnode_modules\nrenders/\n__pycache__/\n.DS_Store\n"
    )


def test_init_writes_the_composed_guide(tmp_path: Path) -> None:
    init_workspace.main([str(tmp_path)])

    guide = (tmp_path / "AGENTS.md").read_text()
    composition = load(tmp_path / "poser.toml", tmp_path / "sounds", tmp_path / "renders")
    core = resource("guide.md").decode()
    assert guide == compose_guide(core, composition.instrument_notes(), composition.guide_parts())
    assert guide.startswith(GENERATED + "\n")
    for heading in (
        "## Instruments here",
        "## Pedalboard instruments",
        "## Where results are kept",
    ):
        assert f"\n{heading}\n" in guide
    assert not (tmp_path / "sounds").exists()
    assert not (tmp_path / "renders").exists()


@pytest.mark.parametrize("existing", list(WRITTEN_FILES))
def test_existing_target_refuses_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], existing: str
) -> None:
    (tmp_path / existing).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / existing).write_text("mine")

    assert init_workspace.main([str(tmp_path)]) == init_workspace.EXIT_CONFLICT

    assert capsys.readouterr().err == f"init-workspace: {tmp_path / existing} exists\n"
    assert (tmp_path / existing).read_text() == "mine"
    for path in WRITTEN_FILES:
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
