"""Tests of performances: the events a track's instrument plays, in samples."""

import json
from collections.abc import Callable

import pytest

from poser.core.audio import SAMPLE_RATE, TAIL_S
from poser.core.performance import (
    Control,
    Event,
    NoteOff,
    NoteOn,
    Performance,
    PitchProblem,
    PitchSupport,
    perform,
    perform_for,
    performance_from_data,
    performance_to_data,
)
from poser.core.song import ControlChange, Note, Song, Track


def make_song(*tracks: Track, bpm: float = 120) -> Song:
    return Song(bpm=bpm, tracks=tracks)


def make_track(name: str = "lead", *notes: Note) -> Track:
    return Track(name, "Serum", notes or (Note(0, 60, 1, 100),))


def cc_track(*changes: ControlChange, notes: tuple[Note, ...] = (Note(0, 60, 1, 100),)) -> Track:
    return Track("lead", "Serum", notes, cc=changes)


def performed(track: Track, song: Song | None = None) -> Performance:
    return perform(song or make_song(track), track)


def note_events(track: Track) -> list[tuple[str, float, int]]:
    """Return each note event's kind, pitch, and sample."""
    return [
        ("note_on" if isinstance(event, NoteOn) else "note_off", event.pitch, event.sample)
        for event in performed(track).events
        if isinstance(event, NoteOn | NoteOff)
    ]


def event_fields(track: Track) -> list[tuple[str, float, int]]:
    """Return each event's kind, its pitch or controller, and its sample."""
    fields: list[tuple[str, float, int]] = []
    for event in performed(track).events:
        match event:
            case NoteOff(sample, pitch):
                fields.append(("note_off", pitch, sample))
            case Control(sample, control, _):
                fields.append(("control_change", control, sample))
            case NoteOn(sample, pitch, _):
                fields.append(("note_on", pitch, sample))
    return fields


def events_for(*changes: ControlChange) -> list[tuple[int, int, int]]:
    """Return a track's controller events as (sample, control, value)."""
    return [
        (event.sample, event.control, event.value)
        for event in performed(cc_track(*changes)).events
        if isinstance(event, Control)
    ]


# Notes


def test_beats_convert_to_samples_at_bpm() -> None:
    assert note_events(make_track("lead", Note(2, 60, 1, 100))) == [
        ("note_on", 60, 48000),
        ("note_off", 60, 72000),
    ]


def test_scaled_beats_and_tempo_preserve_sample_timed_performance() -> None:
    slow = Track(
        "lead",
        "Serum",
        (Note(1, 60, 2, 100),),
        cc=(ControlChange(0, 1, 0), ControlChange(2, 1, 64)),
    )
    fast = Track(
        "lead",
        "Serum",
        (Note(2, 60, 4, 100),),
        cc=(ControlChange(0, 1, 0), ControlChange(4, 1, 64)),
    )

    performance = perform(make_song(slow, bpm=60), slow)
    assert Control(96000, 1, 64) in performance.events
    assert performance == perform(make_song(fast, bpm=120), fast)


def test_fixed_beats_follow_song_tempo() -> None:
    track = make_track("lead", Note(1, 60, 2, 100))

    slow = perform(make_song(track, bpm=60), track)
    fast = perform(make_song(track, bpm=120), track)

    assert slow.events == (NoteOn(48000, 60, 100), NoteOff(144000, 60))
    assert fast.events == (NoteOn(24000, 60, 100), NoteOff(72000, 60))
    assert slow.samples == int(5 * SAMPLE_RATE)
    assert fast.samples == int(3.5 * SAMPLE_RATE)


def test_note_velocity_reaches_the_event() -> None:
    (on, _) = performed(make_track("lead", Note(0, 60, 1, 37))).events
    assert on == NoteOn(0, 60, 37)


def test_every_track_performs_over_the_song_length_plus_tail() -> None:
    short = make_track("short", Note(0, 60, 1, 100))
    long = make_track("long", Note(6, 40, 2, 100), Note(0, 40, 1, 100))
    song = make_song(short, long)
    expected = int((4.0 + TAIL_S) * SAMPLE_RATE)
    assert perform(song, short).samples == expected
    assert perform(song, long).samples == expected


def test_repeated_pitch_ends_before_it_retriggers() -> None:
    track = make_track("lead", Note(1, 60, 1, 100), Note(0, 60, 1, 100))
    assert note_events(track) == [
        ("note_on", 60, 0),
        ("note_off", 60, 24000),
        ("note_on", 60, 24000),
        ("note_off", 60, 48000),
    ]


def test_repeated_pitch_retriggers_despite_float_error_in_beats() -> None:
    assert 0.1 + 0.2 != 0.3
    track = make_track("lead", Note(0.1, 60, 0.2, 100), Note(0.3, 60, 0.1, 100))

    types = [event[0] for event in note_events(track)]

    assert types == ["note_on", "note_off", "note_on", "note_off"]


def test_note_shorter_than_a_sample_still_ends_after_it_starts() -> None:
    on, off = note_events(make_track("lead", Note(0, 60, 1e-9, 100)))

    assert on[0] == "note_on"
    assert off[0] == "note_off"
    assert off[2] > on[2]


def test_perform_ends_a_song_whose_last_event_lands_on_its_last_beat() -> None:
    track = cc_track(ControlChange(0, 1, 0), ControlChange(4, 1, 9))  # The note ends at beat 1.
    song = make_song(track)

    performance = perform(song, track)

    assert performance.events[-1] == Control(96000, 1, 9)
    assert performance.samples == int((2.0 + TAIL_S) * SAMPLE_RATE)


# Controller changes


def test_linear_change_sends_one_event_per_value_where_the_line_rounds_to_it() -> None:
    rising = events_for(ControlChange(0, 1, 0, "linear"), ControlChange(1, 1, 4))
    falling = events_for(ControlChange(0, 1, 4, "linear"), ControlChange(1, 1, 0))

    assert rising == [(0, 1, 0), (3000, 1, 1), (9000, 1, 2), (15000, 1, 3), (21000, 1, 4)]
    assert falling == [(0, 1, 4), (3000, 1, 3), (9000, 1, 2), (15000, 1, 1), (21000, 1, 0)]


def test_full_ramp_sends_128_events_however_long_it_is() -> None:
    short = events_for(ControlChange(0, 1, 0, "linear"), ControlChange(1, 1, 127))
    long = events_for(ControlChange(0, 1, 0, "linear"), ControlChange(32, 1, 127))

    assert [value for _, _, value in short] == list(range(128))
    assert [value for _, _, value in long] == list(range(128))


def test_linear_change_ramps_to_the_next_change_of_its_own_controller() -> None:
    events = events_for(
        ControlChange(0, 1, 0, "linear"), ControlChange(0.5, 74, 9), ControlChange(1, 1, 2)
    )

    assert events == [(0, 1, 0), (0, 74, 9), (6000, 1, 1), (18000, 1, 2)]


def test_linear_change_can_ramp_into_another_linear_change() -> None:
    events = events_for(
        ControlChange(0, 1, 0, "linear"), ControlChange(1, 1, 2, "linear"), ControlChange(2, 1, 0)
    )

    assert events == [(0, 1, 0), (6000, 1, 1), (18000, 1, 2), (30000, 1, 1), (42000, 1, 0)]


def test_linear_first_change_holds_its_start_value_until_its_beat() -> None:
    events = events_for(ControlChange(2, 1, 0, "linear"), ControlChange(3, 1, 2))

    assert events == [(0, 1, 0), (54000, 1, 1), (66000, 1, 2)]


def test_first_change_holds_from_the_start_of_the_song() -> None:
    assert events_for(ControlChange(2, 1, 64)) == [(0, 1, 64)]
    assert events_for(ControlChange(2, 1, 64), ControlChange(3, 1, 0)) == [
        (0, 1, 64),
        (72000, 1, 0),
    ]


def test_last_change_at_a_sample_wins_and_other_controllers_keep_listed_order() -> None:
    assert events_for(ControlChange(0, 1, 10), ControlChange(0, 1, 20)) == [(0, 1, 20)]
    assert events_for(ControlChange(1, 74, 5), ControlChange(1, 1, 6)) == [
        (0, 74, 5),
        (0, 1, 6),
    ]


def test_a_ramp_shorter_than_a_sample_sends_one_event() -> None:
    events = events_for(ControlChange(0, 1, 0, "linear"), ControlChange(1e-9, 1, 127))

    assert events == [(0, 1, 127)]


def test_controller_change_sorts_between_note_off_and_note_on_at_one_sample() -> None:
    notes = (Note(0, 60, 1, 100), Note(1, 62, 1, 100))
    track = cc_track(ControlChange(0, 1, 0), ControlChange(1, 1, 90), notes=notes)

    assert event_fields(track) == [
        ("control_change", 1, 0),
        ("note_on", 60, 0),
        ("note_off", 60, 24000),
        ("control_change", 1, 24000),
        ("note_on", 62, 24000),
        ("note_off", 62, 48000),
    ]


def test_controller_value_reaches_the_event() -> None:
    (change, *_) = performed(cc_track(ControlChange(0, 74, 33))).events
    assert change == Control(0, 74, 33)


def test_performance_uses_a_controller_change_after_the_last_note() -> None:
    late = cc_track(ControlChange(6, 1, 0))
    early = cc_track(ControlChange(0.5, 1, 0))
    assert performed(late).samples == int((3.0 + TAIL_S) * SAMPLE_RATE)
    assert performed(early).samples == int((0.5 + TAIL_S) * SAMPLE_RATE)


# Constructors


@pytest.mark.parametrize(
    "build",
    [
        lambda: NoteOff(-1, 60),
        lambda: NoteOff(0, 128),
        lambda: NoteOff(True, 60),
        lambda: NoteOff(0.0, 60),  # ty: ignore[invalid-argument-type]
        lambda: NoteOn(0, -1, 100),
        lambda: NoteOn(0, 60, 0),
        lambda: NoteOn(0, 60, 128),
        lambda: NoteOn(0, 60, True),
        lambda: NoteOn(-1, 60, 100),
        lambda: Control(0, 120, 0),
        lambda: Control(0, -1, 0),
        lambda: Control(0, 1, 128),
        lambda: Control(0, 1, -1),
        lambda: Control(-1, 1, 0),
        lambda: Control(0, False, 0),
    ],
)
def test_event_constructor_refuses_values_out_of_range(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match=r"must be an integer|must be a number"):
        build()


def test_events_accept_their_bounds() -> None:
    assert NoteOff(0, 127).pitch == 127
    assert NoteOn(10**9, 0, 127).velocity == 127
    assert NoteOn(0, 0, 1).velocity == 1
    assert Control(0, 119, 127).control == 119


@pytest.mark.parametrize(
    "events",
    [
        (NoteOn(10, 60, 100), NoteOff(5, 60)),
        (NoteOn(0, 60, 100), NoteOff(0, 60)),
        (NoteOn(0, 60, 100), Control(0, 1, 0)),
        (Control(0, 1, 0), NoteOff(0, 60)),
    ],
    ids=[
        "later sample first",
        "note-on before note-off",
        "note-on before control",
        "control before note-off",
    ],
)
def test_performance_refuses_misordered_events(events: tuple[Event, ...]) -> None:
    with pytest.raises(ValueError, match="out of order"):
        Performance(100, events)


def test_performance_accepts_events_ordered_by_sample_then_kind() -> None:
    events = (NoteOff(0, 60), Control(0, 1, 0), NoteOn(0, 60, 100), NoteOff(99, 60))
    assert Performance(100, events).events == events
    assert Performance(1, ()).events == ()


@pytest.mark.parametrize(
    ("samples", "events", "message"),
    [
        (0, (), "must be an integer"),
        (True, (), "must be an integer"),
        (10.0, (), "must be an integer"),
        (100, [NoteOn(0, 60, 100)], "must be a tuple"),
        (100, ((0, 60),), "must be an event"),
        (100, (NoteOn(100, 60, 100),), "past the 100 samples"),
    ],
)
def test_performance_refuses_broken_rules(samples: int, events: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Performance(samples, events)  # ty: ignore[invalid-argument-type]


# Data


def test_performance_round_trips_through_data_and_json() -> None:
    performance = Performance(
        240000, (NoteOff(0, 59), Control(0, 1, 0), NoteOn(0, 60, 100), NoteOff(24000, 60))
    )

    data = performance_to_data(performance)

    assert data == {
        "samples": 240000,
        "events": [["off", 0, 59], ["cc", 0, 1, 0], ["on", 0, 60, 100], ["off", 24000, 60]],
    }
    assert performance_from_data(data) == performance
    assert performance_from_data(json.loads(json.dumps(data))) == performance


def test_performed_song_round_trips_through_data() -> None:
    track = cc_track(
        ControlChange(0, 1, 0, "linear"),
        ControlChange(2, 1, 127),
        notes=(Note(0, 60, 1, 100), Note(1.5, 62, 0.5, 90)),
    )
    performance = performed(track)
    assert performance_from_data(performance_to_data(performance)) == performance


@pytest.mark.parametrize(
    "data",
    [
        {"samples": 100, "events": [["up", 0, 60]]},
        {"samples": 100, "events": [["off", 0]]},
        {"samples": 100, "events": [["on", 0, 60]]},
        {"samples": 100, "events": [["cc", 0, 1, 0, 9]]},
        {"samples": 100, "events": [[]]},
        {"samples": 100, "events": ["off"]},
        {"samples": 100, "events": [["off", 0.0, 60]]},
        {"samples": 100, "events": [["off", 0, True]]},
        {"samples": 100, "events": [], "extra": 1},
        {"samples": 100},
        {"samples": "100", "events": []},
        {"samples": 100, "events": {}},
        {"samples": 100, "events": [["off", 50, 60], ["off", 10, 60]]},
        {"samples": 100, "events": [["off", 100, 60]]},
        [],
        None,
    ],
    ids=[
        "unknown tag",
        "too short",
        "note-on too short",
        "too long",
        "empty event",
        "event not a list",
        "float field",
        "bool field",
        "extra key",
        "missing key",
        "samples not an int",
        "events not a list",
        "misordered",
        "past the end",
        "not an object",
        "null",
    ],
)
def test_malformed_performance_data_raises_value_error(data: object) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 -- each case has its own message.
        performance_from_data(data)


# Pitch is a number from 0 to 127; whole pitches serialize exactly as before.


def test_note_events_take_a_fractional_pitch_and_store_a_whole_one_as_an_int() -> None:
    assert NoteOn(0, 60.5, 100).pitch == 60.5
    assert NoteOff(0, 60.5).pitch == 60.5
    whole = NoteOn(0, 60.0, 100)
    assert type(whole.pitch) is int
    assert whole == NoteOn(0, 60, 100)


@pytest.mark.parametrize("pitch", [-1, 128, float("nan"), True])
def test_note_events_reject_a_pitch_outside_the_numbers_from_0_to_127(pitch: object) -> None:
    with pytest.raises(ValueError, match="pitch must be a number from 0 to 127"):
        NoteOn(0, pitch, 100)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="pitch must be a number from 0 to 127"):
        NoteOff(0, pitch)  # ty: ignore[invalid-argument-type]


def test_a_performance_with_fractional_pitches_round_trips() -> None:
    track = make_track("lead", Note(0, 60.5, 1, 100), Note(1, 61.25, 0.5, 90))
    performance = performed(track)

    assert performance_from_data(performance_to_data(performance)) == performance
    pitches = [event.pitch for event in performance.events if isinstance(event, NoteOn | NoteOff)]
    assert pitches == [60.5, 60.5, 61.25, 61.25]


def test_an_integer_performance_serializes_exactly_as_before() -> None:
    """The literal was captured before pitch became a number."""
    track = Track(
        "lead",
        "Serum",
        (Note(0, 60, 0.5, 100), Note(0.5, 62, 0.25, 90)),
        cc=(ControlChange(0.25, 1, 10),),
    )

    data = performance_to_data(performed(track))

    assert json.dumps(data) == (
        '{"samples": 114000, "events": [["cc", 0, 1, 10], ["on", 0, 60, 100], '
        '["off", 12000, 60], ["on", 12000, 62, 90], ["off", 18000, 62]]}'
    )


def test_perform_for_refuses_a_fraction_unless_support_is_exactly_continuous() -> None:
    quarter = make_track("lead", Note(0, 60.5, 1, 100))
    song = make_song(quarter)
    # A third-party instrument might declare the plain string; only the enum member plays fractions.
    assert isinstance(perform_for(song, quarter, "semitones"), PitchProblem)  # ty: ignore[invalid-argument-type]
    assert isinstance(perform_for(song, quarter, "continuous"), PitchProblem)  # ty: ignore[invalid-argument-type]


def test_perform_for_refuses_a_fraction_only_on_a_semitone_instrument() -> None:
    whole = make_track("lead", Note(0, 60, 1, 100))
    quarter = make_track("lead", Note(2, 64.5, 1, 100), Note(0, 60, 1, 100), Note(1, 60.5, 1, 90))

    problem = perform_for(make_song(quarter), quarter, PitchSupport.SEMITONES)

    assert problem == PitchProblem(
        "plays only whole MIDI pitches; 2 notes are fractional, first at beat 1 with pitch 60.5"
    )
    for track, pitches in [
        (whole, PitchSupport.SEMITONES),
        (whole, PitchSupport.CONTINUOUS),
        (quarter, PitchSupport.CONTINUOUS),
    ]:
        song = make_song(track)
        assert perform_for(song, track, pitches) == perform(song, track)
