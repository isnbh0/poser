"""Interpreting: reading a song's score through each configured framework, and translating its
authored marks through each configured vocabulary."""

import dataclasses
from collections.abc import Sequence

from poser.core.evidence import tally_pitches
from poser.core.marks import NO_MARKS, LayerName, Mark
from poser.core.names import Identifier
from poser.core.ports import Claim, Framework, Vocabulary
from poser.core.reading import (
    Agrees,
    Ambiguous,
    AuthoredMarks,
    Check,
    Contradicts,
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
    Unchecked,
)
from poser.core.rules import SongError


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


NOTHING_SOUNDS = "nothing sounds"


def _check(
    parsed: ParsedMark, request: ScoreRequest, vocabulary: Identifier
) -> Check | LayerError | None:
    """Check a parsed mark's value against the notes in the mark's span, when it is a claim."""
    value = parsed.value
    if not isinstance(value, Claim):
        return None
    tally = tally_pitches(request, parsed.mark.beat, min(parsed.mark.end, request.end))
    if tally.total <= 0:
        return Unchecked(NOTHING_SOUNDS)
    try:
        result: object = value.check(tally)
    except Exception as error:  # noqa: BLE001 -- a claim's failure is reported, not raised.
        return LayerError(vocabulary, f"{type(error).__name__}: {error}")
    if not isinstance(result, Agrees | Contradicts | Unchecked):
        return LayerError(
            vocabulary,
            f"TypeError: check returned {result!r}, which is not Agrees, Contradicts, or Unchecked",
        )
    return result


def _parse_layer(
    name: LayerName, marks: Sequence[Mark], vocabulary: Vocabulary, request: ScoreRequest
) -> ParsedLayer | LayerError | list[str]:
    """Parse one layer's labels and check those that are claims: its values, its vocabulary's
    failure, or each rejected label's problem. A vocabulary that fails on any label fails the
    whole layer, and the layer's rejections are dropped, since a failing vocabulary's rejections
    cannot be trusted. Claims are checked only in a layer without rejected labels."""
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
    if rejected:
        return rejected
    checked: list[ParsedMark] = []
    for item in parsed:
        check = _check(item, request, vocabulary.name)
        if isinstance(check, LayerError):
            return check
        checked.append(ParsedMark(item.mark, item.value, check))
    return ParsedLayer(vocabulary.name, tuple(checked))


def interpret_marks(request: ScoreRequest, vocabularies: Sequence[Vocabulary]) -> AuthoredMarks:
    """Translate the request's song's mark layers, clipped to its end, through the vocabularies.

    A vocabulary reads the layer equal to its name; a layer no vocabulary reads stays text. A
    label a vocabulary rejects is the author's error: every rejected label in every layer is
    named in one SongError. A vocabulary that raises, or returns neither a ReadingValue nor
    Rejected, makes its layer a LayerError, and the other layers proceed.

    A value that is a Claim is checked against the request's notes inside its mark's span: a
    span where nothing sounds is Unchecked, and otherwise the result is the value's own. A
    check that raises, or returns no check result, makes its layer a LayerError.
    """
    end = request.end
    marks = request.song.marks.within(end)
    by_name = {vocabulary.name: vocabulary for vocabulary in vocabularies}
    rejected: list[str] = []
    layers: dict[LayerName, LayerOutcome] = {}
    for name, layer in marks.layers.items():
        vocabulary = by_name.get(name)
        if vocabulary is None:
            layers[name] = TextLayer(layer)
        else:
            outcome = _parse_layer(name, layer, vocabulary, request)
            if isinstance(outcome, list):
                rejected += outcome
            else:
                layers[name] = outcome
    if rejected:
        raise SongError(rejected)
    return AuthoredMarks(end, layers)
