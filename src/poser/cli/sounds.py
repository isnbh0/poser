"""`list-sounds` and `save-sound`: the sounds a song can name, and saving new ones.

A sound is `<instrument>` or `<instrument>:<name>`, with each instrument's own names.
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    align_rows,
    fail,
    load_composition,
)
from poser.cli.workspace import WorkspaceError, fill_path_options
from poser.core.audio import SILENCE_DBFS
from poser.core.capturing import NotSaved, PreviewFailed, Refused, SaveRequest, save_sound
from poser.core.names import Identifier
from poser.core.ports import ConfigError
from poser.core.sounds import SoundEntry, parse_sound_ref

LIST_PROG = "list-sounds"
SAVE_PROG = "save-sound"
OLD_SOUND_SUFFIX = ".json"


def old_layout_problems(sounds_dir: Path) -> list[str]:
    """Describe each saved sound left directly in the sounds directory by an older poser."""
    if not sounds_dir.is_dir():
        return []
    old = sorted(
        path for path in sounds_dir.iterdir() if path.suffix == OLD_SOUND_SUFFIX and path.is_file()
    )
    return [
        f"{path}: saved sounds now live in sounds/<instrument>/; move it to "
        'sounds/<instrument>/<name>.json and delete its "plugin" key'
        for path in old
    ]


def list_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=LIST_PROG, description="List the sounds a song can use.")
    parser.add_argument("--config", type=Path, help="workspace config (default: poser.toml)")
    parser.add_argument("--sounds-dir", type=Path, help="saved sound files")
    parser.add_argument("--json", action="store_true", help="print the list as JSON")
    args = parser.parse_args(argv)

    try:
        fill_path_options(args)
        instruments = load_composition(args).instruments()
    except (WorkspaceError, ConfigError) as error:
        fail(LIST_PROG, error)
        return EXIT_BAD_CONFIG

    entries: list[SoundEntry] = []
    problems: list[str] = []
    notes: list[str] = []
    for name, instrument in instruments.items():
        listing = instrument.sounds()
        entries += listing.entries
        problems += [f"{name}: {problem}" for problem in listing.problems]
        notes += [f"note: {name}: {note}" for note in listing.notes]
    problems += old_layout_problems(Path(args.sounds_dir))

    if args.json:
        data = [{"sound": str(entry.ref), "description": entry.description} for entry in entries]
        print(json.dumps(data, indent=2))
    else:
        rows: list[tuple[str, ...]] = [("SOUND", "DESCRIPTION")]
        rows += [(str(entry.ref), entry.description) for entry in entries]
        print("\n".join(align_rows(rows)))
    for note in notes:
        fail(LIST_PROG, note)
    for problem in problems:
        fail(LIST_PROG, problem)
    return EXIT_SOME_FAILED if problems else EXIT_ALL_PASS


def argument[T](label: str, parse: Callable[[str], T], text: str) -> T:
    """Parse one command-line argument, naming it in the ValueError when it is not valid."""
    try:
        return parse(text)
    except ValueError as error:
        raise ValueError(f"{label}: {error}") from error


def save_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=SAVE_PROG,
        description="Open an instrument's window and save the sound shaped there under a name.",
    )
    parser.add_argument("instrument", help="instrument name in poser.toml")
    parser.add_argument("name", help="sound name, letters, digits, '_' and '-'")
    parser.add_argument("--description", required=True, help="what the sound is and is for")
    parser.add_argument(
        "--from", dest="start", help="start from this sound of the instrument (default: its own)"
    )
    parser.add_argument("--replace", action="store_true", help="overwrite an existing sound")
    parser.add_argument("--config", type=Path, help="workspace config (default: poser.toml)")
    parser.add_argument("--sounds-dir", type=Path, help="saved sound files")
    parser.add_argument("--output-dir", type=Path, help="where previews go")
    args = parser.parse_args(argv)

    try:
        instrument = argument("instrument", Identifier, args.instrument)
        name = argument("name", Identifier, args.name)
        start = None if args.start is None else argument("--from", parse_sound_ref, args.start)
        request = SaveRequest(instrument, name, args.description, start, args.replace)
    except ValueError as error:
        fail(SAVE_PROG, error)
        return EXIT_BAD_CONFIG

    try:
        fill_path_options(args)
        composition = load_composition(args)
        instruments = composition.instruments()
        audio_files = composition.audio_files()
        renders = composition.render_store()
    except (WorkspaceError, ConfigError) as error:
        fail(SAVE_PROG, error)
        return EXIT_BAD_CONFIG

    print(f"Opening {request.instrument}. Choose the sound, then close the window to save it.")
    try:
        result = save_sound(request, instruments, renders, audio_files)
    except KeyboardInterrupt:
        fail(SAVE_PROG, "interrupted, not saved")
        return EXIT_SOME_FAILED

    if isinstance(result, Refused):
        fail(SAVE_PROG, result.problem)
        return EXIT_BAD_CONFIG
    if isinstance(result, NotSaved):
        fail(SAVE_PROG, f"not saved: {result.problem}")
        return EXIT_SOME_FAILED
    if isinstance(result, PreviewFailed):
        fail(SAVE_PROG, f"saved {result.ref}, but its preview could not be written: {result.error}")
        return EXIT_SOME_FAILED
    print(f"sound: {result.ref}")
    print(f"preview: {result.preview} (peak {result.peak_dbfs:.1f} dBFS)")
    if result.peak_dbfs < SILENCE_DBFS:
        print(
            f"{SAVE_PROG}: warning: the preview is below {SILENCE_DBFS:g} dBFS; "
            "the sound is saved, but check it",
            file=sys.stderr,
        )
    return EXIT_ALL_PASS
