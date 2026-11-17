"""Reference setup preparation does not require a real plugin."""

import sys
import tomllib
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from poser.cli.init_workspace import main
from poser.compose import parse_config
from poser.core.meter import Meter, MeterSpan
from poser.core.ports import ConfigError
from poser.core.song import parse_song
from poser.reference_setups.surge import prepare as surge
from poser.reference_setups.surge.showcase import build


def test_explicit_paths_and_optional_presets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "plugin\x7f🎹 'quoted'.vst3"
    plugin.mkdir()
    monkeypatch.setattr(surge, "preset_paths", lambda _: ())
    setup = surge.prepare(plugin, None)
    config = parse_config(setup.config, tmp_path / "future" / "poser.toml")
    assert config.instruments[0].spec.options["path"] == str(plugin)
    assert "sources" not in config.instruments[0].spec.options
    template = tomllib.loads(
        files("poser.reference_setups.surge").joinpath("poser.toml").read_text(encoding="utf-8")
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
    monkeypatch.setattr(surge, "plugin_paths", lambda: (first, second))
    monkeypatch.setattr(surge, "preset_paths", lambda _: ())
    assert surge.prepare(None, None).plugin == second
    first.mkdir()
    assert surge.prepare(None, None).plugin == first
    with pytest.raises(ConfigError, match="plugin path"):
        surge.prepare(tmp_path / "bad", None)
    with pytest.raises(ConfigError, match="presets path"):
        surge.prepare(first, tmp_path / "bad")


@pytest.mark.parametrize(
    "value", ['"', "\\", "'", "\x7f", "\t", "🎹", "C:\\Users\\Ann\\Surge XT.vst3"]
)
def test_toml_string_round_trips_what_windows_paths_hold(value: str) -> None:
    assert tomllib.loads(f"v = {surge._toml_string(value)}")["v"] == value


WINDOWS_VARIABLES = ("CommonProgramFiles", "LOCALAPPDATA", "ProgramData", "SURGE_DATA_HOME")


def simulate_windows(monkeypatch: pytest.MonkeyPatch, **variables: Path) -> None:
    """Simulate Windows with exactly the given Surge-related environment variables set."""
    monkeypatch.setattr(sys, "platform", "win32")
    for name in WINDOWS_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, str(value))


def test_windows_searches_the_per_user_then_the_installer_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common, local = tmp_path / "Common Files", tmp_path / "Local"
    bundle = Path("Surge Synth Team", "Surge XT.vst3")
    simulate_windows(monkeypatch, CommonProgramFiles=common, LOCALAPPDATA=local)
    assert surge.plugin_paths() == (
        local / "Programs" / "Common" / "VST3" / bundle,
        common / "VST3" / bundle,
    )
    monkeypatch.delenv("LOCALAPPDATA")
    assert surge.plugin_paths() == (common / "VST3" / bundle,)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert surge.plugin_paths() == surge.MAC_PLUGIN_PATHS
    assert surge.preset_paths(tmp_path / "Surge XT.vst3") == surge.MAC_PRESET_PATHS


# Each case: the data folders to create with factory patches, those to create without them,
# whether `SURGE_DATA_HOME` names `Override`, and whose patches win. Folders are relative to the
# test's directory, which holds the bundle `VST3/Team/Surge XT.vst3`, `%ProgramData%` as
# `ProgramData`, and `%LOCALAPPDATA%` as `Local`.
BINARY_FOLDER = "VST3/Team/Surge XT.vst3/Contents/x86_64-win"
DATA_LOOKUPS: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool, str | None]] = {
    "surge-data-home-first": (
        ("Override", "VST3/Team/SurgeXTData", "ProgramData/Surge XT"),
        (),
        True,
        "Override",
    ),
    "in-the-binary-s-folder": (
        (f"{BINARY_FOLDER}/SurgeXTData", "VST3/Team/SurgeXTData"),
        (),
        False,
        f"{BINARY_FOLDER}/SurgeXTData",
    ),
    "beside-the-bundle-before-program-data": (
        ("VST3/Team/SurgeXTData", "ProgramData/Surge XT"),
        (),
        False,
        "VST3/Team/SurgeXTData",
    ),
    "two-folders-above-the-bundle": (("VST3/SurgeXTData",), (), False, "VST3/SurgeXTData"),
    "portable-data-without-patches-hides-program-data": (
        ("ProgramData/Surge XT",),
        ("VST3/Team/SurgeXTData",),
        False,
        None,
    ),
    "program-data-before-local-app-data": (
        ("ProgramData/Surge XT", "Local/Surge XT"),
        (),
        False,
        "ProgramData/Surge XT",
    ),
    "program-data-without-patches-hides-local-app-data": (
        ("Local/Surge XT",),
        ("ProgramData/Surge XT",),
        False,
        None,
    ),
    "local-app-data-without-program-data": (("Local/Surge XT",), (), False, "Local/Surge XT"),
    "none": ((), (), False, None),
}


@pytest.mark.parametrize(
    ("patched", "bare", "override", "winner"), DATA_LOOKUPS.values(), ids=DATA_LOOKUPS.keys()
)
def test_windows_presets_follow_surge_s_data_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patched: tuple[str, ...],
    bare: tuple[str, ...],
    override: bool,
    winner: str | None,
) -> None:
    for folder in patched:
        (tmp_path / folder / "patches_factory").mkdir(parents=True)
    for folder in bare:
        (tmp_path / folder).mkdir(parents=True)
    bundle = tmp_path / "VST3" / "Team" / "Surge XT.vst3"
    bundle.mkdir(parents=True, exist_ok=True)
    variables = {"ProgramData": tmp_path / "ProgramData", "LOCALAPPDATA": tmp_path / "Local"}
    if override:
        variables["SURGE_DATA_HOME"] = tmp_path / "Override"
    simulate_windows(monkeypatch, **variables)
    expected = None if winner is None else (tmp_path / winner / "patches_factory").resolve()
    assert surge.prepare(bundle, None).presets == expected


def test_windows_discovery_writes_the_installed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer's layout: the bundle in the system-wide VST3 folder, the data in
    `%ProgramData%`, and a link to the data beside the bundle."""
    team = tmp_path / "Common Files" / "VST3" / "Surge Synth Team"
    bundle = team / "Surge XT.vst3"
    (bundle / "Contents" / "x86_64-win").mkdir(parents=True)
    patches = tmp_path / "ProgramData" / "Surge XT" / "patches_factory"
    patches.mkdir(parents=True)
    (team / "SurgeXTData").symlink_to(patches.parent, target_is_directory=True)
    simulate_windows(
        monkeypatch,
        CommonProgramFiles=tmp_path / "Common Files",
        LOCALAPPDATA=tmp_path / "Local",
        ProgramData=tmp_path / "ProgramData",
    )
    setup = surge.prepare(None, None)
    assert (setup.plugin, setup.presets) == (bundle.resolve(), patches.resolve())
    config = parse_config(setup.config, tmp_path / "poser.toml")
    options = config.instruments[0].spec.options
    assert options["path"] == str(bundle.resolve())
    assert options["sources"] == {"presets": str(patches.resolve())}


def test_windows_without_vst3_folder_variables_names_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulate_windows(monkeypatch)
    message = "LOCALAPPDATA and CommonProgramFiles, which name the VST3 folders, are unset"
    with pytest.raises(ConfigError, match=message) as caught:
        surge.prepare(None, None)
    assert "--plugin-path" in str(caught.value)


def test_failures_do_not_mutate_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surge, "plugin_paths", lambda: ())
    ignore = tmp_path / ".gitignore"
    ignore.write_text("existing\n", encoding="utf-8", newline="\n")
    assert main([str(tmp_path), "--setup", "surge"]) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == [".gitignore"]
    assert ignore.read_text(encoding="utf-8") == "existing\n"
    plugin = tmp_path / "plugin.vst3"
    plugin.mkdir()
    (tmp_path / "AGENTS.md").write_text("owned", encoding="utf-8", newline="\n")
    assert main([str(tmp_path), "--setup", "surge", "--plugin-path", str(plugin)]) == 2
    assert ignore.read_text(encoding="utf-8") == "existing\n"
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "owned"


def test_gitignore_symlink_refused(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin.vst3"
    plugin.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched", encoding="utf-8", newline="\n")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".gitignore").symlink_to(outside)
    assert main([str(project), "--setup", "surge", "--plugin-path", str(plugin)]) == 2
    assert outside.read_text(encoding="utf-8") == "untouched"
    assert sorted(p.name for p in project.iterdir()) == [".gitignore"]
    assert (project / ".gitignore").is_symlink()
    assert (project / ".gitignore").resolve() == outside


def test_the_packaged_songs_declare_a_meter() -> None:
    four = Meter([MeterSpan(0, 4)])
    showcase = build()
    assert showcase.meter == four
    assert len(four.bars(63.5)) == 16
    demo = files("poser.reference_setups.surge").joinpath("demo.yaml").read_text(encoding="utf-8")
    assert parse_song(yaml.safe_load(demo)).meter == four
