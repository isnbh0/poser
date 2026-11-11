"""Explicit reference setup selection."""

from pathlib import Path

from poser.reference_setups.surge import prepare as surge

SETUPS = ("surge",)


def prepare(name: str, plugin: Path | None, presets: Path | None) -> surge.Setup:
    """Prepare the selected setup without writing studio files."""
    if name == "surge":
        return surge.prepare(plugin, presets)
    raise ValueError(f"unknown setup: {name}")
