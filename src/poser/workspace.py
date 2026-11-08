"""Workspaces: the directory holding a plugin list, saved sounds, and renders.

A workspace is the nearest directory at or above the working directory that holds a file named
`plugins.toml`, found the way uv and ruff find `pyproject.toml`. Only command entry points look
for one. Library functions take every directory as an argument.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "plugins.toml"

# Each command path option, by its argparse destination, and the workspace property it defaults to.
PATH_OPTIONS = {"config": "config", "sounds_dir": "sounds_dir", "output_dir": "renders_dir"}


class WorkspaceError(Exception):
    """No workspace was found."""


class SongOutsideWorkspaceError(WorkspaceError):
    """A song's path is not below the directory render directories are relative to."""


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

    The search runs only when an option was omitted, so a command given every path needs no
    workspace. Returns the base that song render directories are relative to: the workspace root
    when it searched, and the working directory otherwise. Raises WorkspaceError when a workspace
    is needed and none is found.
    """
    omitted = [dest for dest in PATH_OPTIONS if dest in vars(args) and getattr(args, dest) is None]
    if not omitted:
        return Path.cwd()
    workspace = find_workspace()
    for dest in omitted:
        setattr(args, dest, getattr(workspace, PATH_OPTIONS[dest]))
    return workspace.root


def song_render_dir(renders_dir: Path, base: Path, song_path: Path) -> Path:
    """Return a song's render directory: its path below `base`, under `renders_dir`.

    The song's suffix is kept, so `songs/x.py` renders into `renders/songs/x.py/`. No entry poser
    writes inside a render directory ends in a song suffix, so one song's directory never
    coincides with an entry of another's. The song path is resolved, so a symlinked song is placed
    by its target. Raises SongOutsideWorkspaceError when the song is not below `base`.
    """
    base, song = base.resolve(), song_path.resolve()
    if not song.is_relative_to(base):
        raise SongOutsideWorkspaceError(f"{song} is outside the workspace at {base}")
    return renders_dir / song.relative_to(base)
