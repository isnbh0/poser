"""The `tonal` framework: fixtures with known keys, the outcomes' order, and its calibration."""

from collections.abc import Sequence
from pathlib import Path

import pytest
from generated_songs import noise_cluster, random_song, tonal_song
from helpers import SURGE_DIR

from poser.adapters.song_formats import SONG_FORMATS
from poser.adapters.tonal import (
    BETWEEN,
    EVEN,
    GUIDE,
    KEY_GUIDE,
    NO_FIT,
    SILENCE,
    TOO_FEW,
    KeyMark,
    KeyVocabulary,
    TonalKey,
    create_key,
    create_tonal,
    read_keys,
)
from poser.core.graph import Graph
from poser.core.loading import load_song
from poser.core.names import NodeName
from poser.core.ports import AdapterContext, ConfigError, Vocabulary
from poser.core.reading import (
    Ambiguous,
    Known,
    Outcome,
    Reading,
    Rejected,
    ScoreRequest,
    Unknowable,
)
from poser.core.rules import Note
from poser.core.song import Song, Track

type Spec = tuple[float, float, float]  # (beat, pitch, length)

CADENCE: list[Spec] = [(i, p, 1) for i, p in enumerate([60, 62, 64, 65, 67, 69, 71])]
CADENCE.append((7, 72, 4))
CHANT: list[Spec] = [
    (0, 62, 2),
    (2, 64, 1),
    (3, 65, 1),
    (4, 67, 1),
    (5, 65, 1),
    (6, 64, 1),
    (7, 62, 2),
    (9, 69, 2),
    (11, 67, 1),
    (12, 65, 1),
    (13, 64, 1),
    (14, 62, 2),
]
# Just-intonation offsets from equal temperament, in cents, by pitch.
JUST_CENTS = {62: 3.91, 64: -13.69, 65: -1.96, 67: 1.96, 69: -15.64, 71: -11.73}


def notes(specs: Sequence[Spec]) -> tuple[Note, ...]:
    return tuple(Note(beat, pitch, length, 100) for beat, pitch, length in specs)


def one_track(specs: Sequence[Spec]) -> Song:
    return Song(60, (Track("melody", "synth", notes(specs)),))


def whole(song: Song | Graph) -> Outcome[TonalKey]:
    """The outcome of a reading with one window covering the whole song."""
    request = ScoreRequest.default(song if isinstance(song, Graph) else song.graph())
    (span,) = read_keys(request, request.end).spans
    return span.outcome


def known(outcome: Outcome[TonalKey], tonic: int, mode: str) -> TonalKey:
    assert isinstance(outcome, Known), outcome
    assert (outcome.value.tonic, outcome.value.mode) == (tonic, mode)
    return outcome.value


# Fixtures


def test_a_c_major_cadence_is_known_c_major() -> None:
    key = known(whole(one_track(CADENCE)), 0, "major")
    assert key.correlation == pytest.approx(0.8556, abs=1e-4)
    assert key.deviation_cents == 0
    assert key.name == "C major"
    assert key.describe() == "C major (r 0.86)"


def test_a_plain_c_major_scale_is_ambiguous_with_a_minor() -> None:
    outcome = whole(one_track(CADENCE[:7]))
    assert isinstance(outcome, Ambiguous)
    c_major, a_minor = outcome.candidates
    assert (c_major.name, a_minor.name) == ("C major", "A minor")
    assert c_major.correlation == pytest.approx(0.756, abs=1e-3)
    assert a_minor.correlation == pytest.approx(0.712, abs=1e-3)


def test_the_reference_demo_is_known_c_major() -> None:
    demo = load_song(SURGE_DIR / "demo.yaml", SONG_FORMATS)
    assert known(whole(demo.graph), 0, "major").correlation == pytest.approx(0.902, abs=1e-3)


def test_a_d_dorian_chant_is_known_d_minor() -> None:
    assert known(whole(one_track(CHANT)), 2, "minor").correlation == pytest.approx(0.858, abs=1e-3)


def test_a_just_intonation_cadence_is_c_major_off_equal_temperament() -> None:
    just = [(b, p + JUST_CENTS.get(int(p), 0) / 100, n) for b, p, n in CADENCE]
    key = known(whole(one_track(just)), 0, "major")
    assert key.deviation_cents > 0
    assert "cents from equal temperament" in key.describe()


def test_a_19_edo_cadence_is_c_major() -> None:
    steps = (0, 3, 6, 8, 11, 14, 17)
    cadence = [(i, 60 + 12 * s / 19, 1) for i, s in enumerate(steps)] + [(7, 72, 4)]
    assert known(whole(one_track(cadence)), 0, "major").deviation_cents > 0


@pytest.mark.parametrize(
    ("specs", "reason"),
    [
        ([(i, 60 + 12 * i / 19, 1) for i in range(19)], EVEN),
        ([(i, 60 + 0.5 * i, 1) for i in range(24)], BETWEEN),
        ([(0.1 * pc, 60 + pc, 0.3) for pc in range(12)], EVEN),
        ([(0, 60, 1), (0, 64, 1), (0, 67, 1)], TOO_FEW),
        ([(0, 69, 2), (2, 61, 2), (4, 64, 3), (7, 70, 2), (9, 67, 1)], NO_FIT),
    ],
    ids=["19-edo-chromatic", "quarter-tones", "staggered-even", "c-major-triad", "no-fit"],
)
def test_unknowable_fixtures(specs: Sequence[Spec], reason: str) -> None:
    assert whole(one_track(specs)) == Unknowable(reason)


def test_two_close_strong_keys_are_ambiguous() -> None:
    """A best fit above the strong threshold is still ambiguous with a key within the lead."""
    outcome = whole(one_track([(0, 70, 2), (2, 69, 2), (4, 65, 3), (7, 62, 3), (10, 60, 2)]))
    assert isinstance(outcome, Ambiguous), outcome
    keys = [(key.tonic, key.mode, round(key.correlation, 4)) for key in outcome.candidates]
    assert keys == [(2, "minor", 0.8695), (5, "major", 0.8382)]


def test_a_lone_weak_fit_is_ambiguous_with_the_runner_up() -> None:
    """A lone best key below the strong threshold is listed with the next-best key."""
    outcome = whole(one_track([(0, 64, 1), (1, 69, 3), (4, 70, 1), (5, 66, 1), (6, 62, 1)]))
    assert isinstance(outcome, Ambiguous), outcome
    keys = [(key.tonic, key.mode, round(key.correlation, 4)) for key in outcome.candidates]
    assert keys == [(9, "major", 0.7533), (2, "major", 0.5964)]


def test_a_lone_weak_fit_lists_every_key_tied_for_second() -> None:
    song, _ = tonal_song(140)
    spans = read_keys(ScoreRequest.default(song.graph()), 7.5).spans
    (span,) = [span for span in spans if span.start == 15]
    assert isinstance(span.outcome, Ambiguous), span.outcome
    best, *tied = span.outcome.candidates
    assert (best.tonic, best.mode, round(best.correlation, 4)) == (1, "major", 0.8238)
    assert [(key.tonic, key.mode) for key in tied] == [(5, "minor"), (10, "minor")]
    assert tied[0].correlation == tied[1].correlation < best.correlation - 0.05


# Other cases


def with_quarter_tones(share: float) -> Song:
    """The cadence plus quarter-tone notes holding `share` of the sounding time."""
    sounding = sum(length for _, _, length in CADENCE)
    extra = sounding * share / (1 - share)
    return one_track([*CADENCE, (0, 66.5, extra)])


def test_quarter_tones_past_a_quarter_of_the_time_are_between_classes() -> None:
    assert whole(with_quarter_tones(0.3)) == Unknowable(BETWEEN)
    assert whole(with_quarter_tones(0.2)) != Unknowable(BETWEEN)


def test_an_empty_selection_is_silent_in_every_window() -> None:
    song = one_track([*CADENCE, (20, 60, 4)])
    request = ScoreRequest(song.graph(), ())

    reading = read_keys(request, 16)

    assert reading.end == 24
    assert [span.outcome for span in reading.spans] == [Unknowable(SILENCE)] * 2


def test_two_keys_read_as_two_spans_and_a_thin_tail_as_too_few() -> None:
    raised = [(beat + 16, pitch + 6, length) for beat, pitch, length in CADENCE]
    song = one_track(CADENCE + raised)

    first, second = read_keys(ScoreRequest.default(song.graph()), 16).spans
    assert (first.start, first.end, second.start, second.end) == (0, 16, 16, 27)
    known(first.outcome, 0, "major")
    assert known(second.outcome, 6, "major").name == "F# major"

    tail = one_track([*CADENCE, *raised, (32, 60, 4), (36, 67, 4)])
    spans = read_keys(ScoreRequest.default(tail.graph()), 16).spans
    assert [(span.start, span.end) for span in spans] == [(0, 16), (16, 32), (32, 40)]
    assert spans[2].outcome == Unknowable(TOO_FEW)


def test_window_boundaries_are_multiples_of_the_window() -> None:
    song = one_track([(0, 60, 30)])
    reading = read_keys(ScoreRequest.default(song.graph()), 7.5)
    assert [span.end for span in reading.spans] == [7.5, 15, 22.5, 30]


def test_unpitched_tracks_are_not_read() -> None:
    drums = Track("drums", "kit", notes([(0, 36, 1), (1, 38, 1), (2, 42, 1)]), pitched=False)
    melody = Track("melody", "synth", notes(CADENCE))
    with_drums = Song(60, (drums, melody))

    request = ScoreRequest.default(with_drums.graph())

    assert request.tracks == (NodeName("melody"),)
    assert read_keys(request, 16) == read_keys(ScoreRequest.default(one_track(CADENCE).graph()), 16)


def test_the_adapter_reads_with_its_window() -> None:
    root = Path("/studio")
    framework = create_tonal({"window_beats": 8}, AdapterContext(root, root, root))
    song = one_track([*CADENCE, (12, 60, 4)])
    request = ScoreRequest.default(song.graph())
    assert framework.name == "tonal"
    assert framework.guide.startswith("## Tonal reading")
    assert framework.read(request) == read_keys(request, 8)
    assert isinstance(framework.read(request), Reading)


def test_the_guide_states_every_reason() -> None:
    text = " ".join(GUIDE.split())
    for reason in (SILENCE, BETWEEN, TOO_FEW, EVEN, "no key fits"):
        assert reason in text


# The key vocabulary: stated keys in a `key` mark layer.

KEY_REASON = "must be a tonic A to G with optional # or b, then major or minor, such as A minor"
LETTER_CLASSES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
ACCIDENTAL_STEPS = {"": 0, "#": 1, "b": -1}
KEY_GRID = [
    (f"{letter}{accidental} {mode}", (pitch + step) % 12, mode)
    for letter, pitch in LETTER_CLASSES.items()
    for accidental, step in ACCIDENTAL_STEPS.items()
    for mode in ("major", "minor")
]


def test_the_key_grid_has_42_labels() -> None:
    assert len(KEY_GRID) == 42


@pytest.mark.parametrize(("text", "tonic", "mode"), KEY_GRID)
def test_the_key_vocabulary_parses_every_tonic_and_mode(text: str, tonic: int, mode: str) -> None:
    parsed = KeyVocabulary().parse(text)
    assert isinstance(parsed, KeyMark)
    assert (parsed.tonic, parsed.mode) == (tonic, mode)


@pytest.mark.parametrize(
    ("text", "tonic"), [("Cb major", 11), ("B# minor", 0), ("E# major", 5), ("Bb minor", 10)]
)
def test_accidentals_wrap_around_the_octave(text: str, tonic: int) -> None:
    parsed = KeyVocabulary().parse(text)
    assert isinstance(parsed, KeyMark)
    assert parsed.tonic == tonic


@pytest.mark.parametrize(
    "text",
    [
        "",
        "a minor",
        "A Minor",
        "A  minor",
        "A dorian",
        "H major",
        "A## major",
        " A minor",
        "A minor ",
        "A minor\n",
        "Ab",
    ],
)
def test_the_key_vocabulary_rejects_other_spellings(text: str) -> None:
    assert KeyVocabulary().parse(text) == Rejected(KEY_REASON)


@pytest.mark.parametrize(("text", "tonic", "mode"), KEY_GRID)
def test_a_parsed_key_describes_itself_as_a_label_it_parses_back(
    text: str, tonic: int, mode: str
) -> None:
    vocabulary = KeyVocabulary()
    parsed = vocabulary.parse(text)
    assert isinstance(parsed, KeyMark)
    assert vocabulary.parse(parsed.describe()) == parsed


def test_a_key_mark_writes_its_name_tonic_and_mode() -> None:
    mark = KeyMark(9, "minor")
    assert mark.describe() == "A minor"
    assert mark.to_data() == {"key": "A minor", "tonic": 9, "mode": "minor"}
    assert KeyMark(10, "major").describe() == "A# major"


@pytest.mark.parametrize(
    ("tonic", "mode", "message"),
    [
        (-1, "major", "KeyMark.tonic"),
        (12, "major", "KeyMark.tonic"),
        (1.0, "major", "KeyMark.tonic"),
        (True, "major", "KeyMark.tonic"),
        ("C", "major", "KeyMark.tonic"),
        (0, "dorian", "KeyMark.mode"),
        (0, "Major", "KeyMark.mode"),
    ],
)
def test_a_key_mark_validates_like_a_tonal_key(tonic: object, mode: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        KeyMark(tonic, mode)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match=message.replace("KeyMark", "TonalKey")):
        TonalKey(tonic, mode, 0.9, 0.0)  # ty: ignore[invalid-argument-type]


def test_the_key_factory_builds_a_vocabulary_and_takes_no_options() -> None:
    root = Path("/studio")
    context = AdapterContext(root, root, root)
    vocabulary = create_key({}, context)
    assert isinstance(vocabulary, Vocabulary)
    assert vocabulary.name == "key"
    assert vocabulary.guide == KEY_GUIDE
    assert vocabulary.guide.startswith("## Key marks")
    with pytest.raises(ConfigError, match="'window_beats'"):
        create_key({"window_beats": 8}, context)


# Calibration: these pin the seed sets, so any change in behaviour shows. The bounds hold for these
# generators, not for music in general.

SEEDS = range(300)


def known_share(songs: list[Song], window_beats: float | None) -> float:
    """The share of windows read as known; `None` reads each song as one window."""
    windows = known = 0
    for song in songs:
        request = ScoreRequest.default(song.graph())
        reading = read_keys(request, window_beats or request.end)
        windows += len(reading.spans)
        known += sum(isinstance(span.outcome, Known) for span in reading.spans)
    return known / windows


@pytest.mark.parametrize(("window_beats", "bound"), [(16, 0.02), (7.5, 0.03)])
def test_random_notes_are_rarely_known(window_beats: float, bound: float) -> None:
    assert known_share([random_song(seed) for seed in SEEDS], window_beats) <= bound


def test_noise_clusters_are_rarely_known() -> None:
    assert known_share([noise_cluster(seed) for seed in SEEDS], None) <= 0.02


def test_tonal_songs_are_mostly_known_in_their_key() -> None:
    windows = right = 0
    for seed in SEEDS:
        song, (tonic, mode) = tonal_song(seed)
        for span in read_keys(ScoreRequest.default(song.graph()), 16).spans:
            windows += 1
            outcome = span.outcome
            right += isinstance(outcome, Known) and (
                (outcome.value.tonic, outcome.value.mode) == (tonic, mode)
            )
    assert right / windows >= 0.9


@pytest.mark.parametrize("window", [0, -1, float("nan"), float("inf")])
def test_read_keys_refuses_a_window_that_is_not_a_positive_number(window: float) -> None:
    song, _ = tonal_song(0)
    with pytest.raises(ValueError, match="window_beats must be a finite positive number"):
        read_keys(ScoreRequest.default(song.graph()), window)
