"""The `chords` vocabulary: chord symbols such as `Am7` or `C/G` in a song's `chords` marks."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from poser.core.jsonvalue import JsonValue
from poser.core.music import CHORDS, LETTER_SEMITONES
from poser.core.names import Identifier
from poser.core.ports import AdapterContext, ConfigError
from poser.core.reading import Rejected
from poser.core.song import is_int

NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
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

GUIDE = """\
## Chords

The `chords` vocabulary reads a song's `chords` marks. A label is a root `A` to `G` with an
optional `#` or `b`, then a quality, then an optional `/` and a bass note spelled the same way.
The qualities are none (major), `m`, `dim`, `aug`, `sus2`, `sus4`, `7`, `maj7`, `m7`, `m7b5`,
and `dim7`. Spelling is strict: no spaces, no lowercase roots, and no other qualities. For
example, `Am7` is A minor seventh and `C/G` is C major over G.

`read-song` shows the chords marks above the framework readings, with each label as written.
"""


@dataclass(frozen=True)
class Chord:
    """A chord symbol: a root, a quality from `CHORDS`, and a bass, each pitch class 0 (C) to 11."""

    root: int
    quality: str
    bass: int

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
    return Chord(root, SUFFIXES[suffix], bass)


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
