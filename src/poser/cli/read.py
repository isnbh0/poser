"""`read-song`: show a song's meter and its authored marks, through the configured vocabularies,
and read its score through the configured frameworks, without loading plugins."""

import argparse
import json
import math
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path

from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    fail,
    load_composition,
)
from poser.cli.workspace import WorkspaceError, fill_path_options
from poser.core.interpreting import interpret_marks, read_song
from poser.core.jsonvalue import JsonValue
from poser.core.loading import load_song
from poser.core.marks import Mark
from poser.core.meter import Bar, Stretch
from poser.core.names import NodeName
from poser.core.ports import ConfigError, Framework, Vocabulary
from poser.core.reading import (
    Agrees,
    Ambiguous,
    AuthoredMarks,
    Check,
    Contradicts,
    FrameworkError,
    Known,
    LayerError,
    Outcome,
    ParsedLayer,
    ReadingValue,
    ScoreRequest,
    SongReadings,
    TextLayer,
    Unchecked,
    Unknowable,
    marks_to_data,
    readings_to_data,
)
from poser.core.rules import plain_number
from poser.core.song import SongError

PROG = "read-song"


def _beat(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(round(float(value), 6))


def _same_beat(first: float, second: float) -> bool:
    """Whether two beats are one beat: beats that differ only by float noise print the same."""
    return math.isclose(first, second, rel_tol=0.0, abs_tol=1e-9)


def _ends_bar(end: float, bar: Bar) -> bool:
    return _same_beat(bar.end, end) or (bar.line is not None and _same_beat(bar.line, end))


def _span(start: float, end: float, bars: Sequence[Bar]) -> str:
    """A range of beats, naming its bars when it starts on a bar's start, ends on a bar's end,
    and every beat between lies in a bar.

    A bar a sliver joined also ends, for a range, on the bar line the sliver runs past.
    """
    beats = f"beats {_beat(start)}-{_beat(end)}"
    first = next((i for i, bar in enumerate(bars) if _same_beat(bar.beat, start)), None)
    last = next((i for i, bar in enumerate(bars) if _ends_bar(end, bar)), None)
    if first is None or last is None or last < first:
        return beats
    covered = bars[first : last + 1]
    if not all(_same_beat(bar.end, following.beat) for bar, following in pairwise(covered)):
        return beats
    low, high = covered[0].number, covered[-1].number
    return f"{beats} (bar {low})" if low == high else f"{beats} (bars {low}-{high})"


def format_meter(stretches: Sequence[Stretch]) -> str:
    """The meter block: one line per metered stretch, or nothing when there is none."""
    lines = [
        f"  beats {_beat(stretch.start)}-{_beat(stretch.end)}: {_beat(stretch.bar)}-beat bars"
        for stretch in stretches
        if stretch.bar is not None
    ]
    return "\n".join(["meter", *lines]) if lines else ""


def meter_to_json(stretches: Sequence[Stretch]) -> JsonValue:
    """The metered stretches' JSON form."""
    return [
        {
            "beat": plain_number(stretch.start),
            "end": plain_number(stretch.end),
            "bar": plain_number(stretch.bar),
        }
        for stretch in stretches
        if stretch.bar is not None
    ]


def _describe(outcome: Outcome[ReadingValue]) -> str:
    match outcome:
        case Known(value=value):
            return value.describe()
        case Ambiguous(candidates=candidates):
            return "ambiguous: " + ", ".join(candidate.describe() for candidate in candidates)
        case Unknowable(reason=reason):
            return f"unknowable: {reason}"


def format_readings(readings: SongReadings, bars: Sequence[Bar] = ()) -> str:
    """One block per framework: a header naming the tracks read, then a line per span, which
    names the bars of `bars` it covers."""
    tracks = ", ".join(readings.tracks) if readings.tracks else "none; every track is unpitched"
    blocks: list[str] = []
    for name, reading in readings.frameworks.items():
        if isinstance(reading, FrameworkError):
            blocks.append(f"{name}: error: {reading.detail}")
            continue
        lines = [f"{name} (tracks: {tracks})"]
        lines += [
            f"  {_span(span.start, span.end, bars)}: {_describe(span.outcome)}"
            for span in reading.spans
        ]
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _check_line(mark: Mark, check: Check | None) -> str | None:
    """What to say under a mark about its check: nothing when it agreed and set no flag."""
    match check:
        case Contradicts(detail=detail):
            return f"{'deliberate' if mark.deliberate else 'warning'}: {detail}"
        case Unchecked(reason=reason):
            return f"unchecked: {reason}"
        case Agrees():
            return "deliberate: not needed, the notes agree" if mark.deliberate else None
        case None:
            return "deliberate: nothing checks this layer" if mark.deliberate else None


def format_marks(authored: AuthoredMarks, bars: Sequence[Bar] = ()) -> str:
    """One block per mark layer: a header naming what read it, then each mark's authored text,
    naming the bars of `bars` it covers, with a line under a mark whose check did not simply
    agree."""
    blocks: list[str] = []
    for name, layer in authored.layers.items():
        match layer:
            case LayerError(detail=detail):
                blocks.append(f"{name} marks: error: {detail}")
                continue
            case ParsedLayer(vocabulary=vocabulary, marks=parsed):
                header = f"{name} marks ({vocabulary} vocabulary)"
                marks = tuple((item.mark, item.check) for item in parsed)
            case TextLayer(marks=text_marks):
                header = f"{name} marks (text)"
                marks = tuple((mark, None) for mark in text_marks)
        lines = [header]
        for mark, check in marks:
            lines.append(f"  {_span(mark.beat, mark.end, bars)}: {mark.text}")
            if (line := _check_line(mark, check)) is not None:
                lines.append(f"    {line}")
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
        vocabularies: tuple[Vocabulary, ...] = composition.vocabularies()
    except (WorkspaceError, ConfigError, ValueError) as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG
    try:
        song = load_song(args.song, formats).graph
        if args.track:
            request = ScoreRequest(song, tuple(NodeName(name) for name in args.track))
        else:
            request = ScoreRequest.default(song)
    except (SongError, ValueError, OSError) as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG
    try:
        authored = interpret_marks(request, vocabularies)
    except SongError as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG

    readings = read_song(request, frameworks)
    stretches = song.meter.stretches(request.end)
    bars = song.meter.bars(request.end)
    if args.json:
        data = readings_to_data(readings)
        if isinstance(data, dict):
            if song.meter:
                # The meter follows `end`, ahead of the tracks and readings.
                rest = {key: value for key, value in data.items() if key != "end"}
                data = {"end": data["end"], "meter": meter_to_json(stretches), **rest}
            if authored.layers:
                data["marks"] = marks_to_data(authored)
        print(json.dumps(data, indent=2))
    else:
        if block := format_meter(stretches):
            print(block)
        if authored.layers:
            print(format_marks(authored, bars))
        if not frameworks:
            preset = composition.config.preset
            print("no frameworks are configured" + (f" (preset {preset})" if preset else ""))
        else:
            print(format_readings(readings, bars))
    failed = any(isinstance(item, FrameworkError) for item in readings.frameworks.values())
    failed = failed or any(isinstance(layer, LayerError) for layer in authored.layers.values())
    return EXIT_SOME_FAILED if failed else EXIT_ALL_PASS
