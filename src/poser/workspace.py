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


def fill_path_options(args: argparse.Namespace) -> None:
    """Set each path option the command has but was not given from the workspace.

    The search runs only when an option was omitted, so a command given every path needs no
    workspace. Raises WorkspaceError when one is needed and none is found.
    """
    omitted = [dest for dest in PATH_OPTIONS if dest in vars(args) and getattr(args, dest) is None]
    if not omitted:
        return
    workspace = find_workspace()
    for dest in omitted:
        setattr(args, dest, getattr(workspace, PATH_OPTIONS[dest]))
