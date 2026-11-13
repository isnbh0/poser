"""Lens laws: writes and readings held together over seeded generated songs.

Transposing a song by `k` semitones shifts every `tonal` reading by `k`, exactly. Every
generated pitch is exact in binary floating point, so `pitch + k` is exact for an integer `k`.
The laws are established for these seeds, windows, and shifts; they show that `tonal` is
rotation-equivariant, not that its readings are musically right.
"""

from collections import Counter
from dataclasses import replace
from functools import cache

import pytest
from generated_songs import random_song

from poser.adapters.tonal import TonalKey, read_keys
from poser.core.music import scale, transpose, transpose_degrees
from poser.core.reading import Ambiguous, Known, Outcome, Reading, ScoreRequest, Unknowable
from poser.core.song import Note, Song

SEEDS = range(300)
WINDOWS = (16.0, 7.5)
SHIFTS = range(-12, 13)
ROUND_TRIP_INTERVALS = (-12, -0.5, 0.25, 7)
DEGREE_STEPS = range(-8, 9)
DEGREE_MODES = ("major", "minor", "dorian")

type Shifted = tuple[str, object]


def shifted(outcome: Outcome[TonalKey], k: int) -> Shifted:
    """The outcome with every tonic moved by `k`, in a form compared with `==` and no tolerance.

    Ambiguous candidates become a set, because the tonic breaks ties in their order.
    """
    match outcome:
        case Known(value=key):
            return ("known", replace(key, tonic=(key.tonic + k) % 12))
        case Ambiguous(candidates=candidates):
            return (
                "ambiguous",
                frozenset(
                    ((key.tonic + k) % 12, key.mode, key.correlation, key.deviation_cents)
                    for key in candidates
                ),
            )
        case Unknowable(reason=reason):
            return ("unknowable", reason)


def transposed(song: Song, k: float) -> Song:
    return replace(
        song,
        tracks=tuple(replace(track, notes=transpose(track.notes, k)) for track in song.tracks),
    )


def read(song: Song, window: float) -> Reading[TonalKey]:
    return read_keys(ScoreRequest.default(song), window)


@cache
def base_reading(seed: int, window: float) -> Reading[TonalKey]:
    return read(random_song(seed), window)


@pytest.mark.parametrize("window", WINDOWS)
@pytest.mark.parametrize("seed", SEEDS)
def test_transposing_shifts_every_tonal_reading(seed: int, window: float) -> None:
    song = random_song(seed)
    base = base_reading(seed, window)
    bounds = [(span.start, span.end) for span in base.spans]
    for k in SHIFTS:
        moved = read(transposed(song, k), window)
        assert [(span.start, span.end) for span in moved.spans] == bounds, (seed, window, k)
        expected = [shifted(span.outcome, k) for span in base.spans]
        actual = [shifted(span.outcome, 0) for span in moved.spans]
        assert actual == expected, (seed, window, k)


def test_the_law_is_not_vacuous() -> None:
    kinds: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for seed in SEEDS:
        for window in WINDOWS:
            for span in base_reading(seed, window).spans:
                kinds[type(span.outcome).__name__] += 1
                if isinstance(span.outcome, Unknowable):
                    reasons[span.outcome.reason] += 1
    assert kinds["Known"] > 0
    assert kinds["Ambiguous"] > 0
    assert len(reasons) >= 3, reasons


@pytest.mark.parametrize("seed", SEEDS)
def test_transpose_round_trips(seed: int) -> None:
    for track in random_song(seed).tracks:
        for interval in ROUND_TRIP_INTERVALS:
            assert transpose(transpose(track.notes, interval), -interval) == track.notes


@cache
def generated_notes() -> tuple[Note, ...]:
    return tuple(
        note for seed in SEEDS for track in random_song(seed).tracks for note in track.notes
    )


def move_in_range(
    notes: tuple[Note, ...], steps_scale: tuple[int, ...], steps: int
) -> tuple[tuple[Note, ...], tuple[Note, ...]]:
    """The notes whose move by `steps` stays in 0 to 127, and those notes moved."""
    try:
        return notes, transpose_degrees(notes, steps_scale, steps)
    except ValueError:
        if len(notes) == 1:
            return (), ()
        half = len(notes) // 2
        low, low_moved = move_in_range(notes[:half], steps_scale, steps)
        high, high_moved = move_in_range(notes[half:], steps_scale, steps)
        return low + high, low_moved + high_moved


@pytest.mark.parametrize("mode", DEGREE_MODES)
@pytest.mark.parametrize("root", range(60, 72))
def test_degree_moves_round_trip_and_stay_on_the_scale(root: int, mode: str) -> None:
    steps_scale = scale(root, mode)
    classes = {value % 12 for value in steps_scale}
    on_scale = tuple(
        n for n in generated_notes() if isinstance(n.pitch, int) and n.pitch % 12 in classes
    )
    assert on_scale
    for steps in DEGREE_STEPS:
        movable, moved = move_in_range(on_scale, steps_scale, steps)
        assert movable
        assert all(isinstance(n.pitch, int) and n.pitch % 12 in classes for n in moved)
        assert transpose_degrees(moved, steps_scale, -steps) == movable
