"""Claim checks: the pitch-class tally, the key and chord rules with their thresholds, their
calibration against generated songs, and `interpret_marks` checking claims."""

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pytest
from generated_songs import chord_labels, tonal_song
from helpers import SURGE_DIR

from poser.adapters.chords import (
    CHORD_CLASH_LIMIT,
    CHORD_TONE_FLOOR,
    Chord,
    ChordsVocabulary,
    parse_chord,
)
from poser.adapters.song_formats import SONG_FORMATS
from poser.adapters.tonal import KEY_OUTSIDE_LIMIT, KeyMark, KeyVocabulary
from poser.core.evidence import (
    BETWEEN,
    TOO_LITTLE,
    PitchTally,
    percent_above,
    percent_below,
    tally_pitches,
    twelve_tone_obstacle,
)
from poser.core.graph import Graph
from poser.core.interpreting import interpret_marks
from poser.core.jsonvalue import JsonValue
from poser.core.loading import load_song
from poser.core.marks import Mark, Marks
from poser.core.names import Identifier, NodeName
from poser.core.ports import Claim, Vocabulary
from poser.core.reading import (
    Agrees,
    Contradicts,
    LayerError,
    ParsedLayer,
    ReadingValue,
    Rejected,
    ScoreRequest,
    TextLayer,
    Unchecked,
)
from poser.core.rules import Note
from poser.core.song import Song, Track
from poser.reference_setups.surge.showcase import build as build_showcase

SEEDS = range(300)
C, CS, D, DS, E, F, FS, G, GS, A, AS, B = range(60, 72)


def request(*tracks: Track) -> ScoreRequest:
    return ScoreRequest.default(Song(120, tracks).graph())


def held(times: Mapping[float, float]) -> PitchTally:
    """The tally of pitches held together from beat 0, each for its number of beats."""
    notes = tuple(Note(0, pitch, length, 100) for pitch, length in times.items())
    return tally_pitches(request(Track("keys", "synth", notes)), 0, max(times.values()))


def evenly(*pitches: float) -> PitchTally:
    return held(dict.fromkeys(pitches, 1))


def chord(text: str) -> Chord:
    parsed = parse_chord(text)
    assert isinstance(parsed, Chord)
    return parsed


# The tally


def test_a_note_counts_for_the_part_inside_the_span() -> None:
    notes = (Note(0, C, 4, 100), Note(3, E, 4, 100), Note(6, G, 1, 100))
    tally = tally_pitches(request(Track("keys", "synth", notes)), 2, 5)

    assert tally.weights == (2, 0, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0)
    assert tally.total == 4
    assert tally.between == 0
    assert tally.share([0]) == 0.5
    assert tally.share([0, 0, 12, 4]) == 1


def test_overlapping_notes_each_count() -> None:
    tally = evenly(C, C + 12, E)

    assert (tally.weights[0], tally.weights[4], tally.total) == (2, 1, 3)


def test_unpitched_and_unselected_tracks_are_left_out() -> None:
    keys = Track("keys", "synth", (Note(0, C, 4, 100),))
    bass = Track("bass", "synth", (Note(0, E, 4, 100),))
    drums = Track("drums", "synth", (Note(0, G, 4, 100),), pitched=False)
    graph = Song(120, (keys, bass, drums)).graph()

    everything = tally_pitches(ScoreRequest.default(graph), 0, 4)
    selected = tally_pitches(ScoreRequest(graph, (NodeName("bass"),)), 0, 4)

    assert (everything.weights[0], everything.weights[4], everything.total) == (4, 4, 8)
    assert (selected.weights[4], selected.total) == (4, 4)


def test_a_silent_span_has_no_total_and_no_shares() -> None:
    tally = tally_pitches(request(Track("keys", "synth", (Note(0, C, 1, 100),))), 2, 4)

    assert (tally.total, tally.between, sum(tally.weights)) == (0, 0, 0)
    with pytest.raises(ValueError, match="nothing sounds"):
        tally.share([0])


@pytest.mark.parametrize("seed", range(20))
def test_a_tally_does_not_depend_on_note_order(seed: int) -> None:
    rng = random.Random(seed)
    notes = [
        Note(rng.uniform(0, 8), rng.uniform(40, 80), rng.uniform(0.1, 3), 100) for _ in range(40)
    ]
    shuffled = rng.sample(notes, len(notes))

    def tally(ordered: list[Note]) -> PitchTally:
        return tally_pitches(request(Track("keys", "synth", tuple(ordered))), 1, 7)

    assert tally(notes) == tally(shuffled)


def test_a_fractional_pitch_counts_toward_its_nearest_class() -> None:
    tally = held({C + 0.3: 1, E - 0.45: 1})

    assert (tally.weights[0], tally.weights[4], tally.between) == (1, 1, 1)


def test_sounding_classes_are_listed_longest_first_then_from_c() -> None:
    tally = held({FS: 2, AS: 1, CS: 1, D: 0.5})

    assert tally.loudest([FS]) == "F#"
    assert tally.loudest([FS, CS]) == "F# and C#"
    assert tally.loudest([CS, AS, FS, D, E]) == "F#, C# and A#"
    assert tally.loudest([E]) == ""


def test_printed_shares_never_fall_on_the_passing_side() -> None:
    assert percent_above(0.16, 0.15) == 16
    assert percent_above(0.1501, 0.15) == 16
    assert percent_above(1 / 3, 0.15) == 34
    assert percent_below(0.39, 0.4) == 39
    assert percent_below(0.3999, 0.4) == 39
    assert percent_below(0, 0.4) == 0


def test_the_obstacles_to_a_twelve_tone_rule() -> None:
    assert twelve_tone_obstacle(held({C: 0.2})) == TOO_LITTLE == "too little sounds"
    assert twelve_tone_obstacle(held({C: 0.25})) is None
    assert twelve_tone_obstacle(evenly(C + 0.5, E + 0.5, G)) == BETWEEN
    assert BETWEEN == "pitches lie between twelve-tone pitch classes"
    assert twelve_tone_obstacle(evenly(C + 0.5, E, G, B)) is None
    assert twelve_tone_obstacle(evenly(C, E, G)) is None


# The key rule

C_MAJOR = KeyMark(0, "major")


def test_key_marks_and_chords_are_claims() -> None:
    assert isinstance(C_MAJOR, Claim)
    assert isinstance(chord("C"), Claim)


def test_a_major_key_agrees_with_its_scale_and_its_relative_minor() -> None:
    assert C_MAJOR.check(evenly(C, D, E, F, G, A, B)) == Agrees()
    assert C_MAJOR.check(held({A: 4, C + 12: 2, E: 2, B: 1, D: 1})) == Agrees()


def test_a_major_key_is_contradicted_by_a_distant_triad() -> None:
    assert C_MAJOR.check(held({FS: 2, AS: 1, CS: 1, C: 1})) == Contradicts(
        "80% of the sounding time is outside C major, most on F#, C# and A#"
    )


def test_minor_allows_the_raised_sixth_and_seventh() -> None:
    assert KeyMark(9, "minor").check(evenly(A, B, C, D, E, FS, GS, F, G)) == Agrees()
    assert len(KeyMark(9, "minor").allowed) == 9


def test_a_modal_key_agrees_with_its_own_scale() -> None:
    dorian = evenly(D, E, F, G, A, B, C)

    assert KeyMark(2, "dorian").check(dorian) == Agrees()
    assert KeyMark(2, "minor").check(dorian) == Agrees()
    assert KeyMark(2, "phrygian").check(dorian) == Contradicts(
        "29% of the sounding time is outside D phrygian, most on E and B"
    )


def test_the_key_limit_is_pinned() -> None:
    assert KEY_OUTSIDE_LIMIT == 0.15
    assert C_MAJOR.check(held({C: 4.3, G: 4.3, FS: 1.4})) == Agrees()
    assert C_MAJOR.check(held({C: 4.2, G: 4.2, FS: 1.6})) == Contradicts(
        "16% of the sounding time is outside C major, most on F#"
    )


def test_a_parallel_minor_passage_contradicts_a_major_key() -> None:
    check = KeyMark(2, "major").check(evenly(D, E, F, G, A, AS, C))

    assert check == Contradicts("43% of the sounding time is outside D major, most on C, F and A#")


# The chord rule


def test_a_chord_agrees_with_its_tones_some_of_them_and_a_passing_note() -> None:
    assert chord("C").check(evenly(C, E, G)) == Agrees()
    assert chord("C").check(evenly(E, G)) == Agrees()
    assert chord("C").check(held({C: 1, E: 1, G: 1, D: 1})) == Agrees()


def test_a_chord_is_contradicted_by_another_chord_s_notes() -> None:
    assert chord("C").check(evenly(FS, AS, CS)) == Contradicts(
        "0% of the sounding time is on the tones of C, most of the rest on C#, F# and A#"
    )


def test_the_chord_tone_floor_is_pinned() -> None:
    assert CHORD_TONE_FLOOR == 0.4
    assert chord("C").check(held({C: 1.4, E: 1.4, G: 1.3, D: 2, F: 2, A: 1.9})) == Agrees()
    assert chord("C").check(held({C: 1.3, E: 1.3, G: 1.3, D: 2.1, F: 2, A: 2})) == Contradicts(
        "39% of the sounding time is on the tones of C, most of the rest on D, F and A"
    )


def test_the_other_third_contradicts_a_chord() -> None:
    assert chord("Cm").check(evenly(C, E, G)) == Contradicts(
        "34% of the sounding time is on E, which makes this chord major, not Cm"
    )
    assert chord("C").check(evenly(C, DS, G)) == Contradicts(
        "34% of the sounding time is on D#, which makes this chord minor, not C"
    )


def test_the_other_seventh_contradicts_a_chord() -> None:
    assert chord("Cmaj7").check(evenly(C, E, G, AS)) == Contradicts(
        "25% of the sounding time is on A#, which makes this chord a dominant seventh, not Cmaj7"
    )
    assert chord("C7").check(evenly(C, E, G, B)) == Contradicts(
        "25% of the sounding time is on B, which makes this chord a major seventh, not C7"
    )


def test_the_chord_clash_limit_is_pinned() -> None:
    assert CHORD_CLASH_LIMIT == 0.15
    assert chord("Cm").check(held({C: 2.9, DS: 2.9, G: 2.8, E: 1.4})) == Agrees()
    assert chord("Cm").check(held({C: 2.8, DS: 2.8, G: 2.8, E: 1.6})) == Contradicts(
        "16% of the sounding time is on E, which makes this chord major, not Cm"
    )


def test_a_perfect_fifth_contradicts_a_chord_without_one() -> None:
    assert chord("Cdim").check(evenly(C, DS, G)) == Contradicts(
        "34% of the sounding time is on G, which gives this chord a perfect fifth, not Cdim"
    )
    assert chord("Caug").check(evenly(C, E, GS)) == Agrees()


def test_several_clashing_tones_are_named_with_the_largest_first() -> None:
    assert chord("Cm7b5").check(held({C: 3, DS: 3, FS: 2, G: 1.2, E: 0.8})) == Contradicts(
        "20% of the sounding time is on G and E, which gives this chord a perfect fifth, not Cm7b5"
    )


def test_a_suspended_chord_has_no_third_to_clash_with() -> None:
    assert chord("Csus4").clashes == {}
    assert chord("Csus4").check(held({C: 1, F: 1, G: 1, E: 1})) == Agrees()


@pytest.mark.parametrize(
    ("text", "clashes"),
    [
        ("C", {3}),
        ("Cm", {4}),
        ("Cdim", {4, 7}),
        ("Caug", {3, 7}),
        ("Csus2", set()),
        ("C7", {3, 11}),
        ("Cmaj7", {3, 10}),
        ("Cm7", {4, 11}),
        ("Cm7b5", {4, 7, 11}),
        ("Cdim7", {4, 7}),
        ("Cm/E", set()),
        ("D7", {5, 1}),
    ],
)
def test_the_clashing_tones_of_each_quality(text: str, clashes: set[int]) -> None:
    assert set(chord(text).clashes) == clashes


def test_the_bass_is_a_chord_tone() -> None:
    assert chord("C/E").tones == {0, 4, 7}
    assert chord("C/D").tones == {0, 2, 4, 7}
    passage = held({D: 7, C: 3})
    assert chord("C/D").check(passage) == Agrees()
    assert chord("C").check(passage) == Contradicts(
        "30% of the sounding time is on the tones of C, most of the rest on D"
    )


@pytest.mark.parametrize("value", [C_MAJOR, chord("C")], ids=["key", "chord"])
def test_both_rules_leave_what_they_cannot_judge_unchecked(value: Claim) -> None:
    assert value.check(evenly(C + 0.5, E + 0.5, G + 0.5)) == Unchecked(BETWEEN)
    assert value.check(held({FS: 0.2})) == Unchecked(TOO_LITTLE)


# Calibration against generated songs


def whole(song: Song) -> PitchTally:
    score = ScoreRequest.default(song.graph())
    return tally_pitches(score, 0, score.end)


def test_generated_songs_agree_with_their_own_key_and_not_with_a_distant_one() -> None:
    contradicted = 0
    for seed in SEEDS:
        song, (tonic, mode) = tonal_song(seed)
        tally = whole(song)
        assert KeyMark(tonic, mode).check(tally) == Agrees(), seed
        distant = KeyMark((tonic + 6) % 12, mode).check(tally)
        contradicted += isinstance(distant, Contradicts)

    assert contradicted >= 0.95 * len(SEEDS)


def test_generated_chords_agree_with_their_labels_and_not_with_altered_ones() -> None:
    marks = transposed = swapped = 0
    for seed in SEEDS:
        song, _ = tonal_song(seed)
        score = ScoreRequest.default(song.graph())
        for start, end, text in chord_labels(song):
            tally = tally_pitches(score, start, end)
            label = chord(text)
            marks += 1
            assert label.check(tally) == Agrees(), (seed, start, text)
            moved = Chord((label.root + 6) % 12, label.quality, (label.bass + 6) % 12)
            transposed += isinstance(moved.check(tally), Contradicts)
            other = Chord(label.root, {"maj": "min", "min": "maj"}[label.quality], label.bass)
            swapped += isinstance(other.check(tally), Contradicts)

    assert marks == 16 * len(SEEDS)
    assert transposed >= 0.95 * marks
    assert swapped >= 0.9 * marks


VOCABULARIES: tuple[Vocabulary, ...] = (KeyVocabulary(), ChordsVocabulary())


def packaged_songs() -> dict[str, Graph]:
    return {
        "showcase": build_showcase().graph(),
        "demo": load_song(SURGE_DIR / "demo.yaml", SONG_FORMATS).graph,
    }


@pytest.mark.parametrize("name", ["showcase", "demo"])
def test_the_packaged_songs_marks_are_never_contradicted(name: str) -> None:
    authored = interpret_marks(ScoreRequest.default(packaged_songs()[name]), VOCABULARIES)

    for layer in authored.layers.values():
        assert isinstance(layer, ParsedLayer)
        for parsed in layer.marks:
            assert isinstance(parsed.check, Agrees | Unchecked), parsed
    if name == "showcase":
        assert set(authored.layers) == {"chords", "key"}


# `interpret_marks` checking claims

TRIAD = Track("keys", "synth", tuple(Note(0, pitch, 4, 100) for pitch in (C, E, G)))
LATER = Track("later", "synth", (Note(8, C, 4, 100),))


@dataclass(frozen=True)
class Plain:
    """A value that makes no claim."""

    text: str

    def to_data(self) -> JsonValue:
        return {"label": self.text}

    def describe(self) -> str:
        return self.text


@dataclass(frozen=True)
class Probe:
    """A claim that answers with whatever `answer` makes of the tally."""

    answer: Callable[[PitchTally], object]

    def to_data(self) -> JsonValue:
        return {}

    def describe(self) -> str:
        return "probe"

    def check(self, tally: PitchTally) -> Agrees | Contradicts | Unchecked:
        return self.answer(tally)  # ty: ignore[invalid-return-type]


class Fixed:
    """A vocabulary reading the layer `name`, every label as `value`."""

    def __init__(self, name: str, value: ReadingValue) -> None:
        self._name = Identifier(name)
        self._value = value

    @property
    def name(self) -> Identifier:
        return self._name

    @property
    def guide(self) -> str:
        return f"## {self._name}\n"

    def parse(self, text: str) -> ReadingValue | Rejected:
        return self._value


def checks(layer: object) -> list[object]:
    assert isinstance(layer, ParsedLayer)
    return [parsed.check for parsed in layer.marks]


def test_interpreting_checks_each_claim_in_its_own_span() -> None:
    marks = Marks(
        {
            "chords": [Mark(0, 2, "C"), Mark(2, 4, "F#"), Mark(4, 8, "C"), Mark(8, 12, "C")],
            "key": [Mark(0, 4, "C major")],
            "parts": [Mark(0, 4, "intro")],
            "plain": [Mark(0, 4, "anything")],
        }
    )
    song = Song(120, (TRIAD, LATER), marks=marks)

    authored = interpret_marks(
        ScoreRequest.default(song.graph()), [*VOCABULARIES, Fixed("plain", Plain("p"))]
    )

    assert checks(authored.layers["chords"]) == [
        Agrees(),
        Contradicts(
            "0% of the sounding time is on the tones of F#, most of the rest on C, E and G"
        ),
        Unchecked("nothing sounds"),
        Agrees(),
    ]
    assert checks(authored.layers["key"]) == [Agrees()]
    assert authored.layers["parts"] == TextLayer((Mark(0, 4, "intro"),))
    assert checks(authored.layers["plain"]) == [None]


def test_a_claim_is_checked_against_the_selected_tracks() -> None:
    other = Track("other", "synth", tuple(Note(0, p, 4, 100) for p in (FS, AS, CS, DS, GS)))
    song = Song(120, (TRIAD, other), marks=Marks({"chords": [Mark(0, 4, "C")]}))
    graph = song.graph()

    everything = interpret_marks(ScoreRequest.default(graph), VOCABULARIES)
    selected = interpret_marks(ScoreRequest(graph, (NodeName("keys"),)), VOCABULARIES)

    assert isinstance(checks(everything.layers["chords"])[0], Contradicts)
    assert checks(selected.layers["chords"]) == [Agrees()]


def test_a_silent_span_is_unchecked_without_calling_the_claim() -> None:
    def fail(_: PitchTally) -> object:
        raise AssertionError("called")

    song = Song(120, (TRIAD, LATER), marks=Marks({"probe": [Mark(4, 8, "x")]}))

    authored = interpret_marks(ScoreRequest.default(song.graph()), [Fixed("probe", Probe(fail))])

    assert checks(authored.layers["probe"]) == [Unchecked("nothing sounds")]


def test_a_mark_past_the_song_s_end_is_tallied_up_to_the_end() -> None:
    seen: list[PitchTally] = []

    def record(tally: PitchTally) -> object:
        seen.append(tally)
        return Agrees()

    long = Track("keys", "synth", (Note(0, C, 4, 100), Note(2, E, 2, 100)))
    song = Song(120, (long,), marks=Marks({"probe": [Mark(1, 100, "x")]}))
    score = ScoreRequest.default(song.graph())

    authored = interpret_marks(score, [Fixed("probe", Probe(record))])

    layer = authored.layers["probe"]
    assert isinstance(layer, ParsedLayer)
    assert layer.marks[0].mark == Mark(1, 4, "x")
    assert seen == [tally_pitches(score, 1, 4)]
    assert seen[0].total == 5


def raising(_: PitchTally) -> object:
    raise RuntimeError("cannot check")


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (raising, "RuntimeError: cannot check"),
        (
            lambda _: "agrees",
            "TypeError: check returned 'agrees', which is not Agrees, Contradicts, or Unchecked",
        ),
    ],
    ids=["raises", "returns-a-string"],
)
def test_a_faulty_check_fails_only_its_layer(
    answer: Callable[[PitchTally], object], detail: str
) -> None:
    marks = Marks({"probe": [Mark(0, 4, "x")], "chords": [Mark(0, 4, "C")]})
    song = Song(120, (TRIAD,), marks=marks)

    authored = interpret_marks(
        ScoreRequest.default(song.graph()), [Fixed("probe", Probe(answer)), *VOCABULARIES]
    )

    assert authored.layers["probe"] == LayerError(Identifier("probe"), detail)
    assert checks(authored.layers["chords"]) == [Agrees()]
