"""Prepare the packaged Surge XT reference setup."""

import json
import os
import sys
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from poser.core.ports import ConfigError

DOC_URL = "https://github.com/isnbh0/poser/blob/main/src/poser/reference_setups/surge/README.md"
MAC_PLUGIN_PATHS = (
    Path("/Library/Audio/Plug-Ins/VST3/Surge XT.vst3"),
    Path.home() / "Library/Audio/Plug-Ins/VST3/Surge XT.vst3",
)
MAC_PRESET_PATHS = (
    Path("/Library/Application Support/Surge XT/patches_factory"),
    Path.home() / "Library/Application Support/Surge XT/patches_factory",
)
# The system and per-user VST3 folders, each as (environment variable, folder under it).
WINDOWS_VST3_FOLDERS = (("CommonProgramFiles", "VST3"), ("LOCALAPPDATA", "Programs/Common/VST3"))
WINDOWS_BUNDLE = "Surge Synth Team/Surge XT.vst3"


def _environment_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def plugin_paths() -> tuple[Path, ...]:
    """Return where Surge XT's installers put its VST3 bundle on this platform, in search order.

    On Windows, the installer's system-wide folder comes first, then the same layout in the
    per-user VST3 folder; a folder whose environment variable is unset or empty is left out.
    Elsewhere, the macOS locations.
    """
    if sys.platform != "win32":
        return MAC_PLUGIN_PATHS
    roots = ((_environment_path(name), folder) for name, folder in WINDOWS_VST3_FOLDERS)
    return tuple(root / folder / WINDOWS_BUNDLE for root, folder in roots if root is not None)


def preset_paths(plugin: Path) -> tuple[Path, ...]:
    """Return the factory patch folders to search for the bundle `plugin`, in order.

    On Windows, the `patches_factory` of the data folder Surge XT itself uses (`windows_data`),
    so poser and the plugin agree on the patches. Elsewhere, the macOS locations.
    """
    if sys.platform != "win32":
        return MAC_PRESET_PATHS
    data = windows_data(plugin)
    return () if data is None else (data / "patches_factory",)


def windows_data(plugin: Path) -> Path | None:
    """Return the data folder Surge XT uses on Windows for the bundle `plugin`, or None.

    Follows Surge XT's own lookup (`SurgeStorage.cpp`): a nonempty `SURGE_DATA_HOME`; else the
    first `SurgeXTData` folder from the binary's folder, `plugin/Contents/x86_64-win`, up to but
    excluding the drive root, as the portable zip lays it out; else `%ProgramData%\\Surge XT`
    when it is a directory; else `%LOCALAPPDATA%\\Surge XT`, or None when `LOCALAPPDATA` is unset.
    """
    override = _environment_path("SURGE_DATA_HOME")
    if override is not None:
        return override
    binary_folder = plugin / "Contents" / "x86_64-win"
    for folder in (binary_folder, *binary_folder.parents):
        if folder == folder.parent:
            break
        if (folder / "SurgeXTData").is_dir():
            return folder / "SurgeXTData"
    shared = _environment_path("ProgramData")
    if shared is not None and (shared / "Surge XT").is_dir():
        return shared / "Surge XT"
    user = _environment_path("LOCALAPPDATA")
    return None if user is None else user / "Surge XT"


def _toml_string(value: str) -> str:
    # JSON string escapes are valid TOML, except literal DEL is forbidden by TOML.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


@dataclass(frozen=True)
class Setup:
    config: str
    demo: str
    plugin: Path
    presets: Path | None


def prepare(plugin: Path | None, presets: Path | None) -> Setup:
    """Resolve paths and produce a complete, self-contained configuration and demo."""
    if plugin is not None:
        plugin = plugin.expanduser().resolve()
        if not plugin.is_dir():
            raise ConfigError(f"plugin path is not a directory: {plugin}; see {DOC_URL}")
    else:
        candidates = plugin_paths()
        plugin = next((p for p in candidates if p.is_dir()), None)
        if plugin is None:
            if candidates:
                checked = ", ".join(str(p) for p in candidates)
                missing = f"Surge XT not found in checked locations: {checked}"
            else:
                missing = "Surge XT not found: this platform has no standard Surge XT location"
            raise ConfigError(
                f"{missing}. "
                f"Retry: uv run init-workspace --setup surge --plugin-path PATH. See {DOC_URL}"
            )
        plugin = plugin.resolve()
    if presets is not None:
        presets = presets.expanduser().resolve()
        if not presets.is_dir():
            raise ConfigError(f"presets path is not a directory: {presets}; see {DOC_URL}")
    else:
        presets = next((p.resolve() for p in preset_paths(plugin) if p.is_dir()), None)

    resources = files(__package__)
    template = tomllib.loads(resources.joinpath("poser.toml").read_text(encoding="utf-8"))
    instrument = template["instruments"]["surge"]
    config = (
        "# Surge XT reference instrument; run `uv run update-guide` after editing.\n"
        "[instruments.surge]\n"
        f"adapter = {json.dumps(instrument['adapter'], ensure_ascii=False)}\n"
        f"path = {_toml_string(str(plugin))}\n"
        f"guide = {json.dumps(instrument['guide'], ensure_ascii=False)}\n"
    )
    if presets is not None:
        config += "\n[instruments.surge.sources]\n"
        config += f"presets = {_toml_string(str(presets))}\n"
    demo = resources.joinpath("demo.yaml").read_text(encoding="utf-8")
    return Setup(config, demo, plugin, presets)
