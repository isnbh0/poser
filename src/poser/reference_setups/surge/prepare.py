"""Prepare the packaged Surge XT reference setup."""

import json
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from poser.core.ports import ConfigError
from poser.reference_setups.host import Host

DOC_URL = "https://github.com/isnbh0/poser/blob/main/src/poser/reference_setups/surge/README.md"
# macOS's VST3 folder and Surge XT's data folder, each under a user's or the system's Library.
MAC_VST3 = Path("Audio/Plug-Ins/VST3/Surge XT.vst3")
MAC_DATA = Path("Application Support/Surge XT")
# Steinberg's per-user and system-wide VST3 folders, in its priority order, each as
# (environment variable, folder under it).
WINDOWS_VST3_FOLDERS = (("LOCALAPPDATA", "Programs/Common/VST3"), ("CommonProgramFiles", "VST3"))
WINDOWS_BUNDLE = "Surge Synth Team/Surge XT.vst3"


def plugin_paths(host: Host) -> tuple[Path, ...]:
    """Return where Surge XT's VST3 bundle is installed on `host`, in search order.

    The per-user VST3 folder, then the system-wide one where the installer puts it: Steinberg's
    priority, so the search finds the bundle a VST3 host loads. On Windows, a folder whose
    environment variable is unset or empty is left out. Off Windows, the macOS folders.
    """
    if host.platform == "win32":
        roots = ((host.path(name), folder) for name, folder in WINDOWS_VST3_FOLDERS)
        return tuple(root / folder / WINDOWS_BUNDLE for root, folder in roots if root is not None)
    return (host.home / "Library" / MAC_VST3, host.library / MAC_VST3)


def preset_paths(plugin: Path, host: Host) -> tuple[Path, ...]:
    """Return the factory patch folders to search for the bundle `plugin` on `host`, in order:
    the `patches_factory` of the data folder Surge XT itself uses, so poser and the plugin agree
    on the patches."""
    data = windows_data(plugin, host) if host.platform == "win32" else mac_data(host)
    return () if data is None else (data / "patches_factory",)


def mac_data(host: Host) -> Path:
    """Return the data folder Surge XT uses on macOS.

    Follows Surge XT's own lookup (`SurgeStorage.cpp`): a nonempty `SURGE_DATA_HOME`; else
    `~/Library/Application Support/Surge XT` when it is a directory; else
    `/Library/Application Support/Surge XT`, where the installer puts it.
    """
    override = host.path("SURGE_DATA_HOME")
    if override is not None:
        return override
    user = host.home / "Library" / MAC_DATA
    return user if user.is_dir() else host.library / MAC_DATA


def windows_data(plugin: Path, host: Host) -> Path | None:
    """Return the data folder Surge XT uses on Windows for the bundle `plugin`, or None.

    Follows Surge XT's own lookup (`SurgeStorage.cpp`): a nonempty `SURGE_DATA_HOME`; else the
    first `SurgeXTData` folder from the binary's folder, `plugin/Contents/x86_64-win`, up to but
    excluding the drive root; else `%ProgramData%\\Surge XT` when it is a directory; else
    `%LOCALAPPDATA%\\Surge XT`, or None when `LOCALAPPDATA` is unset. The installer links a
    `SurgeXTData` beside its `Surge XT.vst3` to `%ProgramData%\\Surge XT`, and the portable zip
    keeps the real folder there.
    """
    override = host.path("SURGE_DATA_HOME")
    if override is not None:
        return override
    # Windows hosts other than x64 are refused by `poser.adapters.windows_host.check_windows_host`,
    # so the x64 bundle folder is the only one poser looks in.
    binary_folder = plugin / "Contents" / "x86_64-win"
    for folder in (binary_folder, *binary_folder.parents):
        if folder == folder.parent:
            break
        if (folder / "SurgeXTData").is_dir():
            return folder / "SurgeXTData"
    shared = host.path("ProgramData")
    if shared is not None and (shared / "Surge XT").is_dir():
        return shared / "Surge XT"
    user = host.path("LOCALAPPDATA")
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


def prepare(plugin: Path | None, presets: Path | None, host: Host) -> Setup:
    """Resolve paths, searching `host` for those not given, and produce a complete,
    self-contained configuration and demo."""
    if plugin is not None:
        plugin = plugin.expanduser().resolve()
        if not plugin.is_dir():
            raise ConfigError(f"plugin path is not a directory: {plugin}; see {DOC_URL}")
    else:
        candidates = plugin_paths(host)
        plugin = next((p for p in candidates if p.is_dir()), None)
        if plugin is None:
            if candidates:
                checked = ", ".join(str(p) for p in candidates)
                missing = f"Surge XT not found in checked locations: {checked}"
            else:
                unset = " and ".join(name for name, _ in WINDOWS_VST3_FOLDERS)
                missing = f"Surge XT not found: {unset}, which name the VST3 folders, are unset"
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
        presets = next((p.resolve() for p in preset_paths(plugin, host) if p.is_dir()), None)

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
