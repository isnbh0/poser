"""Interpreting: reading a song's score through each configured framework."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from poser.core.ports import Framework
from poser.core.reading import (
    Ambiguous,
    FrameworkError,
    Known,
    Reading,
    ReadingValue,
    ScoreRequest,
    SongReadings,
)

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

    A framework that raises, returns something other than a Reading of ReadingValues, or returns a
    reading whose end is not the request's end is recorded as a FrameworkError; the others'
    readings are unaffected. An unknowable span is an outcome, never an error.
    """
    readings: dict[Identifier, Reading[ReadingValue] | FrameworkError] = {
        framework.name: _read(framework, request) for framework in frameworks
    }
    return SongReadings(request.end, request.tracks, readings)
