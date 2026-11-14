"""The `chords` vocabulary: chord symbols such as `Am7` or `C/G` in a song's `chords` marks."""

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from poser.core.evidence import (
    BETWEEN_SHARE,
    CLASS_NAMES,
    FLAT_NAMES,
    TOO_LITTLE_BEATS,
    PitchTally,
    percent_above,
    percent_below,
    twelve_tone_obstacle,
)
from poser.core.jsonvalue import JsonValue
from poser.core.music import CHORDS, LETTER_SEMITONES
from poser.core.names import Identifier
from poser.core.ports import AdapterContext, ConfigError
from poser.core.reading import Agrees, Contradicts, Rejected, Unchecked
from poser.core.song import is_int

NAMES = CLASS_NAMES
ACCIDENTALS: Mapping[str, int] = {"": 0, "#": 1, "b": -1}
# Each quality spelling, mapped to its key of CHORDS; every key has exactly one spelling.
SUFFIXES: Mapping[str, str] = {
    "": "maj",
    "m": "min",
    "dim": "dim",
    "aug": "aug",
    "sus2": "sus2",
    "sus4": "sus4",
    "7": "7",
    "maj7": "maj7",
    "m7": "min7",
    "m7b5": "m7b5",
    "dim7": "dim7",
}
SPELLINGS: Mapping[str, str] = {quality: suffix for suffix, quality in SUFFIXES.items()}

_ROOT = "([A-G])([#b]?)"
_SUFFIX = "|".join(re.escape(s) for s in sorted(SUFFIXES, key=len, reverse=True))
PATTERN = re.compile(f"{_ROOT}({_SUFFIX})(?:/{_ROOT})?")

REASON = (
    "must be a root A to G with optional # or b, a quality such as m, 7, maj7, m7, dim, aug, "
    "sus4, and an optional /bass, such as Am7 or C/G"
)

CHORD_TONE_FLOOR = 0.4  # Chord tones sounding for less than this share contradict the label.
CHORD_CLASH_LIMIT = 0.15  # Clashing tones sounding for more than this share contradict it.
PERFECT_FIFTH = 7
MINOR_THIRD = 3
MINOR_SEVENTH = 10
# Each pair of intervals of which a quality holds one: the other would change the quality.
ALTERNATIVES = ((3, 4), (10, 11))
# What an interval from the root makes of a chord that does not hold it.
CLASH_NAMES: Mapping[int, str] = {
    3: "makes this chord minor",
    4: "makes this chord major",
    10: "makes this chord a dominant seventh",
    11: "makes this chord a major seventh",
    PERFECT_FIFTH: "gives this chord a perfect fifth",
}

GUIDE = """\
## Chords

The `chords` vocabulary reads a song's `chords` marks. A label is a root `A` to `G` with an
optional `#` or `b`, then a quality, then an optional `/` and a bass note spelled the same way.
The qualities are none (major), `m`, `dim`, `aug`, `sus2`, `sus4`, `7`, `maj7`, `m7`, `m7b5`,
and `dim7`. Spelling is strict: no spaces, no lowercase roots, and no other qualities. For
example, `Am7` is A minor seventh and `C/G` is C major over G.

`read-song` shows the chords marks above the framework readings, with each label as written. A
`warning` names the chord as written and spells notes the way its root is spelled: with flats
under a flat root such as `Db`, with sharps under a sharp root such as `F#`, and under a natural
root with sharps, except a clashing minor third or minor seventh, such as `Eb` against `C`.

`read-song` checks each chord mark against the notes inside its span, counting each note for the
beats it sounds there under its nearest twelve-tone pitch class. The chord's tones are its root,
its quality's intervals, and its bass. The mark is contradicted, with a `warning`, when its
tones sound for less than {floor} of the sounding time, or when the tones that would change its
quality sound for more than {limit}: the other third, the other seventh, or the perfect fifth of
a chord without one. The mark is `unchecked` when no selected notes sound in its span, when less
than {little} beats of sound do, or when more than {between} of the sounding time lies between
twelve-tone pitch classes.

A passing check is weak evidence: it tests only the share of the sounding time on the chord's
tones and on its clashing tones. Not every chord tone has to sound. Neither the root nor a slash
bass has to sound, so `C/F#` passes over a C major triad, and the bass is not checked for being
the lowest note. `sus` and a triad are not told apart: `Csus4` passes over a C major triad, and
`C` passes over C, F, and G. A label that shares most of its tones with the notes passes, such
as `Am` or `Em` over a C major triad. An unaccompanied scale run can contradict a true label.

With `--track`, a mark is checked against that track alone. Checking one part at a time finds a
single part that disagrees with a chord the full mix satisfies, such as a wrong bass note. Where
the track is otherwise silent in a mark's span, its notes that ring briefly over the bar line
from the previous chord are all that is checked: several such notes can sum past the
{little}-beat minimum and give a warning that the chord's tones sound for 0% of the time.
""".format(
    floor=f"{CHORD_TONE_FLOOR:.0%}",
    limit=f"{CHORD_CLASH_LIMIT:.0%}",
    little=f"{TOO_LITTLE_BEATS:g}",
    between=f"{BETWEEN_SHARE:.0%}",
)


@dataclass(frozen=True)
class Chord:
    """A chord symbol: a root, a quality from `CHORDS`, and a bass, each pitch class 0 (C) to 11.

    `label` is the symbol as its author wrote it, which check sentences echo; it is empty for a
    chord built from pitch classes, and two chords that differ only in it are equal.
    """

    root: int
    quality: str
    bass: int
    label: str = dataclasses.field(default="", compare=False)

    def __post_init__(self) -> None:
        for field in ("root", "bass"):
            value = getattr(self, field)
            if not (is_int(value) and 0 <= value <= 11):
                raise ValueError(f"Chord.{field} must be an int from 0 to 11, got {value!r}")
        if self.quality not in CHORDS:
            raise ValueError(f"Chord.quality must be a key of CHORDS, got {self.quality!r}")

    @property
    def name(self) -> str:
        """The chord's symbol, spelled with sharps."""
        name = NAMES[self.root] + SPELLINGS[self.quality]
        return name if self.bass == self.root else f"{name}/{NAMES[self.bass]}"

    def to_data(self) -> JsonValue:
        return {"chord": self.name, "root": self.root, "quality": self.quality, "bass": self.bass}

    def describe(self) -> str:
        return self.name

    @property
    def tones(self) -> frozenset[int]:
        """The chord's pitch classes: its root, its quality's intervals, and its bass."""
        return frozenset({self.bass, *((self.root + step) % 12 for step in CHORDS[self.quality])})

    @property
    def clashes(self) -> Mapping[int, str]:
        """Each pitch class that would make this a different chord, with what it makes of it.

        They are the other third, the other seventh, and the perfect fifth of a chord without
        one, less any that are chord tones.
        """
        intervals = CHORDS[self.quality]
        steps = [
            other
            for pair in ALTERNATIVES
            for held, other in (pair, pair[::-1])
            if held in intervals and other not in intervals
        ]
        if PERFECT_FIFTH not in intervals:
            steps.append(PERFECT_FIFTH)
        return {
            (self.root + step) % 12: CLASH_NAMES[step]
            for step in steps
            if (self.root + step) % 12 not in self.tones
        }

    def _spelling(self, written: str) -> Sequence[str]:
        """The name of each pitch class in a sentence about this chord, written as `written`.

        Flats under a flat root and sharps under a sharp one. Under a natural root, sharps,
        except a clashing minor third or minor seventh, which is flat.
        """
        accidental = written[1:2]
        if accidental == "b":
            return FLAT_NAMES
        if accidental == "#":
            return CLASS_NAMES
        flat = {(self.root + step) % 12 for step in (MINOR_THIRD, MINOR_SEVENTH)} & set(
            self.clashes
        )
        return tuple(FLAT_NAMES[pc] if pc in flat else CLASS_NAMES[pc] for pc in range(12))

    def check(self, tally: PitchTally) -> Agrees | Contradicts | Unchecked:
        """Compare the chord with what sounds in its span."""
        if obstacle := twelve_tone_obstacle(tally):
            return Unchecked(obstacle)
        written = self.label or self.name
        names = self._spelling(written)
        tones = self.tones
        on_tones = tally.share(tones)
        if on_tones < CHORD_TONE_FLOOR:
            return Contradicts(
                f"{percent_below(on_tones, CHORD_TONE_FLOOR)}% of the sounding time is on the "
                f"tones of {written}, most of the rest on "
                f"{tally.loudest(set(range(12)) - tones, names)}"
            )
        clashes = self.clashes
        on_clashes = tally.share(clashes)
        if on_clashes <= CHORD_CLASH_LIMIT:
            return Agrees()
        largest = min(clashes, key=lambda pc: (-tally.weights[pc], pc))
        return Contradicts(
            f"{percent_above(on_clashes, CHORD_CLASH_LIMIT)}% of the sounding time is on "
            f"{tally.loudest(clashes, names)}, which {clashes[largest]}, not {written}"
        )


def _pitch_class(letter: str, accidental: str) -> int:
    return (LETTER_SEMITONES[letter] + ACCIDENTALS[accidental]) % 12


def parse_chord(text: str) -> Chord | Rejected:
    """Return the chord a symbol names, or Rejected when it is not one."""
    match = PATTERN.fullmatch(text)
    if match is None:
        return Rejected(REASON)
    letter, accidental, suffix, bass_letter, bass_accidental = match.groups()
    root = _pitch_class(letter, accidental)
    bass = root if bass_letter is None else _pitch_class(bass_letter, bass_accidental)
    return Chord(root, SUFFIXES[suffix], bass, text)


class ChordsVocabulary:
    """The `chords` vocabulary adapter."""

    @property
    def name(self) -> Identifier:
        return Identifier("chords")

    @property
    def guide(self) -> str:
        return GUIDE

    def parse(self, text: str) -> Chord | Rejected:
        return parse_chord(text)


def create_chords(options: Mapping[str, JsonValue], _context: AdapterContext) -> ChordsVocabulary:
    """The factory for `chords`. It takes no options."""
    for key in options:
        raise ConfigError(f"unknown option {key!r}; chords takes no options")
    return ChordsVocabulary()
