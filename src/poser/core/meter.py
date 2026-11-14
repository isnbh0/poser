"""Meter: the bars a song declares, as spans of beats that each give the length of one bar.

Spans are in order and do not overlap; beats no span covers have no meter. Meter never changes
audio. Every meter object is valid: its constructor raises `SongError` listing every problem.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import NotRequired, TypedDict, cast

from poser.core.rules import SongError, field_path, is_int, is_number, plain_number, span_problems


@dataclass(frozen=True)
class MeterSpan:
    """Bars of `bar` beats each, counted from `beat`, up to `end`.

    A span without `end` lasts until the next span's `beat`, or the song's end.
    """

    beat: float
    bar: float
    end: float | None = None

    def __post_init__(self) -> None:
        values = {"beat": self.beat, "bar": self.bar, "end": self.end}
        if problems := meter_span_problems(values, "MeterSpan"):
            raise SongError(problems)
        object.__setattr__(self, "beat", float(self.beat))
        object.__setattr__(self, "bar", float(self.bar))
        if self.end is not None:
            object.__setattr__(self, "end", float(self.end))


def _bar_count(start: float, end: float, bar: float) -> int:
    """Return how many bars of `bar` beats, counted from `start`, begin before `end`."""
    count = max(1, math.ceil((end - start) / bar))
    # Division can round across a bar's start, which multiplication places.
    if count > 1 and start + (count - 1) * bar >= end:
        count -= 1
    elif start + count * bar < end:
        count += 1
    return count


@dataclass(frozen=True)
class Stretch:
    """The beats from `start` to `end`, in bars of `bar` beats, or unmetered when `bar` is None."""

    start: float
    end: float
    bar: float | None

    def bars(self) -> tuple[tuple[float, float], ...]:
        """Return the `(start, end)` beats of each bar, or nothing for an unmetered stretch.

        The last bar ends at the stretch's end, so it is short when the stretch is not a whole
        number of bars.
        """
        if self.bar is None:
            return ()
        count = _bar_count(self.start, self.end, self.bar)
        starts = [self.start + index * self.bar for index in range(count)]
        return tuple(pairwise([*starts, self.end]))


@dataclass(frozen=True)
class Bar:
    """One bar: its number, counted from 1 over the song's metered stretches, and its beats."""

    number: int
    beat: float
    end: float


@dataclass(frozen=True, init=False)
class Meter:
    """A song's meter spans, in song order. A meter with no spans declares no meter."""

    spans: tuple[MeterSpan, ...]

    def __init__(self, spans: Sequence[MeterSpan] = ()) -> None:
        if not isinstance(spans, Sequence) or isinstance(spans, str):
            raise SongError([f"Meter: must be a sequence of meter spans, got {spans!r}"])
        items = list(cast("Sequence[object]", spans))
        problems = [
            f"Meter[{index}]: must be a MeterSpan, got {item!r}"
            for index, item in enumerate(items)
            if not isinstance(item, MeterSpan)
        ]
        valid = [(index, item) for index, item in enumerate(items) if isinstance(item, MeterSpan)]
        problems += meter_order_problems(valid, "Meter")
        if problems:
            raise SongError(problems)
        object.__setattr__(self, "spans", tuple(span for _, span in valid))

    def __bool__(self) -> bool:
        return bool(self.spans)

    def _stops(self) -> list[float | None]:
        """Each span's stop: its `end`, else the next span's `beat`, else None for no limit."""
        following: list[float | None] = [span.beat for span in self.spans[1:]]
        return [
            span.end if span.end is not None else next_beat
            for span, next_beat in zip(self.spans, [*following, None], strict=False)
        ]

    def stretches(self, end: float) -> tuple[Stretch, ...]:
        """Return the stretches covering the beats from 0 to `end`, a song's last beat.

        Each starts where the previous one ended. A span is cut at `end`, a span starting at or
        after `end` is dropped, and beats no span covers form unmetered stretches. Stretches are
        never merged, since bars count from each span's own `beat`.
        """
        if not (is_number(end) and end > 0):
            raise ValueError(f"Meter.stretches needs a positive finite end, got {end!r}")
        last = float(end)
        stretches: list[Stretch] = []
        position = 0.0
        for span, stop in zip(self.spans, self._stops(), strict=True):
            if span.beat >= last:
                break
            if span.beat > position:
                stretches.append(Stretch(position, span.beat, None))
            position = last if stop is None else min(stop, last)
            stretches.append(Stretch(span.beat, position, span.bar))
        if position < last:
            stretches.append(Stretch(position, last, None))
        return tuple(stretches)

    def bars(self, end: float) -> tuple[Bar, ...]:
        """Return every bar of a song ending at beat `end`, numbered from 1 in song order.

        Unmetered stretches have no bars and do not advance the number.
        """
        beats = [bar for stretch in self.stretches(end) for bar in stretch.bars()]
        return tuple(Bar(number, start, stop) for number, (start, stop) in enumerate(beats, 1))

    def beat_of(self, number: int) -> float:
        """Return the beat on which bar `number` starts, numbered as `bars` numbers them.

        A last span with no `end` has bars without limit.
        """
        if not is_int(number) or number < 1:
            raise ValueError(f"Meter.beat_of needs a bar number of at least 1, got {number!r}")
        if not self.spans:
            raise ValueError("Meter.beat_of needs a meter with spans; this one has none")
        passed = 0
        for span, stop in zip(self.spans, self._stops(), strict=True):
            count = None if stop is None else _bar_count(span.beat, stop, span.bar)
            if count is None or number <= passed + count:
                return span.beat + (number - 1 - passed) * span.bar
            passed += count
        raise ValueError(f"Meter.beat_of: the meter has {passed} bars, got bar {number!r}")


# The rules of meter live in these functions, used by the constructors (locations such as
# `MeterSpan.bar` and `Meter[1]`) and by `parse_song` (locations such as `meter[1].bar`).


def meter_span_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check a meter span's fields that are present in `values`. An `end` of None states none."""
    problems: list[str] = []
    beat = values.get("beat")
    if "beat" in values and values.get("end") is not None:
        problems += span_problems(beat, values["end"], where, "beat")
    else:
        if "beat" in values and not is_number(beat):
            problems.append(f"{field_path(where, 'beat')}: must be a finite number, got {beat!r}")
        elif is_number(beat) and beat < 0:
            problems.append(f"{field_path(where, 'beat')}: must not be negative, got {beat!r}")
        end = values.get("end")
        if end is not None and not is_number(end):
            problems.append(f"{field_path(where, 'end')}: must be a finite number, got {end!r}")
    if "bar" in values:
        bar = values["bar"]
        if not (is_number(bar) and bar >= 1):
            problems.append(
                f"{field_path(where, 'bar')}: must be a finite number of at least 1, got {bar!r}"
            )
    return problems


def meter_order_problems(spans: Sequence[tuple[int, MeterSpan]], where: str) -> list[str]:
    """Check that valid spans, given with their indexes, are in order without overlap.

    Touching is allowed: a span may start where the previous one ends. A span after one with no
    `end` starts after that one's `beat`.
    """
    problems: list[str] = []
    for (_, previous), (index, span) in pairwise(spans):
        if previous.end is None:
            if span.beat <= previous.beat:
                problems.append(
                    f"{where}[{index}]: must start after the previous span's beat, beat "
                    f"{plain_number(previous.beat)}, got beat {plain_number(span.beat)}"
                )
        elif span.beat < previous.end:
            problems.append(
                f"{where}[{index}]: must start at or after the previous span's end, beat "
                f"{plain_number(previous.end)}, got beat {plain_number(span.beat)}"
            )
    return problems


def meter_field_problems(meter: object, where: str) -> list[str]:
    """Check a song's or graph's `meter` field."""
    if isinstance(meter, Meter):
        return []
    return [f"{where}.meter: must be a Meter, got {meter!r}"]


class MeterSpanData(TypedDict):
    """A meter span's plain-data form, the song format's own shape for it."""

    beat: float
    end: NotRequired[float]
    bar: float


type MeterData = list[MeterSpanData]


def meter_to_data(meter: Meter) -> MeterData:
    """Return the spans as plain data, in song order, with numbers in one spelling."""
    return [_span_to_data(span) for span in meter.spans]


def _span_to_data(span: MeterSpan) -> MeterSpanData:
    data: MeterSpanData = {"beat": plain_number(span.beat), "bar": plain_number(span.bar)}
    if span.end is not None:
        data = {
            "beat": plain_number(span.beat),
            "end": plain_number(span.end),
            "bar": plain_number(span.bar),
        }
    return data


# The empty meter: the default of every song and graph.
NO_METER = Meter()
