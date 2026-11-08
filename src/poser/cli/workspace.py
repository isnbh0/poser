"""Workspaces: the directory holding a workspace config, saved sounds, and renders.

A workspace is the directory holding the `poser.toml` a command was given with `--config`, or
else the nearest directory at or above the working directory that holds a file named
`poser.toml`, found the way uv and ruff find `pyproject.toml`. Only command entry points look
for one. Library functions take every directory as an argument.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

from poser.core.names import SongId

CONFIG_NAME = "poser.toml"

# Each command path option, by its argparse destination, and the workspace property it defaults to.
PATH_OPTIONS = {"config": "config", "sounds_dir": "sounds_dir", "output_dir": "renders_dir"}


class WorkspaceError(Exception):
    """No workspace was found."""


class SongOutsideWorkspaceError(WorkspaceError):
    """A song's path is not below the directory song ids are relative to."""


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / CONFIG_NAME

    @property
    def sounds_dir(self) -> Path:
        return self.root / "sounds"

    @property
    def renders_dir(self) -> Path:
        return self.root / "renders"


def find_workspace(start: Path | None = None) -> Workspace:
    """Return the workspace at or above `start`, the working directory by default."""
    start = (Path.cwd() if start is None else start).resolve()
    for directory in (start, *start.parents):
        if (directory / CONFIG_NAME).is_file():
            return Workspace(directory)
    raise WorkspaceError(
        f"no {CONFIG_NAME} in {start} or any parent; "
        "run `uv run init-workspace` to create a workspace"
    )


def fill_path_options(args: argparse.Namespace) -> Path:
    """Set each path option the command has but was not given from the workspace.

    The workspace is the directory holding `--config` when it was given, and is searched for
    from the working directory otherwise. Returns the base that song ids are relative to: the
    workspace root when an option was filled from it, and the working directory when every
    option was given. Raises WorkspaceError when a search finds no workspace.
    """
    omitted = [dest for dest in PATH_OPTIONS if dest in vars(args) and getattr(args, dest) is None]
    if not omitted:
        return Path.cwd()
    config = getattr(args, "config", None)
    workspace = find_workspace() if config is None else Workspace(Path(config).absolute().parent)
    for dest in omitted:
        setattr(args, dest, getattr(workspace, PATH_OPTIONS[dest]))
    return workspace.root


def song_id(base: Path, song_path: Path) -> SongId:
    """Return a song's id: its resolved path below `base`, such as `songs/demo.yaml`.

    The song path is resolved, so a symlinked song is placed by its target. Raises
    SongOutsideWorkspaceError when the song is not below `base`.
    """
    base, song = base.resolve(), song_path.resolve()
    outside = SongOutsideWorkspaceError(f"{song} is outside the workspace at {base}")
    if not song.is_relative_to(base):
        raise outside
    try:
        return SongId(song.relative_to(base).as_posix())
    except ValueError as error:
        raise outside from error
