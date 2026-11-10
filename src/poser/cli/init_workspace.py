"""`init-workspace`: write the files a new workspace starts with."""

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path

import yaml

from poser.cli.guide import GUIDE_NAME, guide_path, workspace_guide, write_guide
from poser.cli.workspace import CONFIG_NAME, Workspace
from poser.compose import load
from poser.core.ports import ConfigError

EXIT_OK = 0
EXIT_CONFLICT = 2

# Each scaffold resource, by its name in `poser/scaffold/`, and where it goes in a workspace.
SCAFFOLD_FILES = {
    "claude.md": "CLAUDE.md",
    CONFIG_NAME: CONFIG_NAME,
    "demo.yaml": "songs/demo.yaml",
}
# Composed from poser's guide and the new poser.toml, as `update-guide` does.
WRITTEN_FILES = (GUIDE_NAME, *SCAFFOLD_FILES.values())
GITIGNORE_RESOURCE = "gitignore"  # Merged into .gitignore, never refused.
ONBOARDING_RESOURCE = "onboarding.txt"  # Printed when no instrument is detected, never written.
# The Serum plugin candidate; its presets are SERUM_PRESET_PATH, written only as a directory.
SCAFFOLD_PLUGIN_PATH = Path("/Library/Audio/Plug-Ins/VST3/Serum.vst3")
SERUM_PRESET_PATH = Path("/Library/Audio/Presets/Xfer Records/Serum Presets/Presets")
# Where Surge XT's installer puts the plugin and the factory patches: the documented default
# (all users) first, then the per-user layout. Creation fills the tables from the candidates
# that exist; the scaffold's commented tables carry the same defaults, and a test pins them.
SURGE_PLUGIN_PATHS = (
    Path("/Library/Audio/Plug-Ins/VST3/Surge XT.vst3"),
    Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST3" / "Surge XT.vst3",
)
SURGE_PATCHES_PATHS = (
    Path("/Library/Application Support/Surge XT/patches_factory"),
    Path.home() / "Library" / "Application Support" / "Surge XT" / "patches_factory",
)


def scaffold_bytes(name: str) -> bytes:
    """Return the bytes of a scaffold resource."""
    return (files("poser") / "scaffold" / name).read_bytes()


def split_scaffold_config(scaffold: str) -> tuple[str, str, str]:
    """Split the scaffold config into its head, its Serum tables, and the Surge XT notes."""
    head, separator, rest = scaffold.partition("[instruments.serum]")
    serum_tables, marker, surge_notes = rest.partition("# No Serum on this machine?")
    return head, separator + serum_tables, marker + surge_notes


def surge_tables(plugin: Path, patches: Path | None) -> str:
    """The active Surge XT tables creation writes, with TOML-encoded absolute paths."""
    lines = ["[instruments.surge]", 'adapter = "pedalboard"', f"path = {json.dumps(str(plugin))}"]
    if patches is not None:
        lines += ["", "[instruments.surge.sources]", f"presets = {json.dumps(str(patches))}"]
    return "\n".join(lines) + "\n"


def without_serum_presets(serum_tables: str) -> str:
    """Drop the Serum preset sources table, for machines without the folder."""
    head, _separator, _rest = serum_tables.partition("[instruments.serum.sources]")
    return head


def surge_demo() -> str:
    """The scaffold demo with every track's sound pointed at Surge XT."""
    demo = yaml.safe_load(scaffold_bytes("demo.yaml"))
    for track in demo["tracks"]:
        track["sound"] = "surge"
    return yaml.safe_dump(demo, sort_keys=False, allow_unicode=True)


def merge_gitignore(path: Path) -> None:
    """Append each scaffold ignore line the file lacks, creating the file if it is missing."""
    existing = path.read_text() if path.exists() else ""
    present = set(existing.splitlines())
    missing = [
        line
        for line in scaffold_bytes(GITIGNORE_RESOURCE).decode().splitlines()
        if line not in present
    ]
    if not missing:
        return
    if existing and not existing.endswith("\n"):
        existing += "\n"
    path.write_text(existing + "".join(f"{line}\n" for line in missing))


def conflicts(directory: Path) -> list[str]:
    """Describe each existing path that stops the scaffold from being written whole."""
    targets = [directory / target for target in WRITTEN_FILES]
    folders = sorted({directory, *(target.parent for target in targets)})
    problems = [f"{folder} is not a directory" for folder in folders if folder.is_file()]
    problems += [f"{target} exists" for target in targets if target.exists()]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="init-workspace",
        description="Write a composer guide, an example workspace config, and a demo song into "
        "a directory, making it a workspace.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path(),
        help="workspace directory, created if missing (default: the working directory)",
    )
    args = parser.parse_args(argv)
    directory = Path(args.directory)

    problems = conflicts(directory)
    if problems:
        for problem in problems:
            print(f"init-workspace: {problem}", file=sys.stderr)
        return EXIT_CONFLICT

    serum = SCAFFOLD_PLUGIN_PATH.exists()
    serum_presets = SERUM_PRESET_PATH if SERUM_PRESET_PATH.is_dir() else None
    surge_plugin = next((path for path in SURGE_PLUGIN_PATHS if path.exists()), None)
    surge_patches = next((path for path in SURGE_PATCHES_PATHS if path.is_dir()), None)

    scaffold = scaffold_bytes(CONFIG_NAME).decode()
    head, serum_tables, surge_notes = split_scaffold_config(scaffold)
    reports: list[str] = []

    if not serum and surge_plugin is None:
        poser_text = scaffold
        demo_text = scaffold_bytes("demo.yaml").decode()
    else:
        serum_part = ""
        if serum:
            serum_part = serum_tables if serum_presets else without_serum_presets(serum_tables)
            if serum_presets is None:
                reports.append("Wrote poser.toml from detected serum (preset source omitted)")
        if surge_plugin is None:
            poser_text = head + serum_part + surge_notes
            demo_text = scaffold_bytes("demo.yaml").decode()
        else:
            poser_text = head + serum_part + surge_tables(surge_plugin, surge_patches)
            suffix = "" if surge_patches else " (preset source omitted)"
            reports.append(f"Wrote poser.toml from detected surge{suffix}")
            if serum:
                demo_text = scaffold_bytes("demo.yaml").decode()
            else:
                demo_text = surge_demo()
                reports.append("Wrote songs/demo.yaml from detected surge")

    contents = {
        "claude.md": scaffold_bytes("claude.md"),
        CONFIG_NAME: poser_text.encode(),
        "demo.yaml": demo_text.encode(),
    }
    for name, target in SCAFFOLD_FILES.items():
        path = directory / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents[name])
    merge_gitignore(directory / ".gitignore")
    workspace = Workspace(directory)
    try:
        composition = load(workspace.config, workspace.sounds_dir, workspace.renders_dir)
        write_guide(guide_path(workspace.config), workspace_guide(composition))
    except ConfigError as error:
        print(f"init-workspace: {error}", file=sys.stderr)
        return EXIT_CONFLICT

    for target in WRITTEN_FILES:
        print(target)
    for line in reports:
        print(line)
    print(f"Edit {CONFIG_NAME} to match this machine's plugins, then run: uv run check-instruments")
    print(
        "songs/demo.yaml names the scaffold's instrument; if you changed poser.toml, "
        "point its sound names at yours (uv run list-sounds)"
    )
    if not serum and surge_plugin is None:
        print(scaffold_bytes(ONBOARDING_RESOURCE).decode(), end="")
    return EXIT_OK
