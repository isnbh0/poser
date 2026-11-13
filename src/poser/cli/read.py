"""`read-song`: read a song's score through the configured frameworks, without loading plugins."""

import argparse
import json
from pathlib import Path

from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    fail,
    load_composition,
)
from poser.cli.workspace import WorkspaceError, fill_path_options
from poser.core.interpreting import read_song
from poser.core.loading import load_song
from poser.core.names import NodeName
from poser.core.ports import ConfigError, Framework
from poser.core.reading import (
    Ambiguous,
    FrameworkError,
    Known,
    Outcome,
    ReadingValue,
    ScoreRequest,
    SongReadings,
    Unknowable,
    readings_to_data,
)
from poser.core.song import SongError

PROG = "read-song"


def _beat(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(round(float(value), 6))


def _describe(outcome: Outcome[ReadingValue]) -> str:
    match outcome:
        case Known(value=value):
            return value.describe()
        case Ambiguous(candidates=candidates):
            return "ambiguous: " + ", ".join(candidate.describe() for candidate in candidates)
        case Unknowable(reason=reason):
            return f"unknowable: {reason}"


def format_readings(readings: SongReadings) -> str:
    """One block per framework: a header naming the tracks read, then a line per span."""
    tracks = ", ".join(readings.tracks) if readings.tracks else "none; every track is unpitched"
    blocks: list[str] = []
    for name, reading in readings.frameworks.items():
        if isinstance(reading, FrameworkError):
            blocks.append(f"{name}: error: {reading.detail}")
            continue
        lines = [f"{name} (tracks: {tracks})"]
        lines += [
            f"  beats {_beat(span.start)}-{_beat(span.end)}: {_describe(span.outcome)}"
            for span in reading.spans
        ]
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _selected(configured: tuple[Framework, ...], names: list[str]) -> tuple[Framework, ...]:
    """The configured frameworks the `--framework` names select, in configured order."""
    if not names:
        return configured
    known = {framework.name for framework in configured}
    for name in names:
        if name not in known:
            listed = ", ".join(sorted(known)) or "none"
            raise ValueError(f"no framework {name!r} is configured; configured: {listed}")
    return tuple(framework for framework in configured if framework.name in names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    parser.add_argument("song", type=Path, help="trusted song file (Python build() is executed)")
    parser.add_argument("--config", type=Path, help="workspace config (default: poser.toml)")
    parser.add_argument(
        "--track",
        action="append",
        default=[],
        metavar="NAME",
        help="read this pitched track; repeat for more (default: every pitched track)",
    )
    parser.add_argument(
        "--framework",
        action="append",
        default=[],
        metavar="NAME",
        help="read through this configured framework; repeat for more (default: all)",
    )
    parser.add_argument("--json", action="store_true", help="print the readings as JSON")
    args = parser.parse_args(argv)

    try:
        fill_path_options(args)
        composition = load_composition(args)
        formats = composition.song_formats()
        frameworks = _selected(composition.frameworks(), args.framework)
    except (WorkspaceError, ConfigError, ValueError) as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG
    try:
        song = load_song(args.song, formats)
        if args.track:
            request = ScoreRequest(song, tuple(NodeName(name) for name in args.track))
        else:
            request = ScoreRequest.default(song)
    except (SongError, ValueError, OSError) as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG

    readings = read_song(request, frameworks)
    if args.json:
        print(json.dumps(readings_to_data(readings), indent=2))
    elif not frameworks:
        preset = composition.config.preset
        print("no frameworks are configured" + (f" (preset {preset})" if preset else ""))
    else:
        print(format_readings(readings))
    failed = any(isinstance(item, FrameworkError) for item in readings.frameworks.values())
    return EXIT_SOME_FAILED if failed else EXIT_ALL_PASS
