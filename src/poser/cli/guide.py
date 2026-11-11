"""`update-guide`: write a studio's `POSER.md` from poser and its `poser.toml`."""

import argparse
import os
import stat
import tempfile
from importlib.resources import files
from pathlib import Path

from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    fail,
    load_composition,
)
from poser.cli.workspace import WorkspaceError, fill_path_options
from poser.compose import Composition
from poser.core.guide import GENERATED, compose_guide
from poser.core.ports import ConfigError

GUIDE_NAME = "POSER.md"
PROG = "update-guide"


def core_guide() -> str:
    """Return the guide text true in every workspace."""
    return (files("poser") / "scaffold" / "guide.md").read_text(encoding="utf-8")


def workspace_guide(composition: Composition) -> str:
    """Return the composed guide: the core guide, the instrument notes, then adapters' guides."""
    return compose_guide(core_guide(), composition.instrument_notes(), composition.guide_parts())


def guide_path(config: Path) -> Path:
    """Where the guide of the workspace a config belongs to goes: beside the config."""
    return config.parent / GUIDE_NAME


def written_by_update_guide(path: Path) -> bool:
    """Whether the existing file at `path` starts with the line every composed guide starts with.

    Raises OSError when the file cannot be read.
    """
    with path.open("rb") as file:
        first = file.readline().rstrip(b"\r\n")
    return first == GENERATED.encode()


def foreign_guide_problem(path: Path) -> str:
    return f"{path} was not written by update-guide; move it away to replace it"


def write_guide(path: Path, text: str) -> None:
    """Replace the file at `path` with `text` at once, so a reader never sees half a guide.

    A symlinked `path` keeps its link and the target is replaced. The file keeps an existing
    target's permissions, or gets the usual umask-based ones when new.
    """
    target = path.resolve()
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text)
        Path(temporary).chmod(mode)
        Path(temporary).replace(target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    parser.add_argument("--config", type=Path, help="workspace config (default: poser.toml)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 when the guide is out of date",
    )
    args = parser.parse_args(argv)

    try:
        fill_path_options(args)
        text = workspace_guide(load_composition(args))
    except (WorkspaceError, ConfigError) as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG

    path = guide_path(Path(args.config))
    try:
        if path.exists() and not written_by_update_guide(path):
            fail(PROG, foreign_guide_problem(path))
            return EXIT_SOME_FAILED if args.check else EXIT_BAD_CONFIG
        current = path.read_text(encoding="utf-8") if args.check and path.exists() else None
    except (OSError, UnicodeDecodeError) as error:
        fail(PROG, f"cannot read {path}: {error}")
        return EXIT_SOME_FAILED
    if args.check:
        if current != text:
            fail(PROG, f"{GUIDE_NAME} is out of date; run uv run update-guide")
            return EXIT_SOME_FAILED
        return EXIT_ALL_PASS

    try:
        write_guide(path, text)
    except OSError as error:
        fail(PROG, f"cannot write {path}: {error}")
        return EXIT_SOME_FAILED
    print(path)
    return EXIT_ALL_PASS
