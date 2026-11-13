"""The `chords` vocabulary: its grammar, its values, and its factory."""

import itertools
from pathlib import Path

import pytest

from poser.adapters import chords
from poser.adapters.chords import Chord, ChordsVocabulary, create_chords
from poser.core.music import CHORDS
from poser.core.ports import AdapterContext, ConfigError, Vocabulary
from poser.core.reading import ReadingValue, Rejected

LETTERS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
ACCIDENTALS = {"": 0, "#": 1, "b": -1}
SUFFIXES = {
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
ROOTS = {
    letter + accidental: (pitch + shift) % 12
    for letter, pitch in LETTERS.items()
    for accidental, shift in ACCIDENTALS.items()
}
BASSES: dict[str, int | None] = {"": None} | {f"/{root}": pc for root, pc in ROOTS.items()}
REASON = (
    "must be a root A to G with optional # or b, a quality such as m, 7, maj7, m7, dim, aug, "
    "sus4, and an optional /bass, such as Am7 or C/G"
)
VOCABULARY = ChordsVocabulary()


def _context(root: Path) -> AdapterContext:
    return AdapterContext(root, root, root)


def test_suffixes_cover_every_chords_key_once() -> None:
    assert chords.SUFFIXES == SUFFIXES
    assert sorted(SUFFIXES.values()) == sorted(CHORDS)
    assert len(set(SUFFIXES.values())) == len(SUFFIXES)


def test_every_combination_parses_to_its_chord() -> None:
    cases = list(itertools.product(ROOTS.items(), SUFFIXES.items(), BASSES.items()))
    assert len(cases) == 7 * 3 * 11 * 22
    for (root, pc), (suffix, quality), (bass, bass_pc) in cases:
        expected = Chord(pc, quality, pc if bass_pc is None else bass_pc)
        assert VOCABULARY.parse(root + suffix + bass) == expected, root + suffix + bass


def test_spot_checks() -> None:
    assert VOCABULARY.parse("Cb") == Chord(11, "maj", 11)
    assert VOCABULARY.parse("Am7") == Chord(9, "min7", 9)
    assert VOCABULARY.parse("C/G") == Chord(0, "maj", 7)


@pytest.mark.parametrize("text", ["", "am", "A m", "H", "A##", "Amaj9", "A/", "A/H", " A"])
def test_rejects_with_the_documented_reason(text: str) -> None:
    assert VOCABULARY.parse(text) == Rejected(REASON)


def test_describe_round_trips() -> None:
    for root, suffix, bass in itertools.product(ROOTS, SUFFIXES, BASSES):
        chord = VOCABULARY.parse(root + suffix + bass)
        assert isinstance(chord, Chord)
        assert VOCABULARY.parse(chord.describe()) == chord


def test_values() -> None:
    chord = Chord(10, "min7", 1)
    assert isinstance(chord, ReadingValue)
    assert chord.describe() == "A#m7/C#"
    assert chord.to_data() == {"chord": "A#m7/C#", "root": 10, "quality": "min7", "bass": 1}
    assert Chord(9, "min", 9).describe() == "Am"


@pytest.mark.parametrize(
    ("root", "quality", "bass"), [(12, "maj", 0), (-1, "maj", 0), (0, "maj", True), (0, "m", 0)]
)
def test_chord_rejects_bad_fields(root: int, quality: str, bass: int) -> None:
    with pytest.raises(ValueError, match="Chord"):
        Chord(root, quality, bass)


def test_factory(tmp_path: Path) -> None:
    vocabulary = create_chords({}, _context(tmp_path))
    assert isinstance(vocabulary, Vocabulary)
    assert vocabulary.name == "chords"
    assert vocabulary.guide.startswith("## Chords\n")
    with pytest.raises(ConfigError, match="unknown option 'window_beats'"):
        create_chords({"window_beats": 8}, _context(tmp_path))
