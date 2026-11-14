"""Marks: the laws of `Mark` and `Marks`, and `Marks.within`, by exhaustive enumeration."""

import math
from itertools import pairwise, product

import pytest

from poser.core.marks import LAYER_PATTERN, Mark, Marks
from poser.core.song import SongError

BOUNDS = (-math.inf, -1, 0, 0.5, 1, 2, math.inf, math.nan)
TEXTS = ("", " ", "a", " a", "a ", "a\nb", "a\rb", "a b")


@pytest.mark.parametrize(("beat", "end"), list(product(BOUNDS, BOUNDS)))
def test_a_mark_needs_finite_bounds_with_beat_before_end(beat: float, end: float) -> None:
    valid = math.isfinite(beat) and math.isfinite(end) and 0 <= beat < end
    if valid:
        mark = Mark(beat, end, "x")
        assert (mark.beat, mark.end) == (float(beat), float(end))
        assert isinstance(mark.beat, float)
        assert isinstance(mark.end, float)
    else:
        with pytest.raises(SongError, match=r"Mark\.(beat|end)"):
            Mark(beat, end, "x")


@pytest.mark.parametrize("text", TEXTS)
def test_a_mark_text_is_one_line_without_outer_spaces(text: str) -> None:
    valid = bool(text) and "\n" not in text and "\r" not in text and text == text.strip()
    if valid:
        assert Mark(0, 1, text).text == text
    else:
        with pytest.raises(SongError, match=r"Mark\.text"):
            Mark(0, 1, text)


@pytest.mark.parametrize(
    ("beat", "end", "text"),
    [(0, 1, 5), (0, 1, None), (0, 1, b"a"), (True, 2, "a"), (0, True, "a"), ("0", 1, "a")],
)
def test_a_mark_rejects_values_of_the_wrong_type(beat: object, end: object, text: object) -> None:
    with pytest.raises(SongError):
        Mark(beat, end, text)  # ty: ignore[invalid-argument-type]


def test_bounds_are_compared_as_the_floats_they_become() -> None:
    with pytest.raises(SongError, match=r"Mark\.end: must be after beat"):
        Mark(2**53, 2**53 + 1, "x")


class Text(str):
    """A str subclass, which marks store as plain str."""

    __slots__ = ()


def test_text_and_layer_names_are_stored_as_plain_str() -> None:
    marks = Marks({Text("a"): [Mark(0, 1, Text("x"))]})
    assert [type(name) for name in marks.layers] == [str]
    assert type(marks.layers["a"][0].text) is str


def test_a_mark_reports_every_problem() -> None:
    with pytest.raises(SongError) as error:
        Mark(-1, math.nan, "")
    assert [problem.split(":")[0] for problem in error.value.problems] == ["Mark.end", "Mark.text"]
    with pytest.raises(SongError) as error:
        Mark(2, 1, " x")
    assert [problem.split(":")[0] for problem in error.value.problems] == ["Mark.end", "Mark.text"]


GRID = (0, 1, 2, 3)
GRID_MARKS = [Mark(beat, end, "x") for beat, end in product(GRID, GRID) if beat < end]


@pytest.mark.parametrize(("first", "second"), list(product(GRID_MARKS, GRID_MARKS)))
def test_marks_in_a_layer_are_ordered_and_do_not_overlap(first: Mark, second: Mark) -> None:
    if second.beat >= first.end:
        assert Marks({"a": [first, second]}).layers["a"] == (first, second)
    else:
        with pytest.raises(SongError, match=r"Marks\.a\[1\]"):
            Marks({"a": [first, second]})


@pytest.mark.parametrize("name", ["chords", "a", "0", "a-b_c"])
def test_layer_names_are_lowercase(name: str) -> None:
    assert LAYER_PATTERN.fullmatch(name)
    assert list(Marks({name: [Mark(0, 1, "x")]}).layers) == [name]


@pytest.mark.parametrize("name", ["", "-x", "_x", "Chords", "a b", "a.b", "é"])
def test_layer_names_reject_others(name: str) -> None:
    with pytest.raises(SongError, match="layer names are lowercase"):
        Marks({name: [Mark(0, 1, "x")]})


def test_layers_hold_at_least_one_mark_and_only_marks() -> None:
    with pytest.raises(SongError, match=r"Marks\.a: must list at least one mark"):
        Marks({"a": []})
    with pytest.raises(SongError, match=r"Marks\.a\[0\]: must be a Mark"):
        Marks({"a": [(0, 1, "x")]})  # ty: ignore[invalid-argument-type]
    with pytest.raises(SongError, match=r"Marks\.a: must be a list of marks"):
        Marks({"a": "x"})  # ty: ignore[invalid-argument-type]
    with pytest.raises(SongError, match="must be a mapping"):
        Marks([("a", [Mark(0, 1, "x")])])  # ty: ignore[invalid-argument-type]


def test_every_problem_is_reported() -> None:
    with pytest.raises(SongError) as error:
        Marks({"Upper": [Mark(0, 1, "x")], "b": [Mark(0, 2, "x"), Mark(1, 3, "y")]})
    assert len(error.value.problems) == 2
    assert "Marks.Upper" in str(error.value)
    assert "Marks.b[1]" in str(error.value)


def test_equality_and_hashing_follow_layers_in_order() -> None:
    a, b = [Mark(0, 1, "x")], [Mark(1, 2, "y")]
    assert Marks({"a": a, "b": b}) == Marks({"a": tuple(a), "b": tuple(b)})
    assert hash(Marks({"a": a, "b": b})) == hash(Marks({"a": a, "b": b}))
    assert Marks({"a": a, "b": b}) != Marks({"b": b, "a": a})
    assert list(Marks({"b": b, "a": a}).layers) == ["b", "a"]
    assert not Marks()
    assert Marks({"a": a})


def test_layers_view_is_read_only() -> None:
    marks = Marks({"a": [Mark(0, 1, "x")]})
    with pytest.raises(TypeError):
        marks.layers["b"] = ()  # ty: ignore[invalid-assignment]


WITHIN_GRID = (0, 1, 2, 3, 4)
ENDS = (0.5, 1, 2.5, 4, 5)


def layer_sequences() -> list[tuple[Mark, ...]]:
    """Every non-overlapping, ordered layer of one to three marks over the grid."""
    spans = [(beat, end) for beat, end in product(WITHIN_GRID, WITHIN_GRID) if beat < end]
    return [
        tuple(Mark(b, e, f"m{i}") for i, (b, e) in enumerate(chosen))
        for size in (1, 2, 3)
        for chosen in product(spans, repeat=size)
        if all(nxt[0] >= prev[1] for prev, nxt in pairwise(chosen))
    ]


@pytest.mark.parametrize("end", ENDS)
def test_within_keeps_and_clips_marks_before_the_end(end: float) -> None:
    for layer in layer_sequences():
        marks = Marks({"a": layer, "b": [Mark(4, 5, "late")]})
        clipped = marks.within(end)
        kept = [mark for mark in layer if mark.beat < end]
        expected = tuple(Mark(m.beat, min(m.end, end), m.text) for m in kept)
        assert clipped.layers.get("a", ()) == expected
        assert ("b" in clipped.layers) == (end > 4)
        assert all(clipped.layers.values())
        assert Marks(dict(clipped.layers)) == clipped
        assert clipped.within(end) == clipped
        if all(mark.end <= end for mark in layer) and end > 4:
            assert clipped.layers["a"] == layer


@pytest.mark.parametrize("end", [0, -1, math.nan, math.inf, True])
def test_within_needs_a_positive_finite_end(end: float) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        Marks({"a": [Mark(0, 1, "x")]}).within(end)


def test_a_mark_is_not_deliberate_unless_it_says_so() -> None:
    assert Mark(0, 1, "x").deliberate is False
    assert Mark(0, 1, "x", True).deliberate is True
    assert Mark(0, 1, "x", deliberate=True) != Mark(0, 1, "x")


@pytest.mark.parametrize("value", [1, 0, "yes", "true", None])
def test_a_mark_rejects_a_deliberate_flag_that_is_not_a_bool(value: object) -> None:
    with pytest.raises(SongError, match=r"Mark\.deliberate: must be true or false"):
        Mark(0, 1, "x", deliberate=value)  # ty: ignore[invalid-argument-type]


def test_within_keeps_the_flag_on_a_mark_it_cuts() -> None:
    marks = Marks({"a": [Mark(0, 2, "kept", deliberate=True), Mark(2, 8, "cut", deliberate=True)]})

    assert marks.within(4).layers["a"] == (
        Mark(0, 2, "kept", deliberate=True),
        Mark(2, 4, "cut", deliberate=True),
    )
