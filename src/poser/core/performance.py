"""Performances: what an instrument plays for one track, as events in samples."""

from dataclasses import dataclass
from typing import cast

from poser.core.audio import sample_at, stem_samples
from poser.core.jsonvalue import JsonValue, json_object
from poser.core.song import CONTROL_MAX, ControlChange, Song, Track, is_int


def _check_int(owner: str, field: str, value: object, low: int, high: int | None) -> None:
    if not (is_int(value) and value >= low and (high is None or value <= high)):
        bounds = f"from {low} to {high}" if high is not None else f"of at least {low}"
        raise ValueError(f"{owner}.{field} must be an integer {bounds}, got {value!r}")


@dataclass(frozen=True)
class NoteOff:
    sample: int
    pitch: int  # 0 to 127

    def __post_init__(self) -> None:
        _check_int("NoteOff", "sample", self.sample, 0, None)
        _check_int("NoteOff", "pitch", self.pitch, 0, 127)


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
    pitch: int  # 0 to 127
    velocity: int  # 1 to 127

    def __post_init__(self) -> None:
        _check_int("NoteOn", "sample", self.sample, 0, None)
        _check_int("NoteOn", "pitch", self.pitch, 0, 127)
        _check_int("NoteOn", "velocity", self.velocity, 1, 127)


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


def _ramp_events(song: Song, start: ControlChange, end: ControlChange) -> list[Control]:
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


def _control_events(song: Song, track: Track) -> list[Control]:
    """Return a track's controller changes as events in time order.

    Each controller holds its first value from the start of the song, as automation does in a
    DAW, so a change at a note's start is already in place when the note begins. Linear segments
    expand to one event per value. Per controller, only the last event at a sample is kept, and an
    event that repeats the controller's current value is dropped.
    """
    events: list[Control] = []
    seen: set[int] = set()
    for index, change in enumerate(track.cc):
        events.append(Control(sample_at(song, change.beat), change.control, change.value))
        if change.control not in seen:
            seen.add(change.control)
            events.append(Control(0, change.control, change.value))
        if change.shape == "linear":
            # `Track` guarantees a later change to the same controller.
            target = next(c for c in track.cc[index + 1 :] if c.control == change.control)
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


def perform(song: Song, track: Track) -> Performance:
    """Return what the track's instrument plays, over the song's stem length.

    Times snap to whole samples, so float error, as in beats 0.1 + 0.2 against 0.3, cannot place
    a note-off just after the note-on it should precede. A note ends at least one sample after it
    starts.
    """
    events: list[Event] = []
    for note in track.notes:
        start = sample_at(song, note.beat)
        end = max(sample_at(song, note.beat + note.length), start + 1)
        events.append(NoteOn(start, note.pitch, note.velocity))
        events.append(NoteOff(end, note.pitch))
    events.extend(_control_events(song, track))
    events.sort(key=lambda event: (event.sample, _rank(event)))
    return Performance(stem_samples(song), tuple(events))


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
    if not all(is_int(field) for field in fields):
        raise ValueError(f"an event's fields must be integers, got {value!r}")
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
