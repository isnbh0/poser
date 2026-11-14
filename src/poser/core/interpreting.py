"""Interpreting: reading a song's score through each configured framework, and translating its
authored marks through each configured vocabulary."""

import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING

from poser.core.graph import Graph
from poser.core.marks import NO_MARKS, LayerName, Mark
from poser.core.ports import Framework, Vocabulary
from poser.core.reading import (
    Ambiguous,
    AuthoredMarks,
    FrameworkError,
    Known,
    LayerError,
    LayerOutcome,
    ParsedLayer,
    ParsedMark,
    Reading,
    ReadingValue,
    Rejected,
    ScoreRequest,
    SongReadings,
    TextLayer,
)
from poser.core.rules import SongError
from poser.core.song import song_last_beat

if TYPE_CHECKING:
    from poser.core.names import Identifier


def _values(reading: Reading[ReadingValue]) -> list[object]:
    values: list[object] = []
    for span in reading.spans:
        match span.outcome:
            case Known(value=value):
                values.append(value)
            case Ambiguous(candidates=candidates):
                values.extend(candidates)
            case _:
                pass
    return values


def _read(framework: Framework, request: ScoreRequest) -> Reading[ReadingValue] | FrameworkError:
    try:
        reading = framework.read(request)
    except Exception as error:  # noqa: BLE001 -- a framework's failure is reported, not raised.
        return FrameworkError(f"{type(error).__name__}: {error}")
    if not isinstance(reading, Reading):
        return FrameworkError(f"returned {type(reading).__name__}, not a Reading")
    for value in _values(reading):
        if not isinstance(value, ReadingValue):
            return FrameworkError(f"returned a value {value!r}, which is not a ReadingValue")
    if reading.end != request.end:
        return FrameworkError(
            f"returned a reading ending at beat {reading.end!r}, but the song ends at beat "
            f"{request.end!r}"
        )
    return reading


def read_song(request: ScoreRequest, frameworks: Sequence[Framework]) -> SongReadings:
    """Read the request through each framework, in order.

    Each framework receives the request's song with its marks removed, and the same tracks, so
    no reading can depend on what the author stated. A framework that raises, returns something
    other than a Reading of ReadingValues, or returns a reading whose end is not the request's end
    is recorded as a FrameworkError; the others' readings are unaffected. An unknowable span is an
    outcome, never an error.
    """
    unmarked = ScoreRequest(dataclasses.replace(request.song, marks=NO_MARKS), request.tracks)
    readings: dict[Identifier, Reading[ReadingValue] | FrameworkError] = {
        framework.name: _read(framework, unmarked) for framework in frameworks
    }
    return SongReadings(request.end, request.tracks, readings)


def _parse_layer(
    name: LayerName, marks: Sequence[Mark], vocabulary: Vocabulary
) -> ParsedLayer | LayerError | list[str]:
    """Parse one layer's labels: its values, its vocabulary's failure, or each rejected label's
    problem. A vocabulary that fails on any label fails the whole layer, and the layer's
    rejections are dropped, since a failing vocabulary's rejections cannot be trusted."""
    parsed: list[ParsedMark] = []
    rejected: list[str] = []
    for index, mark in enumerate(marks):
        try:
            result = vocabulary.parse(mark.text)
        except Exception as error:  # noqa: BLE001 -- a vocabulary's failure is reported.
            return LayerError(vocabulary.name, f"{type(error).__name__}: {error}")
        if isinstance(result, Rejected):
            rejected.append(
                f"marks.{name}[{index}]: {mark.text!r} is not a {name} label: {result.reason}"
            )
        elif isinstance(result, ReadingValue):
            parsed.append(ParsedMark(mark, result))
        else:
            return LayerError(
                vocabulary.name,
                f"TypeError: returned {result!r}, which is neither a ReadingValue nor Rejected",
            )
    return rejected or ParsedLayer(vocabulary.name, tuple(parsed))


def interpret_marks(graph: Graph, vocabularies: Sequence[Vocabulary]) -> AuthoredMarks:
    """Translate the song's mark layers, clipped to its end, through the vocabularies.

    A vocabulary reads the layer equal to its name; a layer no vocabulary reads stays text. A
    label a vocabulary rejects is the author's error: every rejected label in every layer is
    named in one SongError. A vocabulary that raises, or returns neither a ReadingValue nor
    Rejected, makes its layer a LayerError, and the other layers proceed.
    """
    end = float(song_last_beat(graph))
    marks = graph.marks.within(end)
    by_name = {vocabulary.name: vocabulary for vocabulary in vocabularies}
    rejected: list[str] = []
    layers: dict[LayerName, LayerOutcome] = {}
    for name, layer in marks.layers.items():
        vocabulary = by_name.get(name)
        if vocabulary is None:
            layers[name] = TextLayer(layer)
        else:
            outcome = _parse_layer(name, layer, vocabulary)
            if isinstance(outcome, list):
                rejected += outcome
            else:
                layers[name] = outcome
    if rejected:
        raise SongError(rejected)
    return AuthoredMarks(end, layers)
