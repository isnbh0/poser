"""Reference setup preparation does not require a real plugin."""

import tomllib
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from hosts import isolated_host, mac_host, windows_host

from poser.cli.init_workspace import main
from poser.compose import parse_config
from poser.core.meter import Meter, MeterSpan
from poser.core.ports import ConfigError
from poser.core.song import parse_song
from poser.reference_setups.surge import prepare as surge
from poser.reference_setups.surge.showcase import build


def test_explicit_paths_and_optional_presets(tmp_path: Path) -> None:
    host = isolated_host(tmp_path / "host")
    plugin = tmp_path / "plugin\x7f🎹 'quoted'.vst3"
    plugin.mkdir()
    setup = surge.prepare(plugin, None, host)
    config = parse_config(setup.config, tmp_path / "future" / "poser.toml")
    assert config.instruments[0].spec.options["path"] == str(plugin)
    assert "sources" not in config.instruments[0].spec.options
    template = tomllib.loads(
        files("poser.reference_setups.surge").joinpath("poser.toml").read_text(encoding="utf-8")
    )
    assert config.instruments[0].guide == template["instruments"]["surge"]["guide"]
    presets = tmp_path / "factory patches"
    presets.mkdir()
    setup = surge.prepare(plugin, presets, host)
    config = parse_config(setup.config, tmp_path / "poser.toml")
    assert config.instruments[0].spec.options["sources"] == {"presets": str(presets)}
    assert setup.demo == files("poser.reference_setups.surge").joinpath("demo.yaml").read_text(
        encoding="utf-8"
    )


def test_macos_finds_the_per_user_bundle_before_the_installer_s(tmp_path: Path) -> None:
    """Steinberg's priority, so the search finds the bundle a VST3 host loads."""
    host = mac_host(tmp_path)
    user = tmp_path / "home" / "Library" / "Audio" / "Plug-Ins" / "VST3" / "Surge XT.vst3"
    system = tmp_path / "Library" / "Audio" / "Plug-Ins" / "VST3" / "Surge XT.vst3"
    assert surge.plugin_paths(host) == (user, system)
    system.mkdir(parents=True)
    assert surge.prepare(None, None, host).plugin == system
    user.mkdir(parents=True)
    assert surge.prepare(None, None, host).plugin == user


def test_explicit_paths_must_be_directories(tmp_path: Path) -> None:
    host = isolated_host(tmp_path / "host")
    plugin = tmp_path / "plugin.vst3"
    plugin.mkdir()
    with pytest.raises(ConfigError, match="plugin path"):
        surge.prepare(tmp_path / "bad", None, host)
    with pytest.raises(ConfigError, match="presets path"):
        surge.prepare(plugin, tmp_path / "bad", host)


# Each case: the data folders to create with factory patches, those to create without them,
# whether `SURGE_DATA_HOME` names `Override`, and whose patches win. `User` is
# `~/Library/Application Support/Surge XT` and `System` is `/Library/Application Support/Surge XT`.
MAC_DATA_LOOKUPS: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool, str | None]] = {
    "surge-data-home-first": (("Override", "User", "System"), (), True, "Override"),
    "user-before-system": (("User", "System"), (), False, "User"),
    "user-without-patches-hides-system": (("System",), ("User",), False, None),
    "system-without-user": (("System",), (), False, "System"),
    "none": ((), (), False, None),
}
MAC_DATA_FOLDERS = {
    "Override": Path("Override"),
    "User": Path("home/Library/Application Support/Surge XT"),
    "System": Path("Library/Application Support/Surge XT"),
}


@pytest.mark.parametrize(
    ("patched", "bare", "override", "winner"),
    MAC_DATA_LOOKUPS.values(),
    ids=MAC_DATA_LOOKUPS.keys(),
)
def test_macos_presets_follow_surge_s_data_lookup(
    tmp_path: Path,
    patched: tuple[str, ...],
    bare: tuple[str, ...],
    override: bool,
    winner: str | None,
) -> None:
    for name in patched:
        (tmp_path / MAC_DATA_FOLDERS[name] / "patches_factory").mkdir(parents=True)
    for name in bare:
        (tmp_path / MAC_DATA_FOLDERS[name]).mkdir(parents=True)
    plugin = tmp_path / "Surge XT.vst3"
    plugin.mkdir()
    variables = {"SURGE_DATA_HOME": tmp_path / "Override"} if override else {}
    expected = None if winner is None else tmp_path / MAC_DATA_FOLDERS[winner] / "patches_factory"
    assert surge.prepare(plugin, None, mac_host(tmp_path, **variables)).presets == expected


@pytest.mark.parametrize(
    "value", ['"', "\\", "'", "\x7f", "\t", "🎹", "C:\\Users\\Ann\\Surge XT.vst3"]
)
def test_toml_string_round_trips_what_windows_paths_hold(value: str) -> None:
    assert tomllib.loads(f"v = {surge._toml_string(value)}")["v"] == value


def test_windows_searches_the_per_user_then_the_installer_folder(tmp_path: Path) -> None:
    common, local = tmp_path / "Common Files", tmp_path / "Local"
    bundle = Path("Surge Synth Team", "Surge XT.vst3")
    assert surge.plugin_paths(
        windows_host(tmp_path, CommonProgramFiles=common, LOCALAPPDATA=local)
    ) == (
        local / "Programs" / "Common" / "VST3" / bundle,
        common / "VST3" / bundle,
    )
    assert surge.plugin_paths(windows_host(tmp_path, CommonProgramFiles=common)) == (
        common / "VST3" / bundle,
    )


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
    expected = None if winner is None else (tmp_path / winner / "patches_factory").resolve()
    assert surge.prepare(bundle, None, windows_host(tmp_path, **variables)).presets == expected


def test_windows_discovery_writes_the_installed_paths(tmp_path: Path) -> None:
    """The installer's layout: the bundle in the system-wide VST3 folder, the data in
    `%ProgramData%`, and a link to the data beside the bundle."""
    team = tmp_path / "Common Files" / "VST3" / "Surge Synth Team"
    bundle = team / "Surge XT.vst3"
    (bundle / "Contents" / "x86_64-win").mkdir(parents=True)
    patches = tmp_path / "ProgramData" / "Surge XT" / "patches_factory"
    patches.mkdir(parents=True)
    (team / "SurgeXTData").symlink_to(patches.parent, target_is_directory=True)
    host = windows_host(
        tmp_path,
        CommonProgramFiles=tmp_path / "Common Files",
        LOCALAPPDATA=tmp_path / "Local",
        ProgramData=tmp_path / "ProgramData",
    )
    setup = surge.prepare(None, None, host)
    assert (setup.plugin, setup.presets) == (bundle.resolve(), patches.resolve())
    config = parse_config(setup.config, tmp_path / "poser.toml")
    options = config.instruments[0].spec.options
    assert options["path"] == str(bundle.resolve())
    assert options["sources"] == {"presets": str(patches.resolve())}


def test_windows_without_vst3_folder_variables_names_the_retry(tmp_path: Path) -> None:
    message = "LOCALAPPDATA and CommonProgramFiles, which name the VST3 folders, are unset"
    with pytest.raises(ConfigError, match=message) as caught:
        surge.prepare(None, None, windows_host(tmp_path))
    assert "--plugin-path" in str(caught.value)


def test_failures_do_not_mutate_project(tmp_path: Path) -> None:
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
