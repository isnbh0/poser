"""Tests for the music helpers. Pure functions, no plugins or files."""

import ast
import math
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vsti_composer import music
from vsti_composer.music import chord, degree, humanize, notes, pitch, scale, swing
from vsti_composer.song import Note


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
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
        scale(root, mode)


@pytest.mark.parametrize(("index", "value"), [(0, 60), (1, 62), (7, 72), (-1, 59), (-7, 48)])
def test_degree_continues_across_octaves(index: int, value: int) -> None:
    assert degree(scale(60, "major"), index) == value


def test_degree_works_with_five_note_scales() -> None:
    pentatonic = scale(60, "minor_pentatonic")
    assert [degree(pentatonic, i) for i in (0, 4, 5, -1)] == [60, 70, 72, 58]


def test_degree_rejects_empty_scale_and_out_of_range() -> None:
    with pytest.raises(ValueError):
        degree((), 0)
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
        swing(at(0.5), ratio)


def test_swing_rejects_non_positive_grid() -> None:
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
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
    with pytest.raises(ValueError):
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


# Typed as `Any` so the type checker allows the deliberately wrong argument types below.
loose: Any = music


@pytest.mark.parametrize(
    "call",
    [
        lambda: loose.notes([60.5], 0, 1, 80),
        lambda: loose.notes([60], 0, 1, 80.5),
        lambda: loose.notes([60], 0, 1, True),
        lambda: loose.notes([60], True, 1, 80),
        lambda: loose.notes([60], 0, float("nan"), 80),
        lambda: loose.scale(60.5, "major"),
        lambda: loose.scale(True, "major"),
        lambda: loose.chord(60.0, "maj"),
        lambda: loose.chord(60, "maj", 1.0),
        lambda: loose.chord(60, "maj", True),
        lambda: loose.degree(scale(60, "major"), 1.0),
        lambda: loose.swing(at(0.5), 0.6, grid=True),
        lambda: loose.humanize(SIXTEEN, None),
        lambda: loose.humanize(SIXTEEN, 1.5),
        lambda: loose.humanize(SIXTEEN, 1, velocity=2.5),
        lambda: loose.humanize(SIXTEEN, 1, timing=float("nan")),
        lambda: loose.pitch(60),
        lambda: loose.notes([60], 10**400, 1, 80),
    ],
)
def test_helpers_reject_wrong_types_with_value_error(call: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
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
