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
    SILENCE,
    TOO_FEW,
    TonalKey,
    create_tonal,
    read_keys,
)
from poser.core.graph import Graph
from poser.core.loading import load_song
from poser.core.names import NodeName
from poser.core.ports import AdapterContext
from poser.core.reading import Ambiguous, Known, Outcome, Reading, ScoreRequest, Unknowable
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
    request = ScoreRequest.default(song)
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
    assert known(whole(demo), 0, "major").correlation == pytest.approx(0.902, abs=1e-3)


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
    ],
    ids=["19-edo-chromatic", "quarter-tones", "staggered-even", "c-major-triad"],
)
def test_unknowable_fixtures(specs: Sequence[Spec], reason: str) -> None:
    assert whole(one_track(specs)) == Unknowable(reason)


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
    request = ScoreRequest(song, ())

    reading = read_keys(request, 16)

    assert reading.end == 24
    assert [span.outcome for span in reading.spans] == [Unknowable(SILENCE)] * 2


def test_two_keys_read_as_two_spans_and_a_thin_tail_as_too_few() -> None:
    raised = [(beat + 16, pitch + 6, length) for beat, pitch, length in CADENCE]
    song = one_track(CADENCE + raised)

    first, second = read_keys(ScoreRequest.default(song), 16).spans
    assert (first.start, first.end, second.start, second.end) == (0, 16, 16, 27)
    known(first.outcome, 0, "major")
    assert known(second.outcome, 6, "major").name == "F# major"

    tail = one_track([*CADENCE, *raised, (32, 60, 4), (36, 67, 4)])
    spans = read_keys(ScoreRequest.default(tail), 16).spans
    assert [(span.start, span.end) for span in spans] == [(0, 16), (16, 32), (32, 40)]
    assert spans[2].outcome == Unknowable(TOO_FEW)


def test_window_boundaries_are_multiples_of_the_window() -> None:
    song = one_track([(0, 60, 30)])
    reading = read_keys(ScoreRequest.default(song), 7.5)
    assert [span.end for span in reading.spans] == [7.5, 15, 22.5, 30]


def test_unpitched_tracks_are_not_read() -> None:
    drums = Track("drums", "kit", notes([(0, 36, 1), (1, 38, 1), (2, 42, 1)]), pitched=False)
    melody = Track("melody", "synth", notes(CADENCE))
    with_drums = Song(60, (drums, melody))

    request = ScoreRequest.default(with_drums)

    assert request.tracks == (NodeName("melody"),)
    assert read_keys(request, 16) == read_keys(ScoreRequest.default(one_track(CADENCE)), 16)


def test_the_adapter_reads_with_its_window() -> None:
    root = Path("/studio")
    framework = create_tonal({"window_beats": 8}, AdapterContext(root, root, root))
    song = one_track([*CADENCE, (12, 60, 4)])
    request = ScoreRequest.default(song)
    assert framework.name == "tonal"
    assert framework.guide.startswith("## Tonal reading")
    assert framework.read(request) == read_keys(request, 8)
    assert isinstance(framework.read(request), Reading)


def test_the_guide_states_every_reason() -> None:
    text = " ".join(GUIDE.split())
    for reason in (SILENCE, BETWEEN, TOO_FEW, EVEN, "no key fits"):
        assert reason in text


# Calibration: these pin the seed sets, so any change in behaviour shows. The bounds hold for these
# generators, not for music in general.

SEEDS = range(300)


def known_share(songs: list[Song], window_beats: float | None) -> float:
    """The share of windows read as known; `None` reads each song as one window."""
    windows = known = 0
    for song in songs:
        request = ScoreRequest.default(song)
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
        for span in read_keys(ScoreRequest.default(song), 16).spans:
            windows += 1
            outcome = span.outcome
            right += isinstance(outcome, Known) and (
                (outcome.value.tonic, outcome.value.mode) == (tonic, mode)
            )
    assert right / windows >= 0.9
