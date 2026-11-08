"""Installed presets as the old commands name them: a plugin's `.fxp` files, used as sounds.

A preset's name is `<plugin name>:<path in the plugin's presets directory, without .fxp>`, such as
`Serum:Bass/BA FunkMogue [CFA]`. Rendering builds the plugin's `raw_state` from the preset's chunk
with `poser.adapters.pedalboard.formats` and `.state`, and restores it like a saved sound.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from poser.adapters.pedalboard.formats import FxpFormat, PresetError
from poser.adapters.pedalboard.sources import PRESET_DESCRIPTION, walk

# Only for annotations: importing `plugin_check` at run time would load `mido`.
if TYPE_CHECKING:
    from poser.plugin_check import PluginSpec


def preset_files(spec: "PluginSpec") -> dict[str, Path]:
    """Map each of a plugin's preset names to its file.

    Lookup goes through this listing, not `Path.exists`, because a default macOS volume would
    match `Serum:bass/Deep` to `Bass/Deep.fxp`.
    """
    if spec.presets is None:
        return {}
    root = Path(spec.presets)
    if not root.exists():
        raise PresetError(f"presets directory {root} for {spec.name!r} does not exist")
    if not root.is_dir():
        raise PresetError(f"presets path {root} for {spec.name!r} is not a directory")
    return {
        f"{spec.name}:{relative[: -len(path.suffix)]}": path
        for relative, path in walk(root, {FxpFormat().suffix}).items()
    }


def preset_description(name: str) -> str:
    """Describe a preset by its folder: `Serum:Bass (Hard)/X` is `Bass (Hard) preset`."""
    folder = name.split(":", 1)[1].rpartition("/")[0]
    return f"{folder} {PRESET_DESCRIPTION}" if folder else PRESET_DESCRIPTION
