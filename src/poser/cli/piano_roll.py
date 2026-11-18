"""Draw authored notes as a PNG without loading plugins or rendering audio."""

import argparse
import stat
from collections.abc import Callable
from pathlib import Path

from poser.cli.common import EXIT_ALL_PASS, EXIT_BAD_CONFIG, command, fail, load_composition
from poser.cli.workspace import fill_path_options
from poser.compose import file_writer
from poser.core.loading import load_song
from poser.core.piano_roll import project_piano_roll
from poser.core.ports import FileWriter

PROG = "piano-roll"


def _publish(
    writer: FileWriter, song: Path, output: Path, draw: Callable[[Path], None], *, replace: bool
) -> Path:
    """Validate before loading, stage, and commit exactly one directory entry through `writer`.

    Checks are not a security boundary against hostile filesystem mutation. Parent mutation is
    outside this contract; replacement is not compare-and-swap. A new PNG gets the umask-based
    permission bits, and a replaced one keeps its own.
    """
    destination = output.absolute()  # Never dereference the final directory entry.

    def validate() -> None:
        if destination.suffix.lower() != ".png":
            raise ValueError(f"{destination}: output must have a .png suffix")
        if (
            destination == song.absolute()
            or destination.parent.resolve() / destination.name == song.resolve()
        ):
            raise ValueError(f"{destination}: output aliases the input song")
        try:
            entry = destination.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(entry.st_mode):
            raise ValueError(
                f"{destination}: output must be a regular file, not a link or directory"
            )
        if destination.samefile(song):
            raise ValueError(f"{destination}: output aliases the input song")
        if not replace:
            raise FileExistsError(f"{destination}: already exists; use --replace")

    def fill(staging: Path) -> None:
        draw(staging)
        validate()

    def leftover(staging: Path, error: OSError) -> None:
        fail(PROG, f"warning: cannot clean up temporary file {staging}: {error}")

    validate()
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer.write(destination, fill, replace=replace, suffix=".png", leftover=leftover)
    return destination


@command
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    parser.add_argument("song", type=Path, help="trusted song file (Python build() is executed)")
    parser.add_argument("--config", type=Path, help="workspace config (default: poser.toml)")
    parser.add_argument("--output", type=Path, required=True, help="exact destination PNG path")
    parser.add_argument("--replace", action="store_true", help="atomically replace a regular file")
    args = parser.parse_args(argv)
    try:
        fill_path_options(args)
        composition = load_composition(args)
        writer = file_writer()

        def draw(path: Path) -> None:
            loaded = load_song(args.song, composition.song_formats())
            composition.piano_roll_drawer().draw(project_piano_roll(loaded.graph), path)

        destination = _publish(writer, args.song, args.output, draw, replace=args.replace)
    except Exception as error:  # noqa: BLE001 — command boundary includes third-party drawing errors.
        fail(PROG, error)
        return EXIT_BAD_CONFIG
    print(destination)
    return EXIT_ALL_PASS
