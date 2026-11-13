"""Tests for the music helpers. Pure functions, no plugins or files."""

import ast
import math
import random
from collections.abc import Callable
from pathlib import Path

import pytest

from poser.core import music
from poser.core.music import (
    chord,
    degree,
    humanize,
    notes,
    pitch,
    scale,
    swing,
    transpose,
    transpose_degrees,
)
from poser.core.song import Note


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("C4", 60),
        ("A4", 69),
        ("c#4", 61),
        ("Db4", 61),
        ("B#3", 60),
        ("Cbb4", 58),
        ("bb4", 70),
        ("C-1", 0),
        ("G9", 127),
    ],
)
def test_pitch_reads_note_names(name: str, value: int) -> None:
    assert pitch(name) == value


@pytest.mark.parametrize("name", ["H4", "C", "C10", "G#9", "Cb-1", "", "C#-2", " C4"])
def test_pitch_rejects_bad_names(name: str) -> None:
    with pytest.raises(ValueError, match=r"^note name "):
        pitch(name)


MODE_STEPS = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "locrian": (0, 1, 3, 5, 6, 8, 10),
    "harmonic_minor": (0, 2, 3, 5, 7, 8, 11),
    "melodic_minor": (0, 2, 3, 5, 7, 9, 11),
    "major_pentatonic": (0, 2, 4, 7, 9),
    "minor_pentatonic": (0, 3, 5, 7, 10),
}


@pytest.mark.parametrize(("mode", "steps"), MODE_STEPS.items())
def test_scale_modes_have_their_steps(mode: str, steps: tuple[int, ...]) -> None:
    assert scale(60, mode) == tuple(60 + step for step in steps)


def test_scale_aliases_match() -> None:
    assert scale(60, "ionian") == scale(60, "major")
    assert scale(60, "aeolian") == scale(60, "minor")


@pytest.mark.parametrize(("root", "mode"), [(60, "blues"), (120, "major")])
def test_scale_rejects_unknown_mode_and_out_of_range(root: int, mode: str) -> None:
    with pytest.raises(ValueError, match=r"unknown mode|outside 0 to 127"):
        scale(root, mode)


@pytest.mark.parametrize(("index", "value"), [(0, 60), (1, 62), (7, 72), (-1, 59), (-7, 48)])
def test_degree_continues_across_octaves(index: int, value: int) -> None:
    assert degree(scale(60, "major"), index) == value


def test_degree_works_with_five_note_scales() -> None:
    pentatonic = scale(60, "minor_pentatonic")
    assert [degree(pentatonic, i) for i in (0, 4, 5, -1)] == [60, 70, 72, 58]


def test_degree_rejects_empty_scale_and_out_of_range() -> None:
    with pytest.raises(ValueError, match="scale is empty"):
        degree((), 0)
    with pytest.raises(ValueError, match="degree 70 reaches pitch 180, outside 0 to 127"):
        degree(scale(60, "major"), 70)


CHORD_INTERVALS = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "min7": (0, 3, 7, 10),
    "m7b5": (0, 3, 6, 10),
    "dim7": (0, 3, 6, 9),
}


@pytest.mark.parametrize(("quality", "intervals"), CHORD_INTERVALS.items())
def test_chord_qualities_have_their_intervals(quality: str, intervals: tuple[int, ...]) -> None:
    assert chord(60, quality) == tuple(60 + interval for interval in intervals)


def test_chord_inversions() -> None:
    assert chord(60, "maj", 1) == (64, 67, 72)
    assert chord(60, "maj", 2) == (67, 72, 76)
    assert chord(60, "maj7", 3) == (71, 72, 76, 79)


@pytest.mark.parametrize(("quality", "inversion"), [("maj", 3), ("maj", -1), ("maj9", 0)], ids=str)
def test_chord_rejects_bad_inversion_and_quality(quality: str, inversion: int) -> None:
    with pytest.raises(ValueError, match=r"must be from 0 to 2 for|unknown chord quality"):
        chord(60, quality, inversion)


def at(*beats: float) -> tuple[Note, ...]:
    return tuple(Note(beat=b, pitch=60 + i, length=0.25, velocity=90) for i, b in enumerate(beats))


def test_swing_moves_off_beats_to_the_ratio() -> None:
    swung = swing(at(0, 0.5, 1, 1.5, 0.25), 2 / 3)
    assert [n.beat for n in swung] == pytest.approx([0, 2 / 3, 1, 5 / 3, 0.25])
    assert [(n.pitch, n.length, n.velocity) for n in swung] == [
        (n.pitch, n.length, n.velocity) for n in at(0, 0.5, 1, 1.5, 0.25)
    ]


def test_swing_catches_off_beats_with_float_error() -> None:
    early, late = swing(at(1.5 - 1e-9, 1.5 + 1e-9), 2 / 3)
    assert (early.beat, late.beat) == pytest.approx((5 / 3, 5 / 3))


def test_swing_half_is_straight() -> None:
    assert swing(at(0, 0.5, 1, 1.5), 0.5) == at(0, 0.5, 1, 1.5)


@pytest.mark.parametrize("ratio", [0.4, 1.0])
def test_swing_rejects_ratio_out_of_range(ratio: float) -> None:
    with pytest.raises(ValueError, match=r"must be at least 0.5 and below 1"):
        swing(at(0.5), ratio)


def test_swing_rejects_non_positive_grid() -> None:
    with pytest.raises(ValueError, match="swing grid 0 must be a positive number of beats"):
        swing(at(0.5), 0.6, grid=0)


def test_swing_uses_the_grid() -> None:
    (moved,) = swing(at(0.25), 0.6, grid=0.25)
    assert moved.beat == pytest.approx(0.6 * 0.5)


SIXTEEN = tuple(Note(beat=i * 0.5, pitch=60, length=0.5, velocity=64) for i in range(16))


def test_humanize_is_repeatable_per_seed() -> None:
    assert humanize(SIXTEEN, 7, velocity=10, timing=0.05) == humanize(
        SIXTEEN, 7, velocity=10, timing=0.05
    )
    assert humanize(SIXTEEN, 7, velocity=10, timing=0.05) != humanize(
        SIXTEEN, 8, velocity=10, timing=0.05
    )


def test_humanize_stays_within_bounds() -> None:
    for before, after in zip(SIXTEEN, humanize(SIXTEEN, 3, velocity=10, timing=0.05), strict=True):
        assert abs(after.velocity - before.velocity) <= 10
        assert abs(after.beat - before.beat) <= 0.05
        assert (after.pitch, after.length) == (before.pitch, before.length)


def test_humanize_clamps_velocity_and_beat() -> None:
    edge = (
        Note(beat=0, pitch=60, length=1, velocity=1),
        Note(beat=0, pitch=60, length=1, velocity=127),
    )
    for seed in range(50):
        for note in humanize(edge, seed, velocity=10, timing=0.1):
            assert 1 <= note.velocity <= 127
            assert note.beat >= 0


def test_humanize_zero_spread_changes_nothing() -> None:
    assert humanize(SIXTEEN, 1) == SIXTEEN


@pytest.mark.parametrize(("velocity", "timing"), [(-1, 0.0), (0, -0.1)])
def test_humanize_rejects_negative_spread(velocity: int, timing: float) -> None:
    with pytest.raises(ValueError, match=r"spread .* must not be negative"):
        humanize(SIXTEEN, 1, velocity=velocity, timing=timing)


def test_notes_without_step_is_a_chord() -> None:
    assert notes(chord(60, "min"), 2, 1, 80) == (
        Note(2, 60, 1, 80),
        Note(2, 63, 1, 80),
        Note(2, 67, 1, 80),
    )


def test_notes_with_step_is_a_line() -> None:
    assert [n.beat for n in notes([60, 62, 64], 0, 0.5, 80, step=0.5)] == [0, 0.5, 1]


@pytest.mark.parametrize(
    ("pitches", "beat", "length", "velocity", "step"),
    [
        ([128], 0, 1, 80, None),
        ([60], -1, 1, 80, None),
        ([60], 0, 0, 80, None),
        ([60], 0, 1, 0, None),
        ([60], 0, 1, 80, -0.5),
    ],
    ids=["pitch", "beat", "length", "velocity", "step"],
)
def test_notes_rejects_bad_values(
    pitches: list[int], beat: float, length: float, velocity: int, step: float | None
) -> None:
    with pytest.raises(
        ValueError,
        match=r"outside 0 to 127|must not be negative|must be positive|must be from 1 to 127",
    ):
        notes(pitches, beat, length, velocity, step)


def test_music_module_imports_no_plugin_code() -> None:
    tree = ast.parse(Path(music.__file__).read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not modules & {"pedalboard", "mido"}


def untyped(function: Callable[..., object]) -> Callable[..., object]:
    """Return the function unchanged, typed so the deliberately wrong arguments below check."""
    return function


@pytest.mark.parametrize(
    "call",
    [
        lambda: untyped(music.notes)([True], 0, 1, 80),
        lambda: untyped(music.notes)([60], 0, 1, 80.5),
        lambda: untyped(music.notes)([60], 0, 1, True),
        lambda: untyped(music.notes)([60], True, 1, 80),
        lambda: untyped(music.notes)([60], 0, float("nan"), 80),
        lambda: untyped(music.scale)(60.5, "major"),
        lambda: untyped(music.scale)(True, "major"),
        lambda: untyped(music.chord)(60.0, "maj"),
        lambda: untyped(music.chord)(60, "maj", 1.0),
        lambda: untyped(music.chord)(60, "maj", True),
        lambda: untyped(music.degree)(scale(60, "major"), 1.0),
        lambda: untyped(music.swing)(at(0.5), 0.6, grid=True),
        lambda: untyped(music.humanize)(SIXTEEN, None),
        lambda: untyped(music.humanize)(SIXTEEN, 1.5),
        lambda: untyped(music.humanize)(SIXTEEN, 1, velocity=2.5),
        lambda: untyped(music.humanize)(SIXTEEN, 1, timing=float("nan")),
        lambda: untyped(music.pitch)(60),
        lambda: untyped(music.notes)([60], 10**400, 1, 80),
    ],
)
def test_helpers_reject_wrong_types_with_value_error(call: Callable[[], object]) -> None:
    with pytest.raises(
        ValueError,
        match=r"not an integer|must be an integer|must be a finite number|^note name 60 is not",
    ):
        call()


def test_humanize_draws_only_from_random_random() -> None:
    # Python keeps `Random.random`'s sequence for a seed stable across versions, unlike `randint`.
    rng = random.Random(5)
    draws = [rng.random() for _ in range(4)]
    expected = []
    for note, (v, t) in zip(SIXTEEN[:2], [draws[0:2], draws[2:4]], strict=True):
        offset = math.floor(v * 21) - 10
        expected.append(
            Note(
                note.beat + 0.05 * (2 * t - 1) if note.beat else max(0.0, 0.05 * (2 * t - 1)),
                note.pitch,
                note.length,
                min(127, max(1, note.velocity + offset)),
            )
        )
    assert humanize(SIXTEEN[:2], 5, velocity=10, timing=0.05) == tuple(expected)


def test_notes_places_fractional_pitches() -> None:
    assert notes([60.5, 64], 0, 1, 100) == (
        Note(beat=0, pitch=60.5, length=1, velocity=100),
        Note(beat=0, pitch=64, length=1, velocity=100),
    )
    with pytest.raises(ValueError, match="outside 0 to 127"):
        notes([127.5], 0, 1, 100)


def test_twelve_tone_helpers_still_take_whole_pitches() -> None:
    with pytest.raises(ValueError, match=r"not an integer|must be an integer"):
        music.scale(60.5, "major")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match=r"not an integer|must be an integer"):
        music.chord(60.5, "maj")  # ty: ignore[invalid-argument-type]


def pitches(moved: tuple[Note, ...]) -> list[float]:
    return [note.pitch for note in moved]


def test_transpose_moves_any_pitch_by_any_interval() -> None:
    melody = notes([60, 60.5, 61], 0, 1, 100, step=1)
    assert pitches(transpose(melody, 7)) == [67, 67.5, 68]
    assert pitches(transpose(melody, -0.25)) == [59.75, 60.25, 60.75]
    assert [(n.beat, n.length, n.velocity) for n in transpose(melody, 7)] == [
        (n.beat, n.length, n.velocity) for n in melody
    ]


def test_transpose_stores_a_whole_result_as_an_int() -> None:
    (moved,) = transpose(notes([60], 0, 1, 100), 7.0)
    assert moved.pitch == 67
    assert isinstance(moved.pitch, int)


def test_transpose_refuses_a_result_out_of_range() -> None:
    melody = notes([60, 120, 125], 0, 1, 100, step=1)
    with pytest.raises(
        ValueError, match=r"^transpose moves the note at beat 1 from pitch 120 to 128"
    ):
        transpose(melody, 8)


C_MAJOR = scale(60, "major")


def test_transpose_degrees_moves_scale_notes_along_the_scale() -> None:
    melody = notes(C_MAJOR, 0, 1, 100, step=1)
    assert pitches(transpose_degrees(melody, C_MAJOR, 4)) == [67, 69, 71, 72, 74, 76, 77]
    assert pitches(transpose_degrees(notes([60], 0, 1, 100), C_MAJOR, -1)) == [59]


@pytest.mark.parametrize("off", [61, 60.5])
def test_transpose_degrees_refuses_off_scale_notes_by_default(off: float) -> None:
    melody = notes([60, off, 64, off], 0, 1, 100, step=1)
    with pytest.raises(
        ValueError, match=rf"^2 notes are off the scale, first at beat 1 with pitch {off};"
    ):
        transpose_degrees(melody, C_MAJOR, 4)


def test_transpose_degrees_moves_off_scale_notes_with_the_note_below() -> None:
    melody = notes([61, 60.5, 70], 0, 1, 100, step=1)
    assert pitches(transpose_degrees(melody, C_MAJOR, 4, off_scale="below")) == [68, 67.5, 77]


def test_transpose_degrees_anchors_below_the_range() -> None:
    # In D major, the scale note below pitch 0 is the continued scale's B at -1, and one step
    # moves that B to C# at 1.
    (moved,) = transpose_degrees(notes([0], 0, 1, 100), scale(62, "major"), 1, off_scale="below")
    assert moved.pitch == 2


def test_transpose_degrees_refuses_a_result_out_of_range() -> None:
    with pytest.raises(ValueError, match=r"^transpose_degrees moves the note at beat 0"):
        transpose_degrees(notes([127], 0, 1, 100), scale(60, "major"), 1)


@pytest.mark.parametrize(
    ("bad", "message"),
    [
        ((), "scale is empty"),
        ((64, 60, 67), "strictly ascending"),
        ((60, 62.5, 64), "not an integer"),
        ((60, 62, 72), "span less than 12"),
        ((60, 64, 75), "span less than 12"),
        ((-2, 0, 2), "outside 0 to 127"),
        ((126, 128), "outside 0 to 127"),
    ],
    ids=["empty", "unsorted", "fractional", "spans-12", "spans-15", "below-0", "above-127"],
)
def test_transpose_degrees_rejects_malformed_scales(bad: tuple[int, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        transpose_degrees(notes([60], 0, 1, 100), bad, 1)
