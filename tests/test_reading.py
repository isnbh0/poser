"""The reading types' laws, the score request, `read_song`'s error handling, and
`interpret_marks`."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from generated_songs import random_song

from poser.adapters.tonal import create_tonal
from poser.core.graph import EffectNode, Graph, TrackNode, Wire
from poser.core.interpreting import interpret_marks, read_song
from poser.core.jsonvalue import JsonValue
from poser.core.marks import Mark, Marks
from poser.core.names import Identifier, NodeName
from poser.core.ports import AdapterContext, Framework, Vocabulary
from poser.core.reading import (
    Ambiguous,
    AuthoredMarks,
    FrameworkError,
    Known,
    LayerError,
    ParsedLayer,
    ParsedMark,
    Reading,
    ReadingSpan,
    ReadingValue,
    Rejected,
    ScoreRequest,
    TextLayer,
    Unknowable,
    marks_to_data,
    readings_to_data,
)
from poser.core.rules import ControlChange, Note, SongError
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
            TrackNode("keys", sound="synth", notes=(NOTE,)),
            TrackNode("drums", sound="kit", notes=(NOTE,), pitched=False),
            EffectNode("wet", effect="reverb"),
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
        ScoreRequest(
            value if isinstance(value, Graph) else value.graph(),
            tuple(NodeName(name) for name in tracks),
        )


def test_a_request_may_select_no_track() -> None:
    assert ScoreRequest(song(KEYS).graph(), ()).tracks == ()


def test_the_default_request_reads_every_pitched_track_in_order() -> None:
    assert ScoreRequest.default(song(BASS, DRUMS, KEYS).graph()).tracks == ("bass", "keys")
    assert ScoreRequest.default(graph()).tracks == ("keys",)
    assert ScoreRequest.default(song(DRUMS).graph()).tracks == ()


def test_a_request_ends_at_the_song_s_last_beat() -> None:
    assert ScoreRequest.default(song(KEYS, BASS).graph()).end == 8
    late = Track("keys", "synth", (NOTE,), cc=(ControlChange(10, 1, 64),))
    assert ScoreRequest.default(song(late).graph()).end == 10
    # Every track counts, including one the request does not read.
    assert ScoreRequest(song(KEYS, BASS).graph(), (NodeName("keys"),)).end == 8


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
    request = ScoreRequest.default(song(KEYS).graph())
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
    request = ScoreRequest(song(KEYS).graph(), ())
    unknowable = Reading(request.end, (ReadingSpan(0, request.end, Unknowable("silence")),))

    readings = read_song(request, [Fake("quiet", unknowable)])

    assert readings.frameworks[Identifier("quiet")] == unknowable


def test_readings_have_a_json_form() -> None:
    request = ScoreRequest.default(song(KEYS, BASS).graph())
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


def test_frameworks_never_see_marks() -> None:
    seen: list[ScoreRequest] = []

    def record(request: ScoreRequest) -> Reading[ReadingValue]:
        seen.append(request)
        return good(request)

    marked = Song(120, (KEYS, BASS), marks=Marks({"parts": [Mark(0, 4, "intro")]})).graph()
    assert marked.marks
    request = ScoreRequest(marked, (NodeName("bass"),))

    read_song(request, [Fake("recording", record)])

    (received,) = seen
    assert received.song.marks == Marks()
    assert received.tracks == ("bass",)
    assert received.song == replace(marked, marks=Marks())


MARKS = Marks(
    {
        "chords": [Mark(0, 4, "Am"), Mark(4, 8, "F"), Mark(8, 64, "C")],
        "parts": [Mark(0.5, 100, "all of it")],
    }
)


def test_marks_never_change_a_reading_of_a_generated_song() -> None:
    """A smoke test over real songs; `test_frameworks_never_see_marks` is the guard."""
    root = Path()
    tonal = cast("Framework", create_tonal({}, AdapterContext(root, root, root)))
    for seed in range(300):
        song = random_song(seed)
        marked = replace(song, marks=MARKS)
        assert read_song(ScoreRequest.default(marked.graph()), [tonal]) == read_song(
            ScoreRequest.default(song.graph()), [tonal]
        ), seed


# Authored marks


@pytest.mark.parametrize("reason", ["", " a", "a ", "a\nb", "a\rb", 3])
def test_a_rejection_needs_one_line_of_reason(reason: object) -> None:
    with pytest.raises(ValueError, match=r"Rejected\.reason"):
        Rejected(cast("str", reason))


def test_a_rejection_keeps_its_reason() -> None:
    assert Rejected("not x or y").reason == "not x or y"


X, Y = Label("x"), Label("y")


def test_authored_marks_hold_every_mark_within_their_end() -> None:
    layers = {
        "fake": ParsedLayer(Identifier("fake"), (ParsedMark(Mark(0, 4, "x"), X),)),
        "parts": TextLayer((Mark(1, 2, "a"),)),
        "broken": LayerError(Identifier("fake2"), "RuntimeError: boom"),
    }
    authored = AuthoredMarks(4, layers)

    assert list(authored.layers) == ["fake", "parts", "broken"]
    with pytest.raises(TypeError):
        authored.layers["more"] = TextLayer((Mark(0, 1, "a"),))  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    "layer",
    [
        ParsedLayer(Identifier("fake"), (ParsedMark(Mark(0, 4.5, "x"), X),)),
        TextLayer((Mark(0, 1, "a"), Mark(3, 5, "b"))),
    ],
    ids=["parsed", "text"],
)
def test_authored_marks_reject_a_mark_ending_after_their_end(
    layer: ParsedLayer | TextLayer,
) -> None:
    with pytest.raises(ValueError, match="after the end"):
        AuthoredMarks(4, {"fake": layer})


@pytest.mark.parametrize(
    ("build", "problem"),
    [
        (lambda: AuthoredMarks(0, {}), "above 0"),
        (lambda: AuthoredMarks(math.inf, {}), "finite"),
        (lambda: AuthoredMarks(4, {"Fake": TextLayer((Mark(0, 1, "a"),))}), "layer name"),
        (lambda: AuthoredMarks(4, {"fake": cast("TextLayer", "a")}), "ParsedLayer"),
        (lambda: TextLayer(()), "not be empty"),
        (lambda: TextLayer((Mark(2, 3, "a"), Mark(0, 1, "b"))), "in order"),
        (lambda: ParsedLayer(Identifier("fake"), ()), "not be empty"),
        (lambda: ParsedLayer(cast("Identifier", "a b"), (ParsedMark(Mark(0, 1, "x"), X),)), "Id"),
        (lambda: ParsedMark(Mark(0, 1, "x"), NOT_A_VALUE), "ReadingValue"),
        (lambda: LayerError(cast("Identifier", ""), "boom"), "Identifier"),
    ],
    ids=[
        "zero-end",
        "infinite-end",
        "uppercase-layer",
        "not-a-layer",
        "empty-text",
        "disorder",
        "empty-parsed",
        "bad-vocabulary",
        "not-a-value",
        "bad-error-vocabulary",
    ],
)
def test_authored_mark_values_reject_broken_laws(build: Callable[[], object], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        build()


class FakeVocabulary:
    """Reads `x` and `y`, rejects other labels, and raises for `boom`."""

    def __init__(self, name: str = "fake", parse: Callable[[str], object] | None = None) -> None:
        self._name = Identifier(name)
        self._parse = parse

    @property
    def name(self) -> Identifier:
        return self._name

    @property
    def guide(self) -> str:
        return f"## {self._name}\n"

    def parse(self, text: str) -> ReadingValue | Rejected:
        if self._parse is not None:
            return cast("ReadingValue | Rejected", self._parse(text))
        if text == "boom":
            raise RuntimeError("boom")
        if text in ("x", "y"):
            return Label(text)
        return Rejected("not x or y")


def test_the_fake_vocabulary_is_a_vocabulary() -> None:
    assert isinstance(FakeVocabulary(), Vocabulary)


def marked(layers: dict[str, list[Mark]]) -> Song:
    """A song ending at beat 4, with the given marks."""
    return Song(120, (KEYS,), marks=Marks(layers))


def test_interpreting_clips_marks_to_the_song_s_end() -> None:
    song = marked({"fake": [Mark(0, 2, "x"), Mark(2, 8, "y"), Mark(9, 10, "x")]})

    authored = interpret_marks(song.graph(), [FakeVocabulary()])

    assert authored.end == 4
    assert authored.layers == {
        "fake": ParsedLayer(
            Identifier("fake"),
            (ParsedMark(Mark(0, 2, "x"), X), ParsedMark(Mark(2, 4, "y"), Y)),
        )
    }


def test_a_layer_no_vocabulary_reads_is_text() -> None:
    song = marked({"parts": [Mark(0, 2, "boom")], "fake": [Mark(0, 1, "x")]})

    authored = interpret_marks(song.graph(), [FakeVocabulary()])

    assert authored.layers["parts"] == TextLayer((Mark(0, 2, "boom"),))
    assert isinstance(authored.layers["fake"], ParsedLayer)
    assert list(authored.layers) == ["parts", "fake"]


def test_interpreting_without_marks_gives_no_layers() -> None:
    assert interpret_marks(song(KEYS).graph(), [FakeVocabulary()]) == AuthoredMarks(4, {})


def test_every_rejected_label_is_named_in_one_song_error() -> None:
    song = marked(
        {"fake": [Mark(0, 1, "x"), Mark(1, 2, "z")], "other": [Mark(0, 1, "w")]},
    )

    with pytest.raises(SongError) as error:
        interpret_marks(song.graph(), [FakeVocabulary(), FakeVocabulary("other")])

    assert error.value.problems == [
        "marks.fake[1]: 'z' is not a fake label: not x or y",
        "marks.other[0]: 'w' is not a other label: not x or y",
    ]


def test_a_vocabulary_that_raises_fails_only_its_layer() -> None:
    song = marked({"fake": [Mark(0, 1, "x"), Mark(1, 2, "boom")], "other": [Mark(0, 1, "y")]})

    authored = interpret_marks(song.graph(), [FakeVocabulary(), FakeVocabulary("other")])

    assert authored.layers["fake"] == LayerError(Identifier("fake"), "RuntimeError: boom")
    assert authored.layers["other"] == ParsedLayer(
        Identifier("other"), (ParsedMark(Mark(0, 1, "y"), Y),)
    )


@pytest.mark.parametrize("result", [None, "x", NotAValue("x")], ids=["none", "str", "object"])
def test_a_vocabulary_returning_neither_value_nor_rejection_fails_its_layer(
    result: object,
) -> None:
    song = marked({"fake": [Mark(0, 1, "x")]})

    authored = interpret_marks(song.graph(), [FakeVocabulary(parse=lambda _: result)])

    layer = authored.layers["fake"]
    assert isinstance(layer, LayerError)
    assert layer.vocabulary == "fake"
    assert "neither a ReadingValue nor Rejected" in layer.detail


def test_authored_marks_have_a_json_form() -> None:
    song = marked(
        {
            "fake": [Mark(0, 1, "x"), Mark(1.5, 6, "y")],
            "parts": [Mark(0, 2, "intro")],
            "other": [Mark(0, 1, "boom")],
        }
    )

    authored = interpret_marks(song.graph(), [FakeVocabulary(), FakeVocabulary("other")])

    assert marks_to_data(authored) == {
        "fake": {
            "vocabulary": "fake",
            "marks": [
                {"beat": 0, "end": 1, "text": "x", "value": {"label": "x"}},
                {"beat": 1.5, "end": 4, "text": "y", "value": {"label": "y"}},
            ],
        },
        "parts": {"vocabulary": None, "marks": [{"beat": 0, "end": 2, "text": "intro"}]},
        "other": {"vocabulary": "other", "error": "RuntimeError: boom"},
    }
