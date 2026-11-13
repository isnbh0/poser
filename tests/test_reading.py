"""The reading types' laws, the score request, and `read_song`'s error handling."""

import math
import re
from dataclasses import dataclass
from typing import cast

import pytest

from poser.core.graph import Graph, Node, Wire
from poser.core.interpreting import read_song
from poser.core.jsonvalue import JsonValue
from poser.core.names import Identifier, NodeName
from poser.core.reading import (
    Ambiguous,
    FrameworkError,
    Known,
    Reading,
    ReadingSpan,
    ReadingValue,
    ScoreRequest,
    Unknowable,
    readings_to_data,
)
from poser.core.rules import ControlChange, Note
from poser.core.song import Song, Track


@dataclass(frozen=True)
class Label:
    text: str

    def to_data(self) -> JsonValue:
        return {"label": self.text}

    def describe(self) -> str:
        return self.text


A, B = Label("a"), Label("b")
NOTE = Note(0, 60, 4, 100)


def span(start: float, end: float) -> ReadingSpan[Label]:
    return ReadingSpan(start, end, Known(A))


def song(*tracks: Track) -> Song:
    return Song(120, tracks)


KEYS = Track("keys", "synth", (NOTE,))
BASS = Track("bass", "synth", (Note(2, 36, 6, 100),))
DRUMS = Track("drums", "kit", (Note(0, 36, 1, 100),), pitched=False)


# Reading


def test_a_reading_accepts_one_span_and_a_contiguous_chain() -> None:
    assert Reading(4, (span(0, 4),)).spans == (span(0, 4),)
    chain = (span(0, 1.5), span(1.5, 3), span(3, 4))
    assert Reading(4, chain).spans == chain


@pytest.mark.parametrize(
    ("end", "spans", "problem"),
    [
        (4, (span(0, 1), span(2, 4)), "contiguous"),
        (4, (span(0, 2), span(1, 4)), "contiguous"),
        (4, (span(1, 4),), "start at 0"),
        (4, (span(0, 3),), "end at 4"),
        (4, (), "empty"),
        (0, (span(0, 1),), "above 0"),
        (-1, (span(0, 1),), "above 0"),
        (math.inf, (span(0, 1),), "finite"),
        (math.nan, (span(0, 1),), "finite"),
    ],
    ids=["gap", "overlap", "late-start", "short", "empty", "zero", "negative", "inf", "nan"],
)
def test_a_reading_rejects_a_broken_cover(
    end: float, spans: tuple[ReadingSpan[Label], ...], problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        Reading(end, spans)


# Outcomes and spans


def test_ambiguous_needs_two_distinct_candidates() -> None:
    assert Ambiguous((A, B)).candidates == (A, B)
    with pytest.raises(ValueError, match="at least two"):
        Ambiguous((A,))
    with pytest.raises(ValueError, match="twice"):
        Ambiguous((A, B, Label("a")))


@pytest.mark.parametrize("reason", ["", "  ", "two\nlines", "trailing\n", "carriage\rreturn"])
def test_unknowable_needs_one_line_of_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="one non-empty line"):
        Unknowable(reason)


@pytest.mark.parametrize(
    ("start", "end", "problem"),
    [
        (2, 2, "ReadingSpan.end: must be after start 2"),
        (3, 2, "ReadingSpan.end: must be after start 3"),
        (-1, 2, "ReadingSpan.start: must not be negative"),
        (0, math.inf, "ReadingSpan.end: must be a finite number"),
        (-math.inf, 1, "ReadingSpan.start: must be a finite number"),
        (math.nan, 1, "ReadingSpan.start: must be a finite number"),
        (0, math.nan, "ReadingSpan.end: must be a finite number"),
        (True, 2, "ReadingSpan.start: must be a finite number"),
        ("0", 2, "ReadingSpan.start: must be a finite number"),
        (2**53, 2**53 + 1, "ReadingSpan.end: must be after start"),
    ],
)
def test_a_span_needs_a_finite_start_before_its_end(start: float, end: float, problem: str) -> None:
    with pytest.raises(ValueError, match=re.escape(problem)):
        ReadingSpan(start, end, Unknowable("why"))


# ScoreRequest


def graph() -> Graph:
    return Graph(
        120,
        (
            Node("keys", "track", sound="synth", notes=(NOTE,)),
            Node("drums", "track", sound="kit", notes=(NOTE,), pitched=False),
            Node("wet", "effect", effect="reverb"),
        ),
        (Wire("keys", "wet", "audio"),),
    )


@pytest.mark.parametrize(
    ("value", "tracks", "named"),
    [
        (song(KEYS), ("lead",), "'lead'"),
        (song(KEYS), ("keys", "keys"), "'keys' is selected twice"),
        (graph(), ("wet",), "'wet' is not a track"),
        (song(KEYS, DRUMS), ("drums",), "track 'drums' is unpitched"),
        (graph(), ("drums",), "track 'drums' is unpitched"),
    ],
    ids=["unknown", "duplicate", "effect-node", "unpitched", "unpitched-node"],
)
def test_a_request_rejects_a_track_it_cannot_read(
    value: Song | Graph, tracks: tuple[str, ...], named: str
) -> None:
    with pytest.raises(ValueError, match=named):
        ScoreRequest(value, tuple(NodeName(name) for name in tracks))


def test_a_request_may_select_no_track() -> None:
    assert ScoreRequest(song(KEYS), ()).tracks == ()


def test_the_default_request_reads_every_pitched_track_in_order() -> None:
    assert ScoreRequest.default(song(BASS, DRUMS, KEYS)).tracks == ("bass", "keys")
    assert ScoreRequest.default(graph()).tracks == ("keys",)
    assert ScoreRequest.default(song(DRUMS)).tracks == ()


def test_a_request_ends_at_the_song_s_last_beat() -> None:
    assert ScoreRequest.default(song(KEYS, BASS)).end == 8
    late = Track("keys", "synth", (NOTE,), cc=(ControlChange(10, 1, 64),))
    assert ScoreRequest.default(song(late)).end == 10
    # Every track counts, including one the request does not read.
    assert ScoreRequest(song(KEYS, BASS), (NodeName("keys"),)).end == 8


# read_song


class Fake:
    def __init__(self, name: str, read: object) -> None:
        self._name = Identifier(name)
        self._read = read

    @property
    def name(self) -> Identifier:
        return self._name

    @property
    def guide(self) -> str:
        return f"## {self._name}\n"

    def read(self, request: ScoreRequest) -> Reading[ReadingValue]:
        result = self._read(request) if callable(self._read) else self._read
        return cast("Reading[ReadingValue]", result)


def good(request: ScoreRequest) -> Reading[ReadingValue]:
    return Reading(request.end, (ReadingSpan(0, request.end, Known(A)),))


def raises(request: ScoreRequest) -> Reading[ReadingValue]:
    raise RuntimeError("no luck")


@dataclass(frozen=True)
class NotAValue:
    text: str


NOT_A_VALUE = cast("ReadingValue", NotAValue("x"))


def test_read_song_records_each_broken_framework_as_an_error() -> None:
    request = ScoreRequest.default(song(KEYS))
    broken = {
        "raises": Fake("raises", raises),
        "wrong-type": Fake("wrong-type", "a string"),
        "bad-value": Fake("bad-value", Reading(4, (ReadingSpan(0, 4, Known(NOT_A_VALUE)),))),
        "bad-candidate": Fake(
            "bad-candidate",
            Reading(4, (ReadingSpan(0, 4, Ambiguous((A, NOT_A_VALUE))),)),
        ),
        "short": Fake("short", Reading(3, (span(0, 3),))),
    }
    frameworks = [Fake("first", good), *broken.values(), Fake("last", good)]

    readings = read_song(request, frameworks)

    assert list(readings.frameworks) == [framework.name for framework in frameworks]
    assert readings.frameworks[Identifier("first")] == good(request)
    assert readings.frameworks[Identifier("last")] == good(request)
    errors = {name: readings.frameworks[Identifier(name)] for name in broken}
    assert all(isinstance(error, FrameworkError) for error in errors.values())
    details = {
        name: error.detail for name, error in errors.items() if isinstance(error, FrameworkError)
    }
    assert details["raises"] == "RuntimeError: no luck"
    assert "not a Reading" in details["wrong-type"]
    assert "not a ReadingValue" in details["bad-value"]
    assert "not a ReadingValue" in details["bad-candidate"]
    assert "3" in details["short"]
    assert "4" in details["short"]


def test_an_unknowable_reading_is_not_an_error() -> None:
    request = ScoreRequest(song(KEYS), ())
    unknowable = Reading(request.end, (ReadingSpan(0, request.end, Unknowable("silence")),))

    readings = read_song(request, [Fake("quiet", unknowable)])

    assert readings.frameworks[Identifier("quiet")] == unknowable


def test_readings_have_a_json_form() -> None:
    request = ScoreRequest.default(song(KEYS, BASS))
    reading = Reading(
        8,
        (
            ReadingSpan(0, 2.5, Known(A)),
            ReadingSpan(2.5, 4, Ambiguous((A, B))),
            ReadingSpan(4, 8, Unknowable("too quiet")),
        ),
    )

    readings = read_song(request, [Fake("labels", reading), Fake("broken", raises)])

    assert readings_to_data(readings) == {
        "end": 8,
        "tracks": ["keys", "bass"],
        "frameworks": {
            "labels": {
                "spans": [
                    {"start": 0, "end": 2.5, "outcome": "known", "value": {"label": "a"}},
                    {
                        "start": 2.5,
                        "end": 4,
                        "outcome": "ambiguous",
                        "candidates": [{"label": "a"}, {"label": "b"}],
                    },
                    {"start": 4, "end": 8, "outcome": "unknowable", "reason": "too quiet"},
                ]
            },
            "broken": {"error": "RuntimeError: no luck"},
        },
    }
