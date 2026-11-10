"""`init-workspace`: write the files a new workspace starts with."""

import argparse
import sys
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
ONBOARDING_RESOURCE = "onboarding.txt"  # Printed after the closing instruction, never written.
# The scaffold poser.toml's instrument path; when it is missing, the free-synth pointer prints.
SCAFFOLD_PLUGIN_PATH = Path("/Library/Audio/Plug-Ins/VST3/Serum.vst3")


def scaffold_bytes(name: str) -> bytes:
    """Return the bytes of a scaffold resource."""
    return (files("poser") / "scaffold" / name).read_bytes()


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

    for name, target in SCAFFOLD_FILES.items():
        path = directory / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(scaffold_bytes(name))
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
    print(f"Edit {CONFIG_NAME} to match this machine's plugins, then run: uv run check-instruments")
    print(
        "songs/demo.yaml names the scaffold's instrument; if you changed poser.toml, "
        "point its sound names at yours (uv run list-sounds)"
    )
    if not SCAFFOLD_PLUGIN_PATH.exists():
        print(scaffold_bytes(ONBOARDING_RESOURCE).decode(), end="")
    return EXIT_OK
