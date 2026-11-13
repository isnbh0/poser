"""Performances: what an instrument plays for one track, as events in samples."""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from poser.core.audio import sample_at, stem_samples
from poser.core.graph import Graph
from poser.core.jsonvalue import JsonValue, json_object
from poser.core.rules import (
    CONTROL_MAX,
    ControlChange,
    Timed,
    _plain,
    is_int,
    is_number,
    is_pitch,
    normal_pitch,
)
from poser.core.song import Song


class PitchSupport(StrEnum):
    """Which pitches an instrument plays, and so which performances it can receive."""

    SEMITONES = "semitones"  # Only whole pitches 0 to 127, as MIDI note numbers.
    CONTINUOUS = "continuous"  # Any pitch from 0 to 127.


def _check_int(owner: str, field: str, value: object, low: int, high: int | None) -> None:
    if not (is_int(value) and value >= low and (high is None or value <= high)):
        bounds = f"from {low} to {high}" if high is not None else f"of at least {low}"
        raise ValueError(f"{owner}.{field} must be an integer {bounds}, got {value!r}")


def _check_pitch(owner: str, value: object) -> None:
    if not is_pitch(value):
        raise ValueError(f"{owner}.pitch must be a number from 0 to 127, got {value!r}")


@dataclass(frozen=True)
class NoteOff:
    sample: int
    pitch: float  # 0 to 127; an integral pitch is stored as an int, any other as a float.

    def __post_init__(self) -> None:
        _check_int("NoteOff", "sample", self.sample, 0, None)
        _check_pitch("NoteOff", self.pitch)
        object.__setattr__(self, "pitch", normal_pitch(self.pitch))


@dataclass(frozen=True)
class Control:
    sample: int
    control: int  # 0 to CONTROL_MAX (119)
    value: int  # 0 to 127

    def __post_init__(self) -> None:
        _check_int("Control", "sample", self.sample, 0, None)
        _check_int("Control", "control", self.control, 0, CONTROL_MAX)
        _check_int("Control", "value", self.value, 0, 127)


@dataclass(frozen=True)
class NoteOn:
    sample: int
    pitch: float  # 0 to 127; an integral pitch is stored as an int, any other as a float.
    velocity: int  # 1 to 127

    def __post_init__(self) -> None:
        _check_int("NoteOn", "sample", self.sample, 0, None)
        _check_pitch("NoteOn", self.pitch)
        _check_int("NoteOn", "velocity", self.velocity, 1, 127)
        object.__setattr__(self, "pitch", normal_pitch(self.pitch))


type Event = NoteOff | Control | NoteOn


def _rank(event: Event) -> int:
    """Return an event's place among events at one sample: note-offs first, then controller
    changes, then note-ons, so a repeated pitch retriggers."""
    match event:
        case NoteOff():
            return 0
        case Control():
            return 1
        case NoteOn():
            return 2


@dataclass(frozen=True)
class Performance:
    samples: int  # The render length; every event's sample is below it.
    events: tuple[Event, ...]

    def __post_init__(self) -> None:
        _check_int("Performance", "samples", self.samples, 1, None)
        if not isinstance(self.events, tuple):
            raise ValueError(f"Performance.events must be a tuple, got {self.events!r}")
        previous: tuple[int, int] | None = None
        for index, event in enumerate(cast("tuple[object, ...]", self.events)):
            if not isinstance(event, NoteOff | Control | NoteOn):
                raise ValueError(f"Performance.events[{index}] must be an event, got {event!r}")
            if event.sample >= self.samples:
                raise ValueError(
                    f"Performance.events[{index}] is at sample {event.sample}, "
                    f"past the {self.samples} samples"
                )
            order = (event.sample, _rank(event))
            if previous is not None and order < previous:
                raise ValueError(
                    f"Performance.events[{index}] is out of order: events are ordered by "
                    "sample, then note-offs, controller changes, and note-ons"
                )
            previous = order


def _ramp_events(song: Song | Graph, start: ControlChange, end: ControlChange) -> list[Control]:
    """Return one event per value along a linear segment, at the beat where the line rounds to it.

    The segment's start value is the start entry's own event, so values run from one past it.
    """
    span = abs(end.value - start.value)
    direction = 1 if end.value > start.value else -1
    return [
        Control(
            sample_at(song, start.beat + (step - 0.5) / span * (end.beat - start.beat)),
            start.control,
            start.value + direction * step,
        )
        for step in range(1, span + 1)
    ]


def _control_events(song: Song | Graph, timed: Timed) -> list[Control]:
    """Return a member's controller changes as events in time order.

    Each controller holds its first value from the start of the song, as automation does in a
    DAW, so a change at a note's start is already in place when the note begins. Linear segments
    expand to one event per value. Per controller, only the last event at a sample is kept, and an
    event that repeats the controller's current value is dropped.
    """
    events: list[Control] = []
    seen: set[int] = set()
    for index, change in enumerate(timed.cc):
        events.append(Control(sample_at(song, change.beat), change.control, change.value))
        if change.control not in seen:
            seen.add(change.control)
            events.append(Control(0, change.control, change.value))
        if change.shape == "linear":
            # The constructor guarantees a later change to the same controller.
            target = next(c for c in timed.cc[index + 1 :] if c.control == change.control)
            events.extend(_ramp_events(song, change, target))
    events.sort(key=lambda event: event.sample)

    last_at_sample = {
        (event.sample, event.control): position for position, event in enumerate(events)
    }
    kept: list[Control] = []
    current: dict[int, int] = {}
    for position, event in enumerate(events):
        if (
            last_at_sample[(event.sample, event.control)] != position
            or current.get(event.control) == event.value
        ):
            continue
        current[event.control] = event.value
        kept.append(event)
    return kept


def perform(song: Song | Graph, timed: Timed) -> Performance:
    """Return what the member's instrument plays, over the song's or graph's stem length.

    Times snap to whole samples, so float error, as in beats 0.1 + 0.2 against 0.3, cannot place
    a note-off just after the note-on it should precede. A note ends at least one sample after it
    starts.
    """
    events: list[Event] = []
    for note in timed.notes:
        start = sample_at(song, note.beat)
        end = max(sample_at(song, note.beat + note.length), start + 1)
        events.append(NoteOn(start, note.pitch, note.velocity))
        events.append(NoteOff(end, note.pitch))
    events.extend(_control_events(song, timed))
    events.sort(key=lambda event: (event.sample, _rank(event)))
    return Performance(stem_samples(song), tuple(events))


@dataclass(frozen=True)
class PitchProblem:
    """Why an instrument cannot play a member's notes."""

    detail: str  # Names the first offending note's beat and pitch, and how many notes are affected.


def perform_for(
    song: Song | Graph, timed: Timed, pitches: PitchSupport
) -> Performance | PitchProblem:
    """Return what the member's instrument plays, or the problem when it cannot play the pitches.

    The render path builds every track's performance here, so an instrument declaring
    `SEMITONES` receives only whole pitches. `Performance` is public, so code outside the render
    path can still build a fractional one; the instrument's own conversion is the backstop.
    """
    if pitches is PitchSupport.SEMITONES:
        fractional = [note for note in timed.notes if not isinstance(note.pitch, int)]
        if fractional:
            first = min(fractional, key=lambda note: note.beat)
            count = len(fractional)
            notes = "1 note is" if count == 1 else f"{count} notes are"
            return PitchProblem(
                f"plays only whole MIDI pitches; {notes} fractional, first at beat "
                f"{_plain(first.beat)} with pitch {first.pitch}"
            )
    return perform(song, timed)


def _event_to_data(event: Event) -> JsonValue:
    match event:
        case NoteOff(sample, pitch):
            return ["off", sample, pitch]
        case Control(sample, control, value):
            return ["cc", sample, control, value]
        case NoteOn(sample, pitch, velocity):
            return ["on", sample, pitch, velocity]


def performance_to_data(performance: Performance) -> JsonValue:
    """Return a performance as JSON data, the inverse of `performance_from_data`."""
    return {
        "samples": performance.samples,
        "events": [_event_to_data(event) for event in performance.events],
    }


def _event_from_data(value: object) -> Event:
    if not isinstance(value, list) or not value:
        raise ValueError(f"an event must be a non-empty list, got {value!r}")
    tag, *fields = cast("list[object]", value)
    # A pitch is any number; every other field is an integer.
    pitch_at = 1 if tag in ("off", "on") else None
    if not all(
        is_number(field) if index == pitch_at else is_int(field)
        for index, field in enumerate(fields)
    ):
        raise ValueError(
            f"an event's pitch must be a number and its other fields integers, got {value!r}"
        )
    numbers = cast("list[int]", fields)
    match tag, numbers:
        case "off", [sample, pitch]:
            return NoteOff(sample, pitch)
        case "cc", [sample, control, level]:
            return Control(sample, control, level)
        case "on", [sample, pitch, velocity]:
            return NoteOn(sample, pitch, velocity)
        case _:
            raise ValueError(f"unknown event {value!r}")


def performance_from_data(value: object) -> Performance:
    """Return the performance that `performance_to_data` gave as `value`, or raise ValueError."""
    fields = json_object(value)
    if fields is None or set(fields) != {"samples", "events"}:
        raise ValueError(f"a performance must be an object with samples and events, got {value!r}")
    samples, events = fields["samples"], fields["events"]
    if not is_int(samples) or not isinstance(events, list):
        raise ValueError(f"malformed performance: {value!r}")
    return Performance(samples, tuple(_event_from_data(event) for event in events))
