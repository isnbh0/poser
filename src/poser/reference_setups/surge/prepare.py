"""Prepare the packaged Surge XT reference setup."""

import json
import tomllib
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from poser.core.ports import ConfigError

DOC_URL = "https://github.com/isnbh0/poser/blob/main/src/poser/reference_setups/surge/README.md"
PLUGIN_PATHS = (
    Path("/Library/Audio/Plug-Ins/VST3/Surge XT.vst3"),
    Path.home() / "Library/Audio/Plug-Ins/VST3/Surge XT.vst3",
)
PRESET_PATHS = (
    Path("/Library/Application Support/Surge XT/patches_factory"),
    Path.home() / "Library/Application Support/Surge XT/patches_factory",
)


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
        plugin = next((p for p in PLUGIN_PATHS if p.is_dir()), None)
        if plugin is None:
            checked = ", ".join(str(p) for p in PLUGIN_PATHS)
            raise ConfigError(
                f"Surge XT not found in checked locations: {checked}. "
                f"Retry: uv run init-workspace --setup surge --plugin-path PATH. See {DOC_URL}"
            )
        plugin = plugin.resolve()
    if presets is not None:
        presets = presets.expanduser().resolve()
        if not presets.is_dir():
            raise ConfigError(f"presets path is not a directory: {presets}; see {DOC_URL}")
    else:
        presets = next((p.resolve() for p in PRESET_PATHS if p.is_dir()), None)

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
