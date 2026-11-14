"""Evidence: how long each pitch class sounds between two beats, which frameworks read and
claims are checked against."""

import math
from collections.abc import Iterable
from dataclasses import dataclass

from poser.core.reading import ScoreRequest

CLASS_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

BETWEEN_SEMITONES = 0.4  # A note farther than 40 cents from its nearest class lies between two.
BETWEEN_SHARE = 0.25  # More than this share of sounding time between classes cannot be judged.
TOO_LITTLE_BEATS = 0.25  # Less sounding time than this cannot be judged.

TOO_LITTLE = "too little sounds"
BETWEEN = "pitches lie between twelve-tone pitch classes"

LISTED_CLASSES = 3  # A sentence names at most this many pitch classes.


@dataclass(frozen=True)
class PitchTally:
    """What sounds between two beats, in beats of sounding time.

    `weights` holds the sounding time of each of the 12 pitch classes, class 0 being C; a note
    counts toward its nearest class. `total` is the sounding time of every counted note, and
    `between` that of the notes more than `BETWEEN_SEMITONES` from their nearest class.
    """

    weights: tuple[float, ...]
    total: float
    between: float

    def __post_init__(self) -> None:
        weights = tuple(self.weights)
        if len(weights) != 12:
            raise ValueError(f"PitchTally.weights must hold 12 numbers, got {len(weights)}")
        for value in (*weights, self.total, self.between):
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"PitchTally holds {value!r}, not a finite time of 0 or more")
        object.__setattr__(self, "weights", weights)

    def share(self, classes: Iterable[int]) -> float:
        """The share of the sounding time on the given pitch classes, each counted once."""
        if self.total <= 0:
            raise ValueError("PitchTally.share needs sounding time, and nothing sounds")
        return math.fsum(self.weights[pc % 12] for pc in {pc % 12 for pc in classes}) / self.total

    def loudest(self, classes: Iterable[int]) -> str:
        """The sounding classes among `classes`, as `F#`, `F# and A#`, or `F#, A# and C#`.

        Longest sounding first, ties in pitch-class order from C, at most three, spelled with
        sharps. Empty when none of them sounds.
        """
        sounding = sorted(
            (pc for pc in {pc % 12 for pc in classes} if self.weights[pc] > 0),
            key=lambda pc: (-self.weights[pc], pc),
        )
        names = [CLASS_NAMES[pc] for pc in sounding[:LISTED_CLASSES]]
        if len(names) < 2:
            return "".join(names)
        return f"{', '.join(names[:-1])} and {names[-1]}"


def tally_pitches(request: ScoreRequest, start: float, end: float) -> PitchTally:
    """Tally the request's notes between the beats `start` and `end`.

    A note counts for the part of its length inside the span, and notes sounding together each
    count. Every sum is exact, so the tally does not depend on the order of the notes.
    """
    by_class: list[list[float]] = [[] for _ in range(12)]
    between: list[float] = []
    for note in request.notes():
        weight = max(0.0, min(note.beat + note.length, end) - max(note.beat, start))
        if weight <= 0:
            continue
        nearest = math.floor(note.pitch + 0.5)
        by_class[nearest % 12].append(weight)
        if abs(note.pitch - nearest) > BETWEEN_SEMITONES:
            between.append(weight)
    return PitchTally(
        weights=tuple(math.fsum(items) for items in by_class),
        total=math.fsum(weight for items in by_class for weight in items),
        between=math.fsum(between),
    )


def twelve_tone_obstacle(tally: PitchTally) -> str | None:
    """The reason a twelve-tone rule cannot judge the tally, or None when it can."""
    if tally.total < TOO_LITTLE_BEATS:
        return TOO_LITTLE
    if tally.between > BETWEEN_SHARE * tally.total:
        return BETWEEN
    return None


def percent_above(share: float, limit: float) -> int:
    """A share that is above `limit`, as a whole percentage rounded up, so it prints above it."""
    return max(math.ceil(round(share * 100, 9)), math.floor(round(limit * 100, 9)) + 1)


def percent_below(share: float, floor: float) -> int:
    """A share that is below `floor`, as a whole percentage rounded down, so it prints below it."""
    return min(math.floor(round(share * 100, 9)), math.ceil(round(floor * 100, 9)) - 1)
