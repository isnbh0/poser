"""Readings: what a framework finds in a song's score, as outcomes over contiguous spans of beats.

Every type is a frozen dataclass whose constructor raises ValueError on a broken law, so a
malformed reading cannot be built. A framework owns its value type: a consumer narrows a value
with `isinstance`, never by reading its JSON form.
"""

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Protocol, cast, runtime_checkable

from poser.core.graph import Graph
from poser.core.jsonvalue import JsonValue
from poser.core.names import Identifier, NodeName
from poser.core.rules import Note, _plain
from poser.core.song import Song, is_number, song_last_beat


@runtime_checkable
class ReadingValue(Protocol):
    """What a framework found in one span.

    Values compare with `==`, so a framework's value type is a frozen dataclass.
    """

    def to_data(self) -> JsonValue:
        """The value's JSON form."""
        ...

    def describe(self) -> str:
        """One line of text for `read-song`."""
        ...


@dataclass(frozen=True)
class Known[V: ReadingValue]:
    """The span has one reading."""

    TAG: ClassVar[str] = "known"
    value: V


@dataclass(frozen=True)
class Ambiguous[V: ReadingValue]:
    """The span fits two or more readings, most likely first. A framework that ranks candidates
    by a score keeps that score in its value type."""

    TAG: ClassVar[str] = "ambiguous"
    candidates: tuple[V, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, Sequence) or isinstance(self.candidates, str):
            raise ValueError(f"Ambiguous.candidates must be a sequence, got {self.candidates!r}")
        candidates = tuple(cast("Sequence[V]", self.candidates))
        if len(candidates) < 2:
            raise ValueError(f"Ambiguous needs at least two candidates, got {len(candidates)}")
        for index, candidate in enumerate(candidates):
            if any(candidate == earlier for earlier in candidates[:index]):
                raise ValueError(f"Ambiguous.candidates holds {candidate!r} twice")
        object.__setattr__(self, "candidates", candidates)


@dataclass(frozen=True)
class Unknowable:
    """The span cannot be read by this framework, for the stated reason. A result, not an error."""

    TAG: ClassVar[str] = "unknowable"
    reason: str

    def __post_init__(self) -> None:
        reason = self.reason
        if not (isinstance(reason, str) and reason.strip() and len(reason.splitlines()) == 1):
            raise ValueError(f"Unknowable.reason must be one non-empty line, got {reason!r}")


type Outcome[V: ReadingValue] = Known[V] | Ambiguous[V] | Unknowable

OUTCOME_TAGS = (Known.TAG, Ambiguous.TAG, Unknowable.TAG)


def _finite(owner: str, field: str, value: object) -> float:
    if not (is_number(value) and math.isfinite(value)):
        raise ValueError(f"{owner}.{field} must be a finite number, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class ReadingSpan[V: ReadingValue]:
    """One outcome over the beats from `start` to `end`."""

    start: float
    end: float
    outcome: Outcome[V]

    def __post_init__(self) -> None:
        start = _finite("ReadingSpan", "start", self.start)
        end = _finite("ReadingSpan", "end", self.end)
        if not 0 <= start < end:
            raise ValueError(f"ReadingSpan needs 0 <= start < end, got {start!r} to {end!r}")
        if not isinstance(self.outcome, Known | Ambiguous | Unknowable):
            raise ValueError(f"ReadingSpan.outcome must be an outcome, got {self.outcome!r}")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True)
class Reading[V: ReadingValue]:
    """A framework's reading of a whole song: spans covering the beats from 0 to `end` exactly,
    each starting where the previous one ended."""

    end: float
    spans: tuple[ReadingSpan[V], ...]

    def __post_init__(self) -> None:
        end = _finite("Reading", "end", self.end)
        if end <= 0:
            raise ValueError(f"Reading.end must be above 0, got {end!r}")
        if not isinstance(self.spans, Sequence) or isinstance(self.spans, str):
            raise ValueError(f"Reading.spans must be a sequence, got {self.spans!r}")
        spans = tuple(cast("Sequence[object]", self.spans))
        if not spans:
            raise ValueError("Reading.spans must not be empty")
        for span in spans:
            if not isinstance(span, ReadingSpan):
                raise ValueError(f"Reading.spans must hold only ReadingSpan, got {span!r}")
        typed = cast("tuple[ReadingSpan[V], ...]", spans)
        if typed[0].start != 0:
            raise ValueError(f"Reading's first span must start at 0, got {typed[0].start!r}")
        for previous, span in itertools.pairwise(typed):
            if span.start != previous.end:
                raise ValueError(
                    f"Reading's spans must be contiguous: one ends at {previous.end!r} and the "
                    f"next starts at {span.start!r}"
                )
        if typed[-1].end != end:
            raise ValueError(
                f"Reading's last span must end at {end!r}, the reading's end, got {typed[-1].end!r}"
            )
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "spans", typed)


def _track_notes(song: Song | Graph) -> dict[str, tuple[bool, bool, tuple[Note, ...]]]:
    """Each node's name, with whether it is a track, whether it is pitched, and its notes."""
    if isinstance(song, Song):
        return {track.name: (True, track.pitched, track.notes) for track in song.tracks}
    return {node.name: (node.kind == "track", node.pitched, node.notes) for node in song.nodes}


@dataclass(frozen=True)
class ScoreRequest:
    """A reading of the score, the authored notes, of some of a song's pitched tracks.

    `tracks` names distinct pitched tracks of the song, a Song's tracks or a Graph's track nodes,
    and may be empty.
    """

    song: Song | Graph
    tracks: tuple[NodeName, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.song, Song | Graph):
            raise ValueError(f"ScoreRequest.song must be a Song or Graph, got {self.song!r}")
        if not isinstance(self.tracks, Sequence) or isinstance(self.tracks, str):
            raise ValueError(f"ScoreRequest.tracks must be a sequence, got {self.tracks!r}")
        known = _track_notes(self.song)
        names: list[NodeName] = []
        for name in cast("Sequence[object]", self.tracks):
            if not isinstance(name, str) or name not in known:
                raise ValueError(f"no track {name!r} in the song")
            is_track, pitched, _ = known[name]
            if not is_track:
                raise ValueError(f"{name!r} is not a track; readings take pitched tracks only")
            if not pitched:
                raise ValueError(f"track {name!r} is unpitched; readings take pitched tracks only")
            if name in names:
                raise ValueError(f"track {name!r} is selected twice")
            names.append(NodeName(name))
        object.__setattr__(self, "tracks", tuple(names))

    @classmethod
    def default(cls, song: Song | Graph) -> "ScoreRequest":
        """Select every pitched track, in source order."""
        names = [
            name
            for name, (is_track, pitched, _) in _track_notes(song).items()
            if is_track and pitched
        ]
        return cls(song, tuple(NodeName(name) for name in names))

    @property
    def end(self) -> float:
        """The song's last beat: its latest note end or controller change, over every track."""
        return float(song_last_beat(self.song))

    def notes(self) -> tuple[Note, ...]:
        """The selected tracks' notes, track by track in selection order."""
        known = _track_notes(self.song)
        return tuple(note for name in self.tracks for note in known[name][2])


@dataclass(frozen=True)
class FrameworkError:
    """A framework failed to read: it raised, or returned something that is not a valid reading."""

    detail: str


@dataclass(frozen=True)
class SongReadings:
    """Each configured framework's reading of one request, or its error, in configured order."""

    end: float
    tracks: tuple[NodeName, ...]
    frameworks: Mapping[Identifier, Reading[ReadingValue] | FrameworkError]

    def __post_init__(self) -> None:
        object.__setattr__(self, "tracks", tuple(self.tracks))
        object.__setattr__(self, "frameworks", MappingProxyType(dict(self.frameworks)))


def _outcome_to_data(outcome: Outcome[ReadingValue]) -> dict[str, JsonValue]:
    match outcome:
        case Known(value=value):
            return {"outcome": Known.TAG, "value": value.to_data()}
        case Ambiguous(candidates=candidates):
            return {
                "outcome": Ambiguous.TAG,
                "candidates": [candidate.to_data() for candidate in candidates],
            }
        case Unknowable(reason=reason):
            return {"outcome": Unknowable.TAG, "reason": reason}


def readings_to_data(readings: SongReadings) -> JsonValue:
    """Return the readings' JSON form; each value is written by its own `to_data`."""
    frameworks: dict[str, JsonValue] = {}
    for name, reading in readings.frameworks.items():
        if isinstance(reading, FrameworkError):
            frameworks[name] = {"error": reading.detail}
            continue
        frameworks[name] = {
            "spans": [
                {"start": _plain(span.start), "end": _plain(span.end)}
                | _outcome_to_data(span.outcome)
                for span in reading.spans
            ]
        }
    return {
        "end": _plain(readings.end),
        "tracks": list(readings.tracks),
        "frameworks": frameworks,
    }
