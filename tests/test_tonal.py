"""The `tonal` framework: fixtures with known keys, the outcomes' order, and its calibration."""

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from generated_songs import noise_cluster, random_song, tonal_song
from helpers import SURGE_DIR

from poser.adapters.song_formats import SONG_FORMATS
from poser.adapters.tonal import (
    BETWEEN,
    DEFAULT_WINDOW_BARS,
    DEFAULT_WINDOW_BEATS,
    EVEN,
    GUIDE,
    KEY_GUIDE,
    KEY_MODES,
    KEY_OUTSIDE_LIMIT,
    KEY_REJECTION,
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
from poser.core.evidence import BETWEEN_SHARE, TOO_LITTLE_BEATS
from poser.core.graph import Graph
from poser.core.loading import load_song
from poser.core.meter import Meter, MeterSpan
from poser.core.music import MODES as SCALES
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


def bounds(
    length: float,
    spans: Sequence[MeterSpan] = (),
    window_beats: float = DEFAULT_WINDOW_BEATS,
    window_bars: int = DEFAULT_WINDOW_BARS,
) -> list[tuple[float, float]]:
    """The windows read over one held note of `length` beats under a meter of `spans`."""
    song = replace(one_track([(0, 60, length)]), meter=Meter(spans))
    reading = read_keys(ScoreRequest.default(song.graph()), window_beats, window_bars)
    return [(span.start, span.end) for span in reading.spans]


def test_metered_windows_are_four_declared_bars() -> None:
    assert bounds(84, [MeterSpan(0, 7)]) == [(0, 28), (28, 56), (56, 84)]


def test_window_bars_sets_the_bars_in_a_window() -> None:
    windows = bounds(84, [MeterSpan(0, 7)], window_bars=2)
    assert windows == [(14 * index, 14 * index + 14) for index in range(6)]


def test_window_beats_does_not_move_metered_windows() -> None:
    assert bounds(84, [MeterSpan(0, 7)], window_beats=5) == bounds(84, [MeterSpan(0, 7)])


def test_metered_windows_start_where_the_meter_starts() -> None:
    assert bounds(44, [MeterSpan(8, 3)]) == [(0, 8), (8, 20), (20, 32), (32, 44)]


def test_a_meter_change_splits_a_window() -> None:
    windows = bounds(40, [MeterSpan(0, 4), MeterSpan(8, 3)])
    assert windows == [(0, 8), (8, 20), (20, 32), (32, 40)]


def test_an_unmetered_stretch_after_a_meter_counts_from_its_own_start() -> None:
    windows = bounds(54, [MeterSpan(0, 3, 12)])
    assert windows == [(0, 12), (12, 28), (28, 44), (44, 54)]


def test_a_sliver_after_the_last_window_joins_it() -> None:
    windows = bounds(176.0036)
    assert windows[-1] == (160, 176.0036)
    assert len(windows) == 11
    assert min(end - start for start, end in windows) >= 8


def test_a_metered_sliver_joins_the_window_before_it() -> None:
    assert bounds(56.0036, [MeterSpan(0, 7)]) == [(0, 28), (28, 56.0036)]
    assert bounds(35, [MeterSpan(0, 7)]) == [(0, 35)]
    assert bounds(42, [MeterSpan(0, 7)]) == [(0, 28), (28, 42)]


def test_a_sliver_past_the_last_bar_line_adds_no_window() -> None:
    windows = bounds(176.0036, [MeterSpan(0, 4)])
    assert len(windows) == 11
    assert windows[-1] == (160, 176.0036)
    # One bar past a whole window is still its own bar, read with the window before it.
    assert bounds(20.0036, [MeterSpan(0, 4)]) == [(0, 20.0036)]
    assert bounds(24.0036, [MeterSpan(0, 4)]) == [(0, 16), (16, 24.0036)]


def test_a_remainder_of_exactly_half_a_window_stays_its_own_window() -> None:
    assert bounds(40) == [(0, 16), (16, 32), (32, 40)]
    assert bounds(39) == [(0, 16), (16, 39)]


def test_a_song_shorter_than_half_a_window_is_one_window() -> None:
    assert bounds(5) == [(0, 5)]


def test_a_short_stretch_is_one_short_window() -> None:
    """A pickup bar given its own meter span never joins the window after or before it."""
    windows = bounds(33, [MeterSpan(0, 1, 1), MeterSpan(1, 4)])
    assert windows == [(0, 1), (1, 17), (17, 33)]


def test_unpitched_tracks_are_not_read() -> None:
    drums = Track("drums", "kit", notes([(0, 36, 1), (1, 38, 1), (2, 42, 1)]), pitched=False)
    melody = Track("melody", "synth", notes(CADENCE))
    with_drums = Song(60, (drums, melody))

    request = ScoreRequest.default(with_drums.graph())

    assert request.tracks == (NodeName("melody"),)
    assert read_keys(request, 16) == read_keys(ScoreRequest.default(one_track(CADENCE).graph()), 16)


CONTEXT = AdapterContext(Path("/studio"), Path("/studio"), Path("/studio"))


def test_the_adapter_reads_metered_stretches_with_its_window_bars() -> None:
    framework = create_tonal({"window_bars": 2, "window_beats": 8}, CONTEXT)
    song = replace(one_track([(0, 60, 26)]), meter=Meter([MeterSpan(8, 3)]))
    request = ScoreRequest.default(song.graph())
    assert (framework.window_bars, framework.window_beats) == (2, 8)
    assert framework.read(request) == read_keys(request, 8, 2)
    assert [span.end for span in framework.read(request).spans] == [8, 14, 20, 26]
    default = create_tonal({}, CONTEXT)
    assert (default.window_bars, default.window_beats) == (4, 16)


@pytest.mark.parametrize("bars", [0, -1, 2.5, 4.0, "4", True, None])
def test_the_factory_rejects_window_bars_that_are_not_a_whole_number(bars: object) -> None:
    message = "window_bars: must be a whole number of at least 1, got"
    with pytest.raises(ConfigError, match=message):
        create_tonal({"window_bars": bars}, CONTEXT)  # ty: ignore[invalid-argument-type]


def test_the_factory_names_both_options_for_an_unknown_one() -> None:
    with pytest.raises(ConfigError, match="tonal takes only 'window_bars' and 'window_beats'"):
        create_tonal({"window": 8}, CONTEXT)


@pytest.mark.parametrize("bars", [0, -1, 2.5, "4", True])
def test_read_keys_refuses_window_bars_that_are_not_a_whole_number(bars: object) -> None:
    song, _ = tonal_song(0)
    with pytest.raises(ValueError, match="window_bars must be a whole number of at least 1"):
        read_keys(ScoreRequest.default(song.graph()), 16, bars)  # ty: ignore[invalid-argument-type]


def test_the_guide_names_both_options_and_their_defaults() -> None:
    text = " ".join(GUIDE.split())
    assert (DEFAULT_WINDOW_BARS, DEFAULT_WINDOW_BEATS) == (4, 16)
    assert f"`window_bars` bars ({DEFAULT_WINDOW_BARS} unless configured" in text
    assert f"`window_beats` beats ({DEFAULT_WINDOW_BEATS:g} unless configured" in text
    assert "shorter than half a window is read together with the one before it" in text
    assert "reason to raise `window_bars`" in text
    assert "four beats" not in text


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

KEY_REASON = (
    "must be a tonic A to G with optional # or b, then major, minor, dorian, phrygian, lydian, "
    "mixolydian, or locrian, such as A minor"
)
LETTER_CLASSES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
ACCIDENTAL_STEPS = {"": 0, "#": 1, "b": -1}
KEY_GRID = [
    (f"{letter}{accidental} {mode}", (pitch + step) % 12, mode)
    for letter, pitch in LETTER_CLASSES.items()
    for accidental, step in ACCIDENTAL_STEPS.items()
    for mode in KEY_MODES
]


def test_the_key_grid_has_147_labels() -> None:
    assert len(KEY_GRID) == 147


def test_the_key_modes_are_seven_modes_of_the_scale_table() -> None:
    assert len(KEY_MODES) == len(set(KEY_MODES)) == 7
    assert set(KEY_MODES) <= set(SCALES)


def test_the_key_rejection_is_the_stated_reason() -> None:
    assert KEY_REJECTION == KEY_REASON


def test_the_key_guide_states_its_rule_with_the_code_s_numbers() -> None:
    text = " ".join(KEY_GUIDE.split())
    assert (KEY_OUTSIDE_LIMIT, TOO_LITTLE_BEATS, BETWEEN_SHARE) == (0.15, 0.25, 0.25)
    assert "more than 15% of the sounding time is outside" in text
    assert "less than 0.25 beats of sound" in text
    assert "more than 25% of the sounding time lies between" in text
    for word in ("`warning`", "`unchecked`", "`--track`", "`mixolydian`", "nine of the twelve"):
        assert word in text


def test_the_key_guide_names_every_mode() -> None:
    for mode in KEY_MODES:
        assert f"`{mode}`" in KEY_GUIDE


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
        "D Dorian",
        "C ionian",
        "A aeolian",
        "A harmonic_minor",
        "C major_pentatonic",
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
    assert KeyMark(2, "dorian").describe() == "D dorian"
    assert KeyMark(2, "dorian").to_data() == {"key": "D dorian", "tonic": 2, "mode": "dorian"}


@pytest.mark.parametrize(
    ("tonic", "mode", "message"),
    [
        (-1, "major", "KeyMark.tonic"),
        (12, "major", "KeyMark.tonic"),
        (1.0, "major", "KeyMark.tonic"),
        (True, "major", "KeyMark.tonic"),
        ("C", "major", "KeyMark.tonic"),
        (0, "Major", "KeyMark.mode"),
        (0, "ionian", "KeyMark.mode"),
        (0, "harmonic_minor", "KeyMark.mode"),
    ],
)
def test_a_key_mark_validates_like_a_tonal_key(tonic: object, mode: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        KeyMark(tonic, mode)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match=message.replace("KeyMark", "TonalKey")):
        TonalKey(tonic, mode, 0.9, 0.0)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("mode", [mode for mode in KEY_MODES if mode not in ("major", "minor")])
def test_a_key_mark_accepts_a_mode_a_tonal_key_refuses(mode: str) -> None:
    assert KeyMark(0, mode).mode == mode  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="must be major or minor"):
        TonalKey(0, mode, 0.9, 0.0)  # ty: ignore[invalid-argument-type]


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


@pytest.mark.parametrize(
    ("window_beats", "bound"),
    [(16, 0.02), (7.5, 0.03), *((length, 0.03) for length in (4, 8, 12, 20, 28, 32, 64))],
)
def test_random_notes_are_rarely_known(window_beats: float, bound: float) -> None:
    assert known_share([random_song(seed) for seed in SEEDS], window_beats) <= bound


def test_noise_clusters_are_rarely_known() -> None:
    assert known_share([noise_cluster(seed) for seed in SEEDS], None) <= 0.02


def right_share(bar: int, metered: bool) -> float:
    """The share of windows of `tonal_song` songs in `bar`-beat bars read as known in the song's
    key at the default options, with the meter declared or not."""
    windows = right = 0
    for seed in SEEDS:
        song, (tonic, mode) = tonal_song(seed, bar)
        if metered:
            song = replace(song, meter=Meter([MeterSpan(0, bar)]))
        for span in read_keys(ScoreRequest.default(song.graph()), DEFAULT_WINDOW_BEATS).spans:
            windows += 1
            outcome = span.outcome
            right += isinstance(outcome, Known) and (
                (outcome.value.tonic, outcome.value.mode) == (tonic, mode)
            )
    return right / windows


def test_tonal_songs_are_mostly_known_in_their_key() -> None:
    assert right_share(4, metered=False) >= 0.9


def test_tonal_songs_with_a_meter_are_mostly_known_in_their_key() -> None:
    assert right_share(4, metered=True) >= 0.9
    assert right_share(4, metered=True) == right_share(4, metered=False)


def test_tonal_songs_in_seven_beat_bars_read_better_with_their_meter() -> None:
    with_meter = right_share(7, metered=True)
    assert with_meter >= 0.9
    assert right_share(7, metered=False) < with_meter


def test_the_default_tonal_songs_are_pinned() -> None:
    digest = hashlib.sha256()
    for seed in SEEDS:
        digest.update(repr(tonal_song(seed)).encode())
    assert digest.hexdigest() == "10208a797778f839a6fe805d07f7f49091a20bc2f04306a83ef12ff2b2f42ed9"
    assert tonal_song(5, 4) == tonal_song(5)
    song, _ = tonal_song(5, 7)
    assert ScoreRequest.default(song.graph()).end == 4 * 4 * 7
    melody = song.tracks[1].notes
    assert [note.beat for note in melody[:8]] == list(range(8))


@pytest.mark.parametrize("window", [0, -1, float("nan"), float("inf")])
def test_read_keys_refuses_a_window_that_is_not_a_positive_number(window: float) -> None:
    song, _ = tonal_song(0)
    with pytest.raises(ValueError, match="window_beats must be a finite positive number"):
        read_keys(ScoreRequest.default(song.graph()), window)
