"""`init-workspace`: write the files a new workspace starts with."""

import argparse
import sys
from importlib.resources import files
from pathlib import Path

EXIT_OK = 0
EXIT_CONFLICT = 2

# Each scaffold resource, by its name in `poser/scaffold/`, and where it goes in a workspace.
SCAFFOLD_FILES = {
    "guide.md": "AGENTS.md",
    "claude.md": "CLAUDE.md",
    "plugins.toml": "plugins.toml",
    "demo.yaml": "songs/demo.yaml",
}
GITIGNORE_RESOURCE = "gitignore"  # Merged into .gitignore, never refused.


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
    targets = [directory / target for target in SCAFFOLD_FILES.values()]
    folders = sorted({directory, *(target.parent for target in targets)})
    problems = [f"{folder} is not a directory" for folder in folders if folder.is_file()]
    problems += [f"{target} exists" for target in targets if target.exists()]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="init-workspace",
        description="Write a composer guide, an example plugin list, and a demo song into a "
        "directory, making it a workspace.",
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

    for target in SCAFFOLD_FILES.values():
        print(target)
    print("Edit plugins.toml to match this machine's plugins, then run: uv run check-plugins")
    return EXIT_OK
