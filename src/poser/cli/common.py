"""Exit codes, table layout, timeouts, and loading shared by poser's commands."""

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from poser.cli.workspace import Workspace
from poser.compose import Composition, load

EXIT_ALL_PASS = 0
EXIT_SOME_FAILED = 1
EXIT_BAD_CONFIG = 2
# Another render-song or analyze-song holds the song's lock.
EXIT_SONG_BUSY = 3

# How long one worker may run a plugin before it counts as timed out.
RENDER_TIMEOUT_S = 120.0


def align_rows(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]


def fail(prog: str, error: object) -> None:
    """Print an error the way every command does: `{prog}: {error}` on stderr."""
    print(f"{prog}: {error}", file=sys.stderr)


def progress(prog: str) -> Callable[[str], None]:
    """Return a printer of progress lines, `{prog}: {line}` on stderr, flushed as they come."""

    def emit(line: str) -> None:
        print(f"{prog}: {line}", file=sys.stderr, flush=True)

    return emit


def load_composition(args: argparse.Namespace) -> Composition:
    """Load the composition for a command whose path options are filled.

    Factories see the directories the command uses, or `<root>/sounds` and `<root>/renders`, where
    `<root>` holds the config, for a command without `--sounds-dir` or `--output-dir`.
    """
    config = Path(args.config)
    workspace = Workspace(config.absolute().parent)
    sounds_dir = getattr(args, "sounds_dir", None) or workspace.sounds_dir
    renders_dir = getattr(args, "output_dir", None) or workspace.renders_dir
    return load(config, Path(sounds_dir), Path(renders_dir))
