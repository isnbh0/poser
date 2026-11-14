"""Readings: what a framework finds in a song's score, as outcomes over contiguous spans of beats,
and what a vocabulary makes of a song's authored marks.

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

from poser.core.graph import Graph, TrackNode
from poser.core.jsonvalue import JsonValue
from poser.core.marks import LAYER_PATTERN, LayerName, Mark
from poser.core.names import Identifier, NodeName
from poser.core.rules import Note, line_problems, plain_number, span_problems
from poser.core.song import is_number, song_last_beat


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
        if not (
            isinstance(reason, str) and reason.strip() and not any(c in reason for c in "\r\n")
        ):
            raise ValueError(f"Unknowable.reason must be one non-empty line, got {reason!r}")


@dataclass(frozen=True)
class Rejected:
    """A vocabulary's answer that a label is not one of its values, for the stated reason."""

    reason: str

    def __post_init__(self) -> None:
        if problems := line_problems(self.reason, "Rejected.reason"):
            raise ValueError("\n".join(problems))


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
        if problems := span_problems(self.start, self.end, "ReadingSpan", "start"):
            raise ValueError("\n".join(problems))
        if not isinstance(self.outcome, Known | Ambiguous | Unknowable):
            raise ValueError(f"ReadingSpan.outcome must be an outcome, got {self.outcome!r}")
        object.__setattr__(self, "start", float(self.start))
        object.__setattr__(self, "end", float(self.end))


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


@dataclass(frozen=True)
class ScoreRequest:
    """A reading of the score, the authored notes, of some of a song's pitched tracks.

    `song` is the song's graph, and `tracks` names distinct pitched track nodes of it, and may be
    empty.
    """

    song: Graph
    tracks: tuple[NodeName, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.song, Graph):
            raise ValueError(f"ScoreRequest.song must be a Graph, got {self.song!r}")
        if not isinstance(self.tracks, Sequence) or isinstance(self.tracks, str):
            raise ValueError(f"ScoreRequest.tracks must be a sequence, got {self.tracks!r}")
        known = {node.name: node for node in self.song.nodes}
        names: list[NodeName] = []
        for name in cast("Sequence[object]", self.tracks):
            if not isinstance(name, str) or name not in known:
                raise ValueError(f"no track {name!r} in the song")
            node = known[name]
            if not isinstance(node, TrackNode):
                raise ValueError(f"{name!r} is not a track; readings take pitched tracks only")
            if not node.pitched:
                raise ValueError(f"track {name!r} is unpitched; readings take pitched tracks only")
            if name in names:
                raise ValueError(f"track {name!r} is selected twice")
            names.append(NodeName(name))
        object.__setattr__(self, "tracks", tuple(names))

    @classmethod
    def default(cls, song: Graph) -> "ScoreRequest":
        """Select every pitched track, in source order."""
        return cls(song, tuple(NodeName(node.name) for node in song.track_nodes if node.pitched))

    @property
    def end(self) -> float:
        """The song's last beat: its latest note end or controller change, over every track."""
        return float(song_last_beat(self.song))

    def notes(self) -> tuple[Note, ...]:
        """The selected tracks' notes, track by track in selection order."""
        known = {node.name: node for node in self.song.track_nodes}
        return tuple(note for name in self.tracks for note in known[name].notes)


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
                {"start": plain_number(span.start), "end": plain_number(span.end)}
                | _outcome_to_data(span.outcome)
                for span in reading.spans
            ]
        }
    return {
        "end": plain_number(readings.end),
        "tracks": list(readings.tracks),
        "frameworks": frameworks,
    }


# Authored marks, as the configured vocabularies read them


def _sequence(owner: str, field: str, value: object) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError(f"{owner}.{field} must be a sequence, got {value!r}")
    items = tuple(cast("Sequence[object]", value))
    if not items:
        raise ValueError(f"{owner}.{field} must not be empty")
    return items


def _ordered(owner: str, marks: Sequence[Mark]) -> None:
    for previous, mark in itertools.pairwise(marks):
        if mark.beat < previous.end:
            raise ValueError(
                f"{owner}'s marks must be in order without overlap: one ends at "
                f"{previous.end!r} and the next starts at {mark.beat!r}"
            )


def _identifier(owner: str, value: object) -> None:
    if not isinstance(value, Identifier):
        raise ValueError(f"{owner}.vocabulary must be an Identifier, got {value!r}")


@dataclass(frozen=True)
class Agrees:
    """The notes in a mark's span are consistent with its label."""

    TAG: ClassVar[str] = "agrees"


@dataclass(frozen=True)
class Contradicts:
    """The notes in a mark's span contradict its label; `detail` is a sentence saying how."""

    TAG: ClassVar[str] = "contradicts"
    detail: str

    def __post_init__(self) -> None:
        if problems := line_problems(self.detail, "Contradicts.detail"):
            raise ValueError("\n".join(problems))


@dataclass(frozen=True)
class Unchecked:
    """A mark's label could not be compared with the notes in its span, for the stated reason."""

    TAG: ClassVar[str] = "unchecked"
    reason: str

    def __post_init__(self) -> None:
        if problems := line_problems(self.reason, "Unchecked.reason"):
            raise ValueError("\n".join(problems))


type Check = Agrees | Contradicts | Unchecked


@dataclass(frozen=True)
class ParsedMark:
    """An authored mark, after clipping to the song's end, the value its label names, and the
    result of checking that value against the notes in the mark's span.

    `check` is None when the value makes no claim about the notes.
    """

    mark: Mark
    value: ReadingValue
    check: Check | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mark, Mark):
            raise ValueError(f"ParsedMark.mark must be a Mark, got {self.mark!r}")
        if not isinstance(self.value, ReadingValue):
            raise ValueError(f"ParsedMark.value must be a ReadingValue, got {self.value!r}")
        if not (self.check is None or isinstance(self.check, Agrees | Contradicts | Unchecked)):
            raise ValueError(
                f"ParsedMark.check must be Agrees, Contradicts, Unchecked, or None, got "
                f"{self.check!r}"
            )


@dataclass(frozen=True)
class ParsedLayer:
    """A layer that the vocabulary named `vocabulary` read: its marks, in order, with values."""

    vocabulary: Identifier
    marks: tuple[ParsedMark, ...]

    def __post_init__(self) -> None:
        _identifier("ParsedLayer", self.vocabulary)
        items = _sequence("ParsedLayer", "marks", self.marks)
        for item in items:
            if not isinstance(item, ParsedMark):
                raise ValueError(f"ParsedLayer.marks must hold only ParsedMark, got {item!r}")
        marks = cast("tuple[ParsedMark, ...]", items)
        _ordered("ParsedLayer", [parsed.mark for parsed in marks])
        object.__setattr__(self, "marks", marks)


@dataclass(frozen=True)
class TextLayer:
    """A layer no configured vocabulary reads: its marks, in order, as authored text."""

    marks: tuple[Mark, ...]

    def __post_init__(self) -> None:
        items = _sequence("TextLayer", "marks", self.marks)
        for item in items:
            if not isinstance(item, Mark):
                raise ValueError(f"TextLayer.marks must hold only Mark, got {item!r}")
        marks = cast("tuple[Mark, ...]", items)
        _ordered("TextLayer", marks)
        object.__setattr__(self, "marks", marks)


@dataclass(frozen=True)
class LayerError:
    """The vocabulary named `vocabulary` failed while reading a layer, other than by Rejected."""

    vocabulary: Identifier
    detail: str

    def __post_init__(self) -> None:
        _identifier("LayerError", self.vocabulary)
        if not isinstance(self.detail, str):
            raise ValueError(f"LayerError.detail must be text, got {self.detail!r}")


type LayerOutcome = ParsedLayer | TextLayer | LayerError


def _layer_marks(layer: LayerOutcome) -> tuple[Mark, ...]:
    match layer:
        case ParsedLayer(marks=marks):
            return tuple(parsed.mark for parsed in marks)
        case TextLayer(marks=marks):
            return marks
        case LayerError():
            return ()


@dataclass(frozen=True)
class AuthoredMarks:
    """A song's mark layers, in authored order, each parsed, kept as text, or failed.

    Every mark ends at or before `end`, the song's last beat.
    """

    end: float
    layers: Mapping[LayerName, LayerOutcome]

    def __post_init__(self) -> None:
        end = _finite("AuthoredMarks", "end", self.end)
        if end <= 0:
            raise ValueError(f"AuthoredMarks.end must be above 0, got {end!r}")
        if not isinstance(self.layers, Mapping):
            raise ValueError(f"AuthoredMarks.layers must be a mapping, got {self.layers!r}")
        layers = dict(cast("Mapping[object, object]", self.layers))
        for name, layer in layers.items():
            if not (isinstance(name, str) and LAYER_PATTERN.fullmatch(name)):
                raise ValueError(f"AuthoredMarks has a layer named {name!r}, not a layer name")
            if not isinstance(layer, ParsedLayer | TextLayer | LayerError):
                raise ValueError(
                    f"AuthoredMarks.layers[{name!r}] must be a ParsedLayer, TextLayer, or "
                    f"LayerError, got {layer!r}"
                )
            for mark in _layer_marks(layer):
                if mark.end > end:
                    raise ValueError(
                        f"AuthoredMarks.layers[{name!r}] has a mark ending at beat {mark.end!r}, "
                        f"after the end, beat {end!r}"
                    )
        object.__setattr__(self, "end", end)
        object.__setattr__(
            self, "layers", MappingProxyType(cast("dict[LayerName, LayerOutcome]", layers))
        )


def _mark_data(mark: Mark) -> dict[str, JsonValue]:
    return {"beat": plain_number(mark.beat), "end": plain_number(mark.end), "text": mark.text}


def _flag_data(mark: Mark) -> dict[str, JsonValue]:
    return {"deliberate": True} if mark.deliberate else {}


def _check_data(check: Check | None) -> dict[str, JsonValue]:
    match check:
        case None:
            return {}
        case Agrees():
            return {"check": Agrees.TAG}
        case Contradicts(detail=detail):
            return {"check": Contradicts.TAG, "detail": detail}
        case Unchecked(reason=reason):
            return {"check": Unchecked.TAG, "detail": reason}


def marks_to_data(authored: AuthoredMarks) -> JsonValue:
    """Return the layers' JSON form, in authored order; each value is written by its `to_data`.

    A checked mark carries `check`, with `detail` unless it agrees, and a mark that sets its
    `deliberate` flag carries it.
    """
    layers: dict[str, JsonValue] = {}
    for name, layer in authored.layers.items():
        match layer:
            case ParsedLayer(vocabulary=vocabulary, marks=marks):
                layers[name] = {
                    "vocabulary": vocabulary,
                    "marks": [
                        _mark_data(parsed.mark)
                        | {"value": parsed.value.to_data()}
                        | _check_data(parsed.check)
                        | _flag_data(parsed.mark)
                        for parsed in marks
                    ],
                }
            case TextLayer(marks=marks):
                layers[name] = {
                    "vocabulary": None,
                    "marks": [_mark_data(mark) | _flag_data(mark) for mark in marks],
                }
            case LayerError(vocabulary=vocabulary, detail=detail):
                layers[name] = {"vocabulary": vocabulary, "error": detail}
    return layers
