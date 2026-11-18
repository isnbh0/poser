"""Explicit reference setup selection."""

from pathlib import Path

from poser.reference_setups.host import Host
from poser.reference_setups.surge import prepare as surge

SETUPS = ("surge",)


def prepare(name: str, plugin: Path | None, presets: Path | None, host: Host) -> surge.Setup:
    """Prepare the selected setup, searching `host` for paths not given, without writing studio
    files."""
    if name == "surge":
        return surge.prepare(plugin, presets, host)
    raise ValueError(f"unknown setup: {name}")
