"""`init-workspace`: write the files a new workspace starts with."""

import argparse
import json
import sys
import textwrap
from importlib.resources import files
from pathlib import Path

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
# The instrument the scaffold's demo song names, which creation rewrites for a Serum machine.
SCAFFOLD_INSTRUMENT = "surge"
# The Serum plugin candidate; its presets are SERUM_PRESET_PATH, written only as a directory.
SERUM_PLUGIN_PATH = Path("/Library/Audio/Plug-Ins/VST3/Serum.vst3")
SERUM_PRESET_PATH = Path("/Library/Audio/Presets/Xfer Records/Serum Presets/Presets")
# Where Surge XT's installer puts the plugin and the factory patches: the documented default
# (all users) first, then the per-user layout. Creation fills the tables from the candidates
# that exist; the scaffold's commented Serum tables carry the same defaults, and a test pins
# them.
SURGE_PLUGIN_PATHS = (
    Path("/Library/Audio/Plug-Ins/VST3/Surge XT.vst3"),
    Path.home() / "Library" / "Audio" / "Plug-Ins" / "VST3" / "Surge XT.vst3",
)
SURGE_PATCHES_PATHS = (
    Path("/Library/Application Support/Surge XT/patches_factory"),
    Path.home() / "Library" / "Application Support" / "Surge XT" / "patches_factory",
)

# Each guide the workspace guide holds for a detected instrument, and the scaffold's Surge XT
# guide, which carries the install steps until one is detected and replaces it.
SURGE_DETECTED_GUIDE = (
    "Surge XT's factory patches are .fxp files under presets/ by category, such as "
    "`surge:presets/Basses/Attacky`."
)
SCAFFOLD_SURGE_GUIDE = (
    "No synthesizer was detected when this workspace was created, so songs cannot render yet. "
    "Install Surge XT (https://surge-synthesizer.github.io/downloads), then run "
    "`uv run check-instruments`, and replace this guide text with a note about the instrument. "
    "Its factory patches, such as `surge:presets/Basses/Attacky`, are .fxp files under "
    "presets/ by category."
)
SERUM_GUIDE = (
    "Serum's default patch neither repeats between renders nor responds to controllers. "
    "Controllers do nothing in Serum here except CC 1, the mod wheel, in presets that route "
    "it. CC 1 on `serum:presets/Leads/LD A Bit of Luck [SD]` is confirmed. On another preset, "
    "compare history records with the lane low and high to see whether it routes CC 1."
)

# The scaffold line that begins its commented Serum tables.
SERUM_NOTES_INTRO = "# Serum is a paid synthesizer."


def scaffold_bytes(name: str) -> bytes:
    """Return the bytes of a scaffold resource."""
    return (files("poser") / "scaffold" / name).read_bytes()


def split_scaffold_config(scaffold: str) -> tuple[str, str, str]:
    """Split the scaffold config into its header, its Surge XT table, and the Serum notes."""
    header, marker, rest = scaffold.partition("[instruments.surge]")
    table_end = rest.index(f"\n{SERUM_NOTES_INTRO}")
    return header, marker + rest[: table_end + 1], rest[table_end + 1 :]


def guide_lines(text: str) -> list[str]:
    # The `guide` table value is a TOML multi-line basic string, so this cannot be a docstring.
    # Return the `guide = """…"""` table lines holding `text`, wrapped, with continuations.
    lines = textwrap.wrap(text, width=96, break_long_words=False, break_on_hyphens=False)
    return ['guide = """', *[f"{line} \\" for line in lines[:-1]], f'{lines[-1]}"""']


def surge_tables(plugin: Path, patches: Path | None, guide: str) -> str:
    """The active Surge XT tables creation writes, with TOML-encoded absolute paths."""
    lines = [
        "[instruments.surge]",
        'adapter = "pedalboard"',
        f"path = {json.dumps(str(plugin))}",
        *guide_lines(guide),
    ]
    if patches is not None:
        lines += ["", "[instruments.surge.sources]", f"presets = {json.dumps(str(patches))}"]
    return "\n".join(lines) + "\n"


def serum_tables(plugin: Path, presets: Path | None) -> str:
    """The active Serum tables creation writes, with TOML-encoded absolute paths."""
    lines = [
        "[instruments.serum]",
        'adapter = "pedalboard"',
        f"path = {json.dumps(str(plugin))}",
        *guide_lines(SERUM_GUIDE),
    ]
    if presets is not None:
        lines += ["", "[instruments.serum.sources]", f"presets = {json.dumps(str(presets))}"]
    return "\n".join(lines) + "\n"


def commented_surge(surge_block: str) -> str:
    """The scaffold's Surge XT table, commented out, for machines without it."""
    lines = (f"# {line}" if line else line for line in surge_block.splitlines())
    return "\n".join(lines) + "\n"


def demo_with_instrument(name: str) -> str:
    """The scaffold demo with every track's sound pointed at one instrument."""
    demo = scaffold_bytes("demo.yaml").decode()
    return demo.replace(f"sound: {SCAFFOLD_INSTRUMENT}", f"sound: {name}")


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

    serum = SERUM_PLUGIN_PATH.exists()
    serum_presets = SERUM_PRESET_PATH if SERUM_PRESET_PATH.is_dir() else None
    surge_plugin = next((path for path in SURGE_PLUGIN_PATHS if path.exists()), None)
    surge_patches = next((path for path in SURGE_PATCHES_PATHS if path.is_dir()), None)

    scaffold = scaffold_bytes(CONFIG_NAME).decode()
    header, surge_block, serum_notes = split_scaffold_config(scaffold)
    reports: list[str] = []
    demo_text = scaffold_bytes("demo.yaml").decode()

    if surge_plugin is not None:
        poser_text = (
            f"{header}{surge_tables(surge_plugin, surge_patches, SURGE_DETECTED_GUIDE)}"
            f"\n{serum_notes}"
        )
        suffix = "" if surge_patches else " (preset source omitted)"
        reports.append(f"Wrote poser.toml from detected surge{suffix}")
    elif serum:
        serum_text = serum_tables(SERUM_PLUGIN_PATH, serum_presets)
        poser_text = f"{header}{commented_surge(surge_block)}{serum_text}"
        suffix = "" if serum_presets else " (preset source omitted)"
        reports.append(f"Wrote poser.toml from detected serum{suffix}")
        demo_text = demo_with_instrument("serum")
        reports.append("Wrote songs/demo.yaml from detected serum")
    else:
        documented = surge_tables(SURGE_PLUGIN_PATHS[0], surge_patches, SCAFFOLD_SURGE_GUIDE)
        poser_text = f"{header}{documented}\n{serum_notes}"

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
    if surge_plugin is None and not serum:
        print(scaffold_bytes(ONBOARDING_RESOURCE).decode(), end="")
    else:
        print("Run: uv run check-instruments")
    return EXIT_OK
