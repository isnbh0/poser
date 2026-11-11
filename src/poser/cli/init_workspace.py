"""`init-workspace`: initialize a workspace from an explicit reference setup."""

import argparse
import sys
from importlib.resources import files
from pathlib import Path
from typing import Never, override

from poser.cli.guide import GUIDE_NAME, workspace_guide
from poser.cli.workspace import CONFIG_NAME, Workspace
from poser.compose import Composition, parse_config
from poser.core.ports import AdapterContext, ConfigError
from poser.reference_setups.dispatch import SETUPS, prepare

REPO_README = "https://github.com/isnbh0/poser/blob/main/README.md"
SETUP_README = (
    "https://github.com/isnbh0/poser/blob/main/src/poser/reference_setups/surge/README.md"
)


def _scaffold(name: str) -> str:
    return (files("poser") / "scaffold" / name).read_text(encoding="utf-8")


def _preflight(directory: Path, targets: tuple[str, ...]) -> None:
    """Reject existing owned files and incompatible parents, including dangling links."""
    paths = [directory / target for target in targets]
    parents = {parent for path in paths for parent in path.parents}
    for parent in sorted(parents):
        if parent.is_symlink() and not parent.exists():
            raise ConfigError(f"{parent} is a dangling symlink")
        if parent.exists() and not parent.is_dir():
            raise ConfigError(f"{parent} is not a directory")
    for path in paths:
        if path.name == ".gitignore":
            if path.is_symlink():
                raise ConfigError(f"{path} is a symlink; refusing to merge")
            if path.exists() and not path.is_file():
                raise ConfigError(f"{path} is not a regular file")
            if path.exists() and path.stat().st_nlink > 1:
                raise ConfigError(f"{path} has multiple hard links; refusing to merge")
        elif path.exists() or path.is_symlink():
            raise ConfigError(f"{path} exists")


def _merged_ignore(path: Path) -> str:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    present = set(existing.splitlines())
    missing = [line for line in _scaffold("gitignore").splitlines() if line not in present]
    if missing and existing and not existing.endswith("\n"):
        existing += "\n"
    return existing + "".join(f"{line}\n" for line in missing)


class _Parser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        super().error(f"{message}\nExample: uv run init-workspace --setup surge")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(
        prog="init-workspace",
        description="Initialize a workspace using a reference setup. "
        f"See {REPO_README} and {SETUP_README}.",
        epilog="Example: uv run init-workspace --setup surge",
    )
    parser.add_argument("directory", nargs="?", type=Path, default=Path())
    parser.add_argument("--setup", required=True, choices=SETUPS, help="reference setup (surge)")
    parser.add_argument("--plugin-path", type=Path, help="override the VST3 bundle path")
    parser.add_argument("--presets-path", type=Path, help="override the factory presets directory")
    args = parser.parse_args(argv)
    directory = args.directory.absolute()
    workspace = Workspace(directory)
    try:
        setup = prepare(args.setup, args.plugin_path, args.presets_path)
        targets = (GUIDE_NAME, "CLAUDE.md", CONFIG_NAME, "songs/demo.yaml", ".gitignore")
        _preflight(directory, targets)
        config = parse_config(setup.config, workspace.config)
        context = AdapterContext(directory, workspace.sounds_dir, workspace.renders_dir)
        guide = workspace_guide(Composition(config, context))
        outputs = {
            GUIDE_NAME: guide,
            "CLAUDE.md": _scaffold("claude.md"),
            CONFIG_NAME: setup.config,
            "songs/demo.yaml": setup.demo,
            ".gitignore": _merged_ignore(directory / ".gitignore"),
        }
    except (ConfigError, OSError, UnicodeError) as error:
        print(f"init-workspace: {error}", file=sys.stderr)
        return 2

    try:
        for target, text in outputs.items():
            path = directory / target
            path.parent.mkdir(parents=True, exist_ok=True)
            if target == ".gitignore" and path.exists():
                path.write_text(text, encoding="utf-8")
            else:
                with path.open("x", encoding="utf-8") as output:
                    output.write(text)
            print(target)
    except OSError as error:
        print(f"init-workspace: cannot write workspace: {error}", file=sys.stderr)
        return 2
    print(f"Surge plugin: {setup.plugin}")
    print(
        f"Surge presets: {setup.presets}" if setup.presets else "Surge presets: not found; omitted"
    )
    print("Run: uv run check-instruments")
    return 0
