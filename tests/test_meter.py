"""Meter: spans of bars, the stretches they resolve to for a song's end, and bar numbers."""

import itertools
import math
import random

import pytest

from poser.core.meter import (
    NO_METER,
    SLIVER_BEATS,
    Bar,
    Meter,
    MeterSpan,
    Stretch,
    meter_field_problems,
    meter_to_data,
)
from poser.core.rules import SongError


def problems_of(**values: object) -> list[str]:
    with pytest.raises(SongError) as error:
        MeterSpan(**values)  # ty: ignore[invalid-argument-type]
    return error.value.problems


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"beat": -1, "bar": 4}, "MeterSpan.beat"),
        ({"beat": math.inf, "bar": 4}, "MeterSpan.beat"),
        ({"beat": math.nan, "bar": 4}, "MeterSpan.beat"),
        ({"beat": "0", "bar": 4}, "MeterSpan.beat"),
        ({"beat": True, "bar": 4}, "MeterSpan.beat"),
        ({"beat": -1, "bar": 4, "end": 8}, "MeterSpan.beat"),
        ({"beat": 0, "bar": 0.5}, "MeterSpan.bar"),
        ({"beat": 0, "bar": 0}, "MeterSpan.bar"),
        ({"beat": 0, "bar": math.inf}, "MeterSpan.bar"),
        ({"beat": 0, "bar": math.nan}, "MeterSpan.bar"),
        ({"beat": 0, "bar": None}, "MeterSpan.bar"),
        ({"beat": 4, "bar": 4, "end": 4}, "MeterSpan.end"),
        ({"beat": 4, "bar": 4, "end": 2}, "MeterSpan.end"),
        ({"beat": 0, "bar": 4, "end": math.inf}, "MeterSpan.end"),
        ({"beat": 0, "bar": 4, "end": "8"}, "MeterSpan.end"),
    ],
)
def test_a_span_rejects_a_bad_field_naming_it(values: dict[str, object], field: str) -> None:
    (problem,) = problems_of(**values)
    assert problem.startswith(f"{field}: ")


def test_a_span_lists_every_problem_and_stores_floats() -> None:
    problems = problems_of(beat=-1, bar=0, end=-2)
    assert [problem.split(":")[0] for problem in problems] == [
        "MeterSpan.beat",
        "MeterSpan.end",
        "MeterSpan.bar",
    ]
    span = MeterSpan(0, 7, 84)
    assert (span.beat, span.bar, span.end) == (0.0, 7.0, 84.0)
    assert all(isinstance(value, float) for value in (span.beat, span.bar, span.end))
    assert MeterSpan(2, 3.5).end is None
    assert MeterSpan(0, 1).bar == 1.0


def test_a_meter_rejects_spans_out_of_order_or_overlapping() -> None:
    with pytest.raises(SongError) as out_of_order:
        Meter([MeterSpan(8, 4, 16), MeterSpan(0, 4, 8)])
    assert out_of_order.value.problems == [
        "Meter[1]: must start at or after the previous span's end, beat 16, got beat 0"
    ]
    with pytest.raises(SongError, match=r"Meter\[1\]: must start at or after"):
        Meter([MeterSpan(0, 4, 8), MeterSpan(7.5, 3, 16)])
    for beat in (0, 4):
        with pytest.raises(SongError) as open_span:
            Meter([MeterSpan(4, 4), MeterSpan(beat, 3)])
        assert open_span.value.problems == [
            f"Meter[1]: must start after the previous span's beat, beat 4, got beat {beat}"
        ]


def test_a_meter_accepts_touching_spans_and_lists_every_problem() -> None:
    touching = Meter([MeterSpan(0, 4, 8), MeterSpan(8, 3, 14), MeterSpan(14, 5)])
    assert len(touching.spans) == 3
    assert Meter([MeterSpan(0, 4), MeterSpan(0.5, 3)]).spans[1].beat == 0.5
    with pytest.raises(SongError) as error:
        Meter([MeterSpan(0, 4, 8), "x", MeterSpan(4, 4, 8), MeterSpan(6, 4)])  # ty: ignore[invalid-argument-type]
    assert [problem.split(":")[0] for problem in error.value.problems] == [
        "Meter[1]",
        "Meter[2]",
        "Meter[3]",
    ]
    for bad in ("44", 4, {"beat": 0}):
        with pytest.raises(SongError, match="Meter: must be a sequence"):
            Meter(bad)  # ty: ignore[invalid-argument-type]


def test_an_empty_meter_is_false_and_is_the_default() -> None:
    assert not Meter()
    assert Meter() == NO_METER == Meter([])
    assert Meter([MeterSpan(0, 4)])
    assert Meter([MeterSpan(0, 4)]) == Meter((MeterSpan(0, 4),))
    assert meter_field_problems(NO_METER, "Song") == []
    assert meter_field_problems([MeterSpan(0, 4)], "Song") == [
        f"Song.meter: must be a Meter, got {[MeterSpan(0, 4)]!r}"
    ]


def test_no_meter_is_one_unmetered_stretch() -> None:
    assert Meter().stretches(10) == (Stretch(0.0, 10.0, None),)
    assert Meter().stretches(10)[0].bars() == ()
    assert Meter().bars(10) == ()


def test_one_span_covers_the_song_in_bars() -> None:
    (stretch,) = Meter([MeterSpan(0, 7)]).stretches(245)
    assert stretch == Stretch(0.0, 245.0, 7.0)
    bars = stretch.bars()
    assert len(bars) == 35
    assert bars[0] == (0, 7)
    assert bars[-1] == (238, 245)


def test_a_span_with_no_end_stops_at_the_next_spans_beat() -> None:
    meter = Meter([MeterSpan(0, 7), MeterSpan(84, 4)])
    assert meter.stretches(112) == (Stretch(0.0, 84.0, 7.0), Stretch(84.0, 112.0, 4.0))


def test_beats_no_span_covers_are_unmetered() -> None:
    assert Meter([MeterSpan(8, 4, 16)]).stretches(20) == (
        Stretch(0.0, 8.0, None),
        Stretch(8.0, 16.0, 4.0),
        Stretch(16.0, 20.0, None),
    )


def test_a_span_that_is_not_whole_bars_ends_with_a_short_bar() -> None:
    (stretch,) = Meter([MeterSpan(0, 4, 10)]).stretches(10)
    assert stretch.bars() == ((0, 4), (4, 8), (8, 10))


def test_a_song_ending_inside_a_bar_cuts_its_last_bar() -> None:
    (stretch,) = Meter([MeterSpan(0, 7)]).stretches(244.903)
    bars = stretch.bars()
    assert len(bars) == 35
    assert bars[-1] == (238, 244.903)


def test_a_span_at_or_after_the_end_is_dropped_and_one_crossing_it_is_cut() -> None:
    meter = Meter([MeterSpan(0, 4, 8), MeterSpan(8, 3, 20), MeterSpan(20, 5)])
    assert meter.stretches(10) == (Stretch(0.0, 8.0, 4.0), Stretch(8.0, 10.0, 3.0))
    assert meter.stretches(8) == (Stretch(0.0, 8.0, 4.0),)
    assert meter.stretches(6) == (Stretch(0.0, 6.0, 4.0),)
    assert Meter([MeterSpan(12, 4)]).stretches(12) == (Stretch(0.0, 12.0, None),)


def test_equal_adjacent_stretches_are_not_merged() -> None:
    meter = Meter([MeterSpan(0, 4, 6), MeterSpan(6, 4)])
    assert meter.stretches(14) == (Stretch(0.0, 6.0, 4.0), Stretch(6.0, 14.0, 4.0))
    assert [(bar.beat, bar.end) for bar in meter.bars(14)] == [(0, 4), (4, 6), (6, 10), (10, 14)]


@pytest.mark.parametrize("end", [-1, -0.5, math.inf, math.nan, "4", None, True])
def test_stretches_need_a_finite_end_of_0_or_more(end: object) -> None:
    for resolve in (Meter([MeterSpan(0, 4)]).stretches, NO_METER.bars):
        with pytest.raises(ValueError, match="finite end of 0 or more"):
            resolve(end)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("meter", [NO_METER, Meter([MeterSpan(0, 4)]), Meter([MeterSpan(2, 3)])])
def test_a_song_ending_at_beat_0_has_no_stretches_and_no_bars(meter: Meter) -> None:
    assert meter.stretches(0) == ()
    assert meter.bars(0) == ()


def eighths(rng: random.Random, low: int, high: int) -> float:
    """A multiple of 1/8 from `low` to `high` eighths, so sums and products of them are exact."""
    return rng.randint(low, high) / 8


def random_meter(rng: random.Random) -> Meter:
    spans: list[MeterSpan] = []
    beat = eighths(rng, 0, 40)
    for _ in range(rng.randint(0, 5)):
        bar = eighths(rng, 8, 72)
        if rng.random() < 0.5:
            spans.append(MeterSpan(beat, bar))
            beat += eighths(rng, 1, 400)
        else:
            end = beat + eighths(rng, 1, 400)
            spans.append(MeterSpan(beat, bar, end))
            beat = end + rng.choice([0, eighths(rng, 0, 80)])
    return Meter(spans)


@pytest.mark.parametrize("seed", range(300))
def test_stretches_and_bars_cover_the_song(seed: int) -> None:
    rng = random.Random(seed)
    meter = random_meter(rng)
    end = eighths(rng, 1, 2400)

    stretches = meter.stretches(end)

    assert stretches[0].start == 0
    assert stretches[-1].end == end
    for previous, stretch in itertools.pairwise(stretches):
        assert previous.end == stretch.start
    numbered: list[tuple[float, float]] = []
    lines: list[float | None] = []
    for stretch in stretches:
        assert stretch.start < stretch.end
        bars = stretch.bars()
        if stretch.bar is None:
            assert bars == ()
            continue
        assert bars[0][0] == stretch.start
        assert bars[-1][1] == stretch.end
        for (_, stop), (start, _) in itertools.pairwise(bars):
            assert stop == start
        # Every bar but the last is whole.
        assert all(stop - start == stretch.bar for start, stop in bars[:-1])
        # The last is short, whole, or longer by a sliver; only a lone bar is a sliver itself.
        length = bars[-1][1] - bars[-1][0]
        assert 0 < length < stretch.bar + SLIVER_BEATS
        assert length >= SLIVER_BEATS or len(bars) == 1
        numbered += bars
        # Only a last bar a sliver made longer carries the bar line it runs past.
        joined = length > stretch.bar
        lines += [None] * (len(bars) - 1) + [bars[-1][0] + stretch.bar if joined else None]

    bars = meter.bars(end)
    assert [(bar.beat, bar.end) for bar in bars] == numbered
    assert [bar.number for bar in bars] == list(range(1, len(numbered) + 1))
    for bar in bars:
        assert meter.beat_of(bar.number) == bar.beat
    assert [bar.line for bar in bars] == lines


def test_a_sliver_past_the_last_bar_line_joins_the_bar_before_it() -> None:
    """Humanized last notes ring to beat 176.0036 in a song of 44 four-beat bars."""
    bars = Meter([MeterSpan(0, 4)]).bars(176.0036)
    assert len(bars) == 44
    assert bars[-2] == Bar(43, 168.0, 172.0)
    assert bars[-1] == Bar(44, 172.0, 176.0036, line=176.0)


@pytest.mark.parametrize(
    ("end", "last"),
    [
        (8 + SLIVER_BEATS / 2, (4, 8 + SLIVER_BEATS / 2)),
        (8 + SLIVER_BEATS, (8, 8 + SLIVER_BEATS)),
        (9, (8, 9)),
    ],
)
def test_only_a_remainder_under_a_sliver_joins(end: float, last: tuple[float, float]) -> None:
    (stretch,) = Meter([MeterSpan(0, 4)]).stretches(end)
    assert stretch.bars()[-1] == last


def test_a_sliver_with_no_bar_before_it_is_a_bar() -> None:
    assert Meter([MeterSpan(0, 4)]).bars(0.1) == (Bar(1, 0.0, 0.1),)
    after_a_change = Meter([MeterSpan(0, 4), MeterSpan(8, 3)]).bars(8.1)
    assert after_a_change[-1] == Bar(3, 8.0, 8.1)


def test_a_bar_longer_only_by_float_rounding_has_no_line() -> None:
    bars = Meter([MeterSpan(0, 1.1)]).bars(11)
    assert len(bars) == 10
    assert all(bar.line is None for bar in bars)


def test_beat_of_counts_a_span_whose_end_leaves_a_sliver_as_bars_does() -> None:
    meter = Meter([MeterSpan(0, 4, 16.1), MeterSpan(16.1, 3)])
    bars = meter.bars(22.1)
    assert [(bar.number, bar.beat, bar.end) for bar in bars] == [
        (1, 0, 4),
        (2, 4, 8),
        (3, 8, 12),
        (4, 12, 16.1),
        (5, 16.1, 19.1),
        (6, 19.1, 22.1),
    ]
    assert [meter.beat_of(bar.number) for bar in bars] == [bar.beat for bar in bars]


def test_a_fractional_bar_length_divides_exactly() -> None:
    (stretch,) = Meter([MeterSpan(0, 3.5)]).stretches(35)
    bars = stretch.bars()
    assert len(bars) == 10
    assert [start for start, _ in bars] == [index * 3.5 for index in range(10)]
    assert bars[-1][1] == 35


def test_bars_are_numbered_across_a_gap_and_a_change_of_meter() -> None:
    gapped = Meter([MeterSpan(0, 4, 8), MeterSpan(12, 4)])
    assert gapped.bars(16) == (Bar(1, 0.0, 4.0), Bar(2, 4.0, 8.0), Bar(3, 12.0, 16.0))
    changed = Meter([MeterSpan(0, 7), MeterSpan(14, 4)])
    assert changed.bars(18) == (Bar(1, 0.0, 7.0), Bar(2, 7.0, 14.0), Bar(3, 14.0, 18.0))
    # A short bar is numbered like any other.
    short = Meter([MeterSpan(0, 4, 6), MeterSpan(6, 3)])
    assert short.bars(9) == (Bar(1, 0.0, 4.0), Bar(2, 4.0, 6.0), Bar(3, 6.0, 9.0))


def test_beat_of_gives_the_beat_a_bar_starts_on() -> None:
    meter = Meter([MeterSpan(0, 7), MeterSpan(84, 4)])
    assert meter.beat_of(1) == 0
    assert meter.beat_of(12) == 77
    assert meter.beat_of(13) == 84
    assert meter.beat_of(17) == 100
    # The last span has no end, so its bars go on past any song's end.
    assert [bar.number for bar in meter.bars(100)][-1] == 16
    assert meter.beat_of(1000) == 84 + (1000 - 13) * 4
    assert Meter([MeterSpan(8, 3.5)]).beat_of(3) == 15


def test_beat_of_refuses_bars_that_do_not_exist() -> None:
    closed = Meter([MeterSpan(0, 4, 10), MeterSpan(12, 3, 18)])
    assert [closed.beat_of(number) for number in range(1, 6)] == [0, 4, 8, 12, 15]
    with pytest.raises(ValueError, match="the meter has 5 bars, got bar 6"):
        closed.beat_of(6)
    for number in (0, -1, 1.0, True, "1"):
        with pytest.raises(ValueError, match="bar number of at least 1"):
            closed.beat_of(number)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="has none"):
        NO_METER.beat_of(1)


def test_meter_data_states_an_end_only_where_the_span_does() -> None:
    meter = Meter([MeterSpan(0, 7, 84), MeterSpan(84, 3.5)])
    data = meter_to_data(meter)
    assert data == [{"beat": 0, "end": 84, "bar": 7}, {"beat": 84, "bar": 3.5}]
    assert [list(span) for span in data] == [["beat", "end", "bar"], ["beat", "bar"]]
    assert isinstance(data[0]["bar"], int)
    assert meter_to_data(NO_METER) == []
