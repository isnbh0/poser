"""`init-workspace`: initialize a workspace from an explicit reference setup."""

import argparse
import os
import stat
import sys
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Never, override

from poser.cli.guide import GUIDE_NAME, workspace_guide
from poser.cli.workspace import CONFIG_NAME, Workspace
from poser.compose import Composition, parse_config, workspace_initializer
from poser.core.ports import AdapterContext, ConfigError
from poser.reference_setups.dispatch import SETUPS, prepare

SETUP_GUIDE = "https://github.com/isnbh0/poser/blob/main/docs/setup.md"
SETUP_README = (
    "https://github.com/isnbh0/poser/blob/main/src/poser/reference_setups/surge/README.md"
)


def _scaffold(name: str) -> str:
    return (files("poser") / "scaffold" / name).read_text(encoding="utf-8")


def _leftover(path: Path, data: bytes) -> bool:
    """Whether `path` holds exactly the bytes this run would write, as an interrupted run left.

    Besides itself, it may be linked only from `.poser-tmp-*` files beside it: a run killed
    between linking a target and unlinking its temp file leaves both.
    """
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return False
    twins = sum(
        1
        for temp in path.parent.glob(".poser-tmp-*")
        if temp != path and os.path.samestat(temp.lstat(), info)
    )
    return info.st_nlink == 1 + twins and path.read_bytes() == data


def _preflight(directory: Path, planned: Mapping[str, bytes]) -> tuple[str, ...]:
    """Reject incompatible parents and owned files; return the targets an interrupted run left.

    `planned` holds every output but `.gitignore`, which merging makes idempotent.
    """
    targets = (*planned, ".gitignore")
    paths = [directory / target for target in targets]
    parents = {parent for path in paths for parent in path.parents}
    for parent in sorted(parents):
        if parent.is_symlink() and not parent.exists():
            raise ConfigError(f"{parent} is a dangling symlink")
        if parent.exists() and not parent.is_dir():
            raise ConfigError(f"{parent} is not a directory")
    leftovers: list[str] = []
    for target, path in zip(targets, paths, strict=True):
        if target == ".gitignore":
            if path.is_symlink():
                raise ConfigError(f"{path} is a symlink; refusing to merge")
            if path.exists() and not path.is_file():
                raise ConfigError(f"{path} is not a regular file")
            if path.exists() and path.stat().st_nlink > 1:
                raise ConfigError(f"{path} has multiple hard links; refusing to merge")
        elif path.exists() or path.is_symlink():
            if not _leftover(path, planned[target]):
                raise ConfigError(f"{path} exists")
            leftovers.append(target)
    return tuple(leftovers)


def _refuse_leftovers(directory: Path, leftovers: tuple[str, ...]) -> Never:
    named = ", ".join(str(directory / target) for target in leftovers)
    raise ConfigError(
        f"{named} were left by an interrupted initialization; "
        "rerun with --remove-leftovers to delete them"
    )


def _merged_ignore(original: bytes | None) -> str | None:
    exists = original is not None
    existing = original.decode("utf-8") if original is not None else ""
    present = set(existing.splitlines())
    missing = [line for line in _scaffold("gitignore").splitlines() if line not in present]
    if exists and not missing:
        return None
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
        f"See {SETUP_GUIDE} and {SETUP_README}.",
        epilog="Example: uv run init-workspace --setup surge",
    )
    parser.add_argument("directory", nargs="?", type=Path, default=Path())
    parser.add_argument("--setup", required=True, choices=SETUPS, help="reference setup (surge)")
    parser.add_argument("--plugin-path", type=Path, help="override the VST3 bundle path")
    parser.add_argument("--presets-path", type=Path, help="override the factory presets directory")
    parser.add_argument("--advice", choices=("on", "off"), default="on")
    parser.add_argument(
        "--remove-leftovers",
        action="store_true",
        help="delete files left by an interrupted init-workspace, then initialize",
    )
    args = parser.parse_args(argv)
    directory = args.directory.absolute()
    workspace = Workspace(directory)
    try:
        initializer = workspace_initializer(directory)
        setup = prepare(args.setup, args.plugin_path, args.presets_path)
        config_text = (
            setup.config + "\n[guidance]\nadvice = " + str(args.advice == "on").lower() + "\n"
        )
        config = parse_config(config_text, workspace.config)
        context = AdapterContext(directory, workspace.sounds_dir, workspace.renders_dir)
        guide = workspace_guide(Composition(config, context))
        outputs = {
            "AGENTS.md": _scaffold("agents.md"),
            GUIDE_NAME: guide,
            "CLAUDE.md": _scaffold("claude.md"),
            CONFIG_NAME: config_text,
            "songs/demo.yaml": setup.demo,
        }
        planned = {name: text.encode("utf-8") for name, text in outputs.items()}
        leftovers = _preflight(directory, planned)
        if leftovers and not args.remove_leftovers:
            _refuse_leftovers(directory, leftovers)
        removed = initializer.remove_leftovers(planned) if args.remove_leftovers else ()
        ignore_path = directory / ".gitignore"
        original_ignore = ignore_path.read_bytes() if ignore_path.exists() else None
        ignore = _merged_ignore(original_ignore)
        if ignore is not None:
            outputs[".gitignore"] = ignore
            planned[".gitignore"] = ignore.encode("utf-8")
        initializer.publish(planned, expected_ignore=original_ignore)
    except (ConfigError, OSError, UnicodeError) as error:
        print(f"init-workspace: {error}", file=sys.stderr)
        return 2

    for target in removed:
        print(f"removed {target}")
    for target in outputs:
        print(target)
    print(f"Surge plugin: {setup.plugin}")
    print(
        f"Surge presets: {setup.presets}" if setup.presets else "Surge presets: not found; omitted"
    )
    print("Run: uv run check-instruments")
    return 0
