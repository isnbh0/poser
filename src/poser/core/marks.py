"""Marks: the author's labels over spans of beats, in named layers.

A layer is named for what it describes, such as `chords`, `key`, or `parts`. Marks in a layer
are in order and do not overlap; a gap means nothing was said there. Marks never change audio or
readings. Every mark object is valid: its constructor raises `SongError` listing every problem.
"""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType
from typing import TypedDict, cast

from poser.core.rules import (
    SongError,
    _at,
    _line_problems,
    _plain,
    _span_problems,
    is_number,
)

LAYER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")

type LayerName = str


@dataclass(frozen=True)
class Mark:
    """One label over the beats from `beat` to `end`."""

    beat: float
    end: float
    text: str

    def __post_init__(self) -> None:
        values = {"beat": self.beat, "end": self.end, "text": self.text}
        if problems := _mark_problems(values, "Mark"):
            raise SongError(problems)
        object.__setattr__(self, "beat", float(self.beat))
        object.__setattr__(self, "end", float(self.end))
        object.__setattr__(self, "text", str(self.text))


_EMPTY: dict[str, Sequence[Mark]] = {}
_NO_LAYERS: Mapping[str, Sequence[Mark]] = MappingProxyType(_EMPTY)


@dataclass(frozen=True, init=False)
class Marks:
    """A song's layers of marks, in authored order. Equal layers in equal order are equal."""

    pairs: tuple[tuple[LayerName, tuple[Mark, ...]], ...]

    def __init__(self, layers: Mapping[str, Sequence[Mark]] = _NO_LAYERS) -> None:
        if not isinstance(layers, Mapping):
            raise SongError([f"Marks: must be a mapping of layer names to marks, got {layers!r}"])
        items = cast("Mapping[object, object]", layers)
        pairs = list(items.items())
        if problems := _marks_problems(pairs, "Marks"):
            raise SongError(problems)
        typed = cast("list[tuple[str, Sequence[Mark]]]", pairs)
        object.__setattr__(self, "pairs", tuple((str(name), tuple(marks)) for name, marks in typed))

    @property
    def layers(self) -> Mapping[LayerName, tuple[Mark, ...]]:
        """The layers by name, in authored order, read-only."""
        return MappingProxyType(dict(self.pairs))

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def within(self, end: float) -> "Marks":
        """Return the marks clipped to a song ending at beat `end`.

        Marks starting at or after `end` are dropped, the rest end no later than `end`, and
        layers left empty are dropped. A kept mark keeps its index in its layer, since only a
        layer's last marks can start at or after `end`.
        """
        if not (is_number(end) and math.isfinite(end) and end > 0):
            raise ValueError(f"Marks.within needs a positive finite end, got {end!r}")
        layers: dict[str, list[Mark]] = {}
        for name, marks in self.pairs:
            kept = [
                mark if mark.end <= end else Mark(mark.beat, end, mark.text)
                for mark in marks
                if mark.beat < end
            ]
            if kept:
                layers[name] = kept
        return Marks(layers)


# The rules of marks live in these functions, used by the constructors (locations such as
# `Mark.beat` and `Marks.chords[1]`) and by `parse_song` (locations such as `marks.chords[1].beat`).


def _mark_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check a mark's fields that are present in `values`."""
    problems: list[str] = []
    if "beat" in values and "end" in values:
        problems += _span_problems(values["beat"], values["end"], where, "beat")
    else:
        problems += [
            f"{_at(where, field)}: must be a finite number, got {values[field]!r}"
            for field in ("beat", "end")
            if field in values and not is_number(values[field])
        ]
        beat = values.get("beat")
        if is_number(beat) and beat < 0:
            problems.append(f"{_at(where, 'beat')}: must not be negative, got {beat!r}")
    if "text" in values:
        problems += _line_problems(values["text"], _at(where, "text"))
    return problems


def _layer_name_problems(name: object, where: str) -> list[str]:
    """Check a layer's name, at `<where>.<name>`."""
    if isinstance(name, str) and LAYER_PATTERN.fullmatch(name):
        return []
    got = "" if isinstance(name, str) else f", got {name!r}; quote it in YAML"
    plain = isinstance(name, str) and name.isprintable() and not any(c.isspace() for c in name)
    shown = name if plain and name else repr(name)
    return [
        (
            f"{where}.{shown}: layer names are lowercase letters, digits, _ and -, starting with "
            f"a letter or digit{got}"
        )
    ]


def _layer_list_problems(marks: object, where: str) -> list[str]:
    """Check that a layer is a non-empty list, not the marks themselves."""
    if not isinstance(marks, list | tuple) or isinstance(marks, str):
        return [f"{where}: must be a list of marks"]
    if not marks:
        return [f"{where}: must list at least one mark"]
    return []


def _layer_order_problems(marks: Sequence[tuple[int, Mark]], where: str) -> list[str]:
    """Check that a layer's valid marks, given with their indexes, are in order without overlap.

    Touching is allowed: a mark may start where the previous one ends.
    """
    return [
        (
            f"{where}[{index}]: must start at or after the previous mark's end, beat "
            f"{_plain(previous.end)}, got beat {_plain(mark.beat)}"
        )
        for (_, previous), (index, mark) in pairwise(marks)
        if mark.beat < previous.end
    ]


def _marks_problems(pairs: Sequence[tuple[object, object]], where: str) -> list[str]:
    """Check layers built in code, whose marks are objects."""
    problems: list[str] = []
    seen: set[object] = set()
    for name, marks in pairs:
        at = f"{where}.{name}"
        problems += _layer_name_problems(name, where)
        if name in seen:
            problems.append(f"{where}: duplicate layer name {name!r}")
        seen.add(name)
        layer_problems = _layer_list_problems(marks, at)
        problems += layer_problems
        if layer_problems:
            continue
        items = cast("Sequence[object]", marks)
        problems += [
            f"{at}[{index}]: must be a Mark, got {item!r}"
            for index, item in enumerate(items)
            if not isinstance(item, Mark)
        ]
        problems += _layer_order_problems(
            [(index, item) for index, item in enumerate(items) if isinstance(item, Mark)], at
        )
    return problems


def _marks_field_problems(marks: object, where: str) -> list[str]:
    """Check a song's or graph's `marks` field."""
    if isinstance(marks, Marks):
        return []
    return [f"{where}.marks: must be Marks, got {marks!r}"]


class MarkData(TypedDict):
    """A mark's plain-data form, the song format's own shape for it."""

    beat: float
    end: float
    text: str


type MarksData = dict[str, list[MarkData]]


def _marks_to_data(marks: Marks) -> MarksData:
    """Return the layers as plain data, in authored order, with numbers in one spelling."""
    return {
        name: [
            {"beat": _plain(mark.beat), "end": _plain(mark.end), "text": mark.text}
            for mark in layer
        ]
        for name, layer in marks.pairs
    }


# The empty marks: the default of every song and graph.
NO_MARKS = Marks()
