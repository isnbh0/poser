"""Music helpers: pure functions that turn musical ideas into pitches and `Note` values.

Nothing here touches plugins, MIDI messages, or files, so every result is an ordinary `Note` that
`render-song` already handles. Invalid input raises `ValueError` naming the bad value. Pitches use
scientific pitch notation, where middle C is `C4` = 60 and `A4` = 69.
"""

import bisect
import itertools
import math
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Literal

from poser.core.song import Note, is_int, is_number

NOTE_NAME = re.compile(r"([A-Ga-g])([#b]*)(-1|[0-9])")
LETTER_SEMITONES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

MODES: dict[str, tuple[int, ...]] = {
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
MODES["ionian"] = MODES["major"]
MODES["aeolian"] = MODES["minor"]

CHORDS: dict[str, tuple[int, ...]] = {
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

# How close, in beats, a note must start to an off-beat grid step for `swing` to move it.
SWING_TOLERANCE = 1e-6


def _require_int(value: object, what: str) -> int:
    if not is_int(value):
        raise ValueError(f"{what} {value!r} must be an integer")
    return value


def _require_number(value: object, what: str) -> float:
    if not is_number(value):
        raise ValueError(f"{what} {value!r} must be a finite number")
    return value


def _check_pitches(pitches: Iterable[object], what: str) -> tuple[int, ...]:
    checked = []
    for value in pitches:
        if not is_int(value):
            raise ValueError(f"{what} has pitch {value!r}, which is not an integer")
        if not 0 <= value <= 127:
            raise ValueError(f"{what} reaches pitch {value}, outside 0 to 127")
        checked.append(value)
    return tuple(checked)


def _check_pitch_numbers(pitches: Iterable[object], what: str) -> tuple[float, ...]:
    """Check pitches that may be any number on the MIDI scale, such as 60.5 for a quarter tone."""
    checked = []
    for value in pitches:
        if not is_number(value):
            raise ValueError(f"{what} has pitch {value!r}, which must be a finite number")
        if not 0 <= value <= 127:
            raise ValueError(f"{what} reaches pitch {value}, outside 0 to 127")
        checked.append(value)
    return tuple(checked)


def pitch(name: str) -> int:
    """Read a note name such as `C4`, `c#4`, `Db4`, or `Cbb4` as a MIDI pitch."""
    match = NOTE_NAME.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        raise ValueError(f"note name {name!r} is not a letter A-G, accidentals, and octave -1 to 9")
    letter, accidentals, octave = match.groups()
    value = (
        (int(octave) + 1) * 12
        + LETTER_SEMITONES[letter.upper()]
        + accidentals.count("#")
        - accidentals.count("b")
    )
    if not 0 <= value <= 127:
        raise ValueError(f"note name {name!r} is pitch {value}, outside 0 to 127")
    return value


def scale(root: int, mode: str) -> tuple[int, ...]:
    """Return one octave of `mode` from `root`, ascending, without the octave repeat."""
    _check_pitches([root], "scale root")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; known modes are {', '.join(MODES)}")
    return _check_pitches([root + step for step in MODES[mode]], f"{mode} scale from {root}")


def degree(scale: Sequence[int], index: int) -> int:
    """Return the scale's `index`-th note from 0, continuing into higher and lower octaves."""
    if not scale:
        raise ValueError("scale is empty")
    _check_pitches(scale, "scale")
    octave, position = divmod(_require_int(index, "degree index"), len(scale))
    return _check_pitches([scale[position] + 12 * octave], f"degree {index}")[0]


def chord(root: int, quality: str, inversion: int = 0) -> tuple[int, ...]:
    """Return a chord's pitches ascending. Inversion `n` moves the lowest `n` notes up an octave."""
    _check_pitches([root], "chord root")
    if quality not in CHORDS:
        raise ValueError(f"unknown chord quality {quality!r}; known are {', '.join(CHORDS)}")
    intervals = CHORDS[quality]
    if not 0 <= _require_int(inversion, "inversion") < len(intervals):
        raise ValueError(
            f"inversion {inversion} must be from 0 to {len(intervals) - 1} for {quality!r}"
        )
    pitches = [root + interval for interval in intervals]
    pitches = pitches[inversion:] + [p + 12 for p in pitches[:inversion]]
    return _check_pitches(pitches, f"{quality} chord from {root}")


def swing(notes: Iterable[Note], ratio: float, grid: float = 0.5) -> tuple[Note, ...]:
    """Move notes on the second step of each pair of grid steps to `ratio` of the pair.

    A ratio of 0.5 is straight and 2/3 is a triplet feel. Other notes are unchanged.
    """
    if not 0.5 <= _require_number(ratio, "swing ratio") < 1:
        raise ValueError(f"swing ratio {ratio} must be at least 0.5 and below 1")
    if not _require_number(grid, "swing grid") > 0:
        raise ValueError(f"swing grid {grid} must be a positive number of beats")
    pair = 2 * grid
    swung = []
    for note in notes:
        start = math.floor(note.beat / pair) * pair
        off_beat = abs(note.beat - start - grid) < SWING_TOLERANCE
        swung.append(replace(note, beat=start + ratio * pair) if off_beat else note)
    return tuple(swung)


def humanize(
    notes: Iterable[Note], seed: int, velocity: int = 0, timing: float = 0.0
) -> tuple[Note, ...]:
    """Add seeded uniform offsets of up to `velocity` steps and `timing` beats to each note.

    Each note draws its velocity offset, then its timing offset, in input order, so a seed always
    gives the same result. Both come from `Random.random`, whose sequence for a seed Python keeps
    stable across versions. Velocities clamp to 1 to 127 and beats to 0 or later.
    """
    rng = random.Random(_require_int(seed, "humanize seed"))
    if _require_int(velocity, "velocity spread") < 0:
        raise ValueError(f"velocity spread {velocity} must not be negative")
    if _require_number(timing, "timing spread") < 0:
        raise ValueError(f"timing spread {timing} must not be negative")
    result = []
    for note in notes:
        offset = math.floor(rng.random() * (2 * velocity + 1)) - velocity
        new_velocity = min(127, max(1, note.velocity + offset))
        new_beat = max(0.0, note.beat + timing * (2 * rng.random() - 1))
        result.append(replace(note, beat=new_beat, velocity=new_velocity))
    return tuple(result)


def notes(
    pitches: Iterable[float],
    beat: float,
    length: float,
    velocity: int,
    step: float | None = None,
) -> tuple[Note, ...]:
    """Place pitches from `beat`: together as a chord, or `step` beats apart as a line.

    A pitch is any number from 0 to 127, such as 60.5 for a quarter tone above middle C.
    """
    if _require_number(beat, "beat") < 0:
        raise ValueError(f"beat {beat} must not be negative")
    if _require_number(length, "length") <= 0:
        raise ValueError(f"length {length} must be positive")
    if not 1 <= _require_int(velocity, "velocity") <= 127:
        raise ValueError(f"velocity {velocity} must be from 1 to 127")
    if step is not None and _require_number(step, "step") < 0:
        raise ValueError(f"step {step} must not be negative")
    placed = _check_pitch_numbers(list(pitches), "notes")
    return tuple(
        Note(beat=beat + (step or 0) * index, pitch=value, length=length, velocity=velocity)
        for index, value in enumerate(placed)
    )


def _given_notes(notes: Iterable[Note], helper: str) -> tuple[Note, ...]:
    given = tuple(notes)
    for item in given:
        if not isinstance(item, Note):
            raise ValueError(
                f"{helper} takes notes, such as notes([60, 64], 0, 1, 100); got {item!r}"
            )
    return given


def _moved(notes: Sequence[Note], results: Sequence[float], helper: str) -> tuple[Note, ...]:
    """Return the notes at their new pitches, or raise naming the first result outside 0 to 127."""
    for note, result in zip(notes, results, strict=True):
        if not 0 <= result <= 127:
            raise ValueError(
                f"{helper} moves the note at beat {note.beat} from pitch {note.pitch} to "
                f"{result}, outside 0 to 127"
            )
    return tuple(replace(note, pitch=result) for note, result in zip(notes, results, strict=True))


def transpose(notes: Iterable[Note], interval: float) -> tuple[Note, ...]:
    """Move every pitch by `interval` semitones, any finite number, such as 7 or -0.25.

    Every frequency is multiplied by 2 ** (interval / 12), so this works on any pitch.
    Beats, lengths, and velocities are unchanged.
    """
    _require_number(interval, "transpose interval")
    given = _given_notes(notes, "transpose")
    return _moved(given, [note.pitch + interval for note in given], "transpose")


def _check_scale(scale: Sequence[int]) -> tuple[int, ...]:
    if isinstance(scale, str) or not isinstance(scale, Sequence):
        raise ValueError(f"scale {scale!r} must be a sequence of integer pitches")
    checked = tuple(scale)
    if not checked:
        raise ValueError("scale is empty")
    _check_pitches(checked, "scale")
    if any(later <= earlier for earlier, later in itertools.pairwise(checked)):
        raise ValueError(f"scale {list(checked)} must be strictly ascending")
    if checked[-1] - checked[0] >= 12:
        raise ValueError(f"scale {list(checked)} must span less than 12, one octave of a mode")
    return checked


def transpose_degrees(
    notes: Iterable[Note],
    scale: Sequence[int],
    steps: int,
    *,
    off_scale: Literal["refuse", "below"] = "refuse",
) -> tuple[Note, ...]:
    """Move every note `steps` positions along `scale`, continued through every octave.

    `scale` is one octave of a mode, as `scale()` returns it. A note is on the scale when its pitch
    is an integer whose pitch class is in the scale. Off-scale notes, including every fractional
    pitch, raise unless `off_scale="below"`, which moves each by the same amount as the highest
    scale note at or below it, so a chromatic note moves as a sharpened scale note.
    """
    steps_in_octave = _check_scale(scale)
    _require_int(steps, "transpose_degrees steps")
    if off_scale not in ("refuse", "below"):
        raise ValueError(f'off_scale {off_scale!r} must be "refuse" or "below"')
    root = steps_in_octave[0]
    offsets = [value - root for value in steps_in_octave]
    count = len(offsets)

    def continued(index: int) -> int:
        octave, position = divmod(index, count)
        return root + offsets[position] + 12 * octave

    given = _given_notes(notes, "transpose_degrees")
    results: list[float] = []
    off: list[Note] = []
    for note in given:
        # Scale pitches are integers, so the highest one at or below the pitch is also the highest
        # at or below its floor.
        octave, within = divmod(math.floor(note.pitch) - root, 12)
        position = bisect.bisect_right(offsets, within) - 1
        if not (is_int(note.pitch) and offsets[position] == within):
            off.append(note)
        index = octave * count + position
        results.append(note.pitch + continued(index + steps) - continued(index))
    if off and off_scale == "refuse":
        first = off[0]
        counted = "1 note is" if len(off) == 1 else f"{len(off)} notes are"
        raise ValueError(
            f"{counted} off the scale, first at beat {first.beat} with pitch {first.pitch}; "
            f'pass off_scale="below" to move each by the same amount as the scale note below it'
        )
    return _moved(given, results, "transpose_degrees")
