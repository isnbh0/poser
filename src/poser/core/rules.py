"""The rules every song and graph is checked against, the timed values they govern, and the
plain-data form those timed values share, so song and graph records write them alike."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal, NotRequired, Protocol, TypedDict, TypeGuard, cast

from poser.core.names import Identifier, NodeName, TrackName
from poser.core.sounds import parse_sound_ref

TRACK_NAME = TrackName.PATTERN
NODE_NAME = NodeName.PATTERN

# A track's level in decibels, applied at mixdown.
GAIN_MIN_DB, GAIN_MAX_DB = -60, 12

# MIDI controllers 120 to 127 are channel mode messages, such as All Notes Off, not controls.
CONTROL_MAX = 119
# `step` holds a value until the next change of its controller; `linear` ramps to that change.
type Shape = Literal["step", "linear"]
SHAPES: tuple[Shape, ...] = ("step", "linear")

# The longest a song may run, to its last beat. Every frame count of a valid song fits in an int.
MAX_SONG_S = 3600.0


class SongError(Exception):
    """A song could not be read or failed validation. Holds one message per problem."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


# Every song object is valid: its constructor raises `SongError` listing every problem.


@dataclass(frozen=True)
class Note:
    beat: float
    # A number on the MIDI scale: 69 is A4 at 440 Hz, and 1 is a twelve-tone equal-tempered
    # semitone. An integral pitch is stored as an int, any other as a float.
    pitch: float
    length: float
    velocity: int

    def __post_init__(self) -> None:
        values = {
            "beat": self.beat,
            "pitch": self.pitch,
            "length": self.length,
            "velocity": self.velocity,
        }
        if problems := note_problems(values, "Note"):
            raise SongError(problems)
        object.__setattr__(self, "pitch", normal_pitch(self.pitch))


@dataclass(frozen=True)
class ControlChange:
    beat: float
    control: int
    value: int
    shape: Shape = "step"

    def __post_init__(self) -> None:
        values = {
            "beat": self.beat,
            "control": self.control,
            "value": self.value,
            "shape": self.shape,
        }
        if problems := control_change_problems(values, "ControlChange"):
            raise SongError(problems)


class Timed(Protocol):
    """A song's or graph's member that carries notes and controller changes on the beat grid."""

    @property
    def notes(self) -> tuple[Note, ...]: ...

    @property
    def cc(self) -> tuple[ControlChange, ...]: ...


class Named(Protocol):
    """A member whose name is checked once per container, so same-case names cannot both exist."""

    @property
    def name(self) -> str: ...


def is_int(value: object) -> TypeGuard[int]:
    """Return whether the value is an int, excluding bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value: object) -> TypeGuard[int | float]:
    """Return whether the value is a finite int or float that fits in a float, excluding bool."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    # Rendering converts numbers to floats, so an int too large for a float is not a number.
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def is_pitch(value: object) -> TypeGuard[int | float]:
    """Return whether the value is a pitch: a number from 0 to 127, excluding bool."""
    return is_number(value) and 0 <= value <= 127


def normal_pitch(pitch: float) -> float:
    """Return a pitch in its one representation: an integral pitch as an int, any other as a
    float, so `60.0` and `60` are one value and `isinstance(pitch, int)` means a whole pitch.
    """
    return int(pitch) if float(pitch).is_integer() else float(pitch)


def _is_int_in_range(value: object, low: int, high: int) -> bool:
    return is_int(value) and low <= value <= high


def field_path(where: str, field: str) -> str:
    """Return the location of a field below `where`, or the field alone at the top level."""
    return f"{where}.{field}" if where else field


# Each class's rules live in one function, used by its constructor (locations such as
# `Note.pitch`) and by `parse_song` (locations such as `tracks[0].notes[2].pitch`). A function
# checks only the fields present in `values`, so the parser can leave out missing keys. Songs and
# graphs share them, so a value never passes one rule as a track and another as a node.


def note_problems(values: Mapping[str, object], where: str) -> list[str]:
    beat, pitch = values.get("beat"), values.get("pitch")
    length, velocity = values.get("length"), values.get("velocity")
    problems: list[str] = []
    if "beat" in values and not (is_number(beat) and beat >= 0):
        problems.append(f"{where}.beat: must be a number that is not negative, got {beat!r}")
    if "pitch" in values and not is_pitch(pitch):
        problems.append(f"{where}.pitch: must be a number from 0 to 127, got {pitch!r}")
    if "length" in values and not (is_number(length) and length > 0):
        problems.append(f"{where}.length: must be a positive number, got {length!r}")
    if "velocity" in values and not _is_int_in_range(velocity, 1, 127):
        problems.append(f"{where}.velocity: must be an integer from 1 to 127, got {velocity!r}")
    return problems


def _bound_problems(value: object, where: str) -> list[str]:
    """Check one bound of a span: a finite number."""
    if is_number(value):
        return []
    return [f"{where}: must be a finite number, got {value!r}"]


def span_problems(start: object, end: object, where: str, start_field: str) -> list[str]:
    """Check a span of beats: finite bounds with `0 <= start < end`.

    Every span type checks its bounds here; `start_field` names its start, such as `beat`.
    """
    problems = _bound_problems(start, field_path(where, start_field)) + _bound_problems(
        end, field_path(where, "end")
    )
    if problems or not (is_number(start) and is_number(end)):
        return problems
    if start < 0:
        problems.append(f"{field_path(where, start_field)}: must not be negative, got {start!r}")
    # Compared as stored: two ints can round to one float.
    if float(end) <= float(start):
        problems.append(
            f"{field_path(where, 'end')}: must be after {start_field} {start!r}, got {end!r}"
        )
    return problems


def line_problems(text: object, where: str) -> list[str]:
    """Check a label: one non-empty line of text without leading or trailing spaces."""
    if not isinstance(text, str):
        return [
            f"{where}: must be text; quote numbers and words like yes or no in YAML, got {text!r}"
        ]
    if not text or "\r" in text or "\n" in text or text != text.strip():
        return [
            f"{where}: must be one non-empty line without leading or trailing spaces, got {text!r}"
        ]
    return []


def is_shape(value: object) -> TypeGuard[Shape]:
    return value in SHAPES


def control_change_problems(values: Mapping[str, object], where: str) -> list[str]:
    beat, control = values.get("beat"), values.get("control")
    cc_value, shape = values.get("value"), values.get("shape")
    problems: list[str] = []
    if "beat" in values and not (is_number(beat) and beat >= 0):
        problems.append(f"{where}.beat: must be a number that is not negative, got {beat!r}")
    if "control" in values and not _is_int_in_range(control, 0, CONTROL_MAX):
        problems.append(
            f"{where}.control: must be an integer from 0 to {CONTROL_MAX}, got {control!r}"
        )
    if "value" in values and not _is_int_in_range(cc_value, 0, 127):
        problems.append(f"{where}.value: must be an integer from 0 to 127, got {cc_value!r}")
    if "shape" in values and not is_shape(shape):
        problems.append(f"{where}.shape: must be step or linear, got {shape!r}")
    return problems


def track_field_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check a track's name, sound, gain, and `pitched`. `track_problems` checks the rest."""
    name, sound, gain = values.get("name"), values.get("sound"), values.get("gain")
    pitched = values.get("pitched")
    problems: list[str] = []
    if "name" in values and not (isinstance(name, str) and TRACK_NAME.fullmatch(name)):
        problems.append(
            f"{where}.name: must contain only letters, digits, '_', and '-', got {name!r}"
        )
    if "sound" in values and not _is_sound_ref(sound):
        problems.append(
            f"{where}.sound: must be <instrument> or <instrument>:<name>, with an instrument "
            f"name matching {Identifier.PATTERN.pattern}, got {sound!r}"
        )
    if "gain" in values and not (is_number(gain) and GAIN_MIN_DB <= gain <= GAIN_MAX_DB):
        problems.append(
            f"{where}.gain: must be a number of decibels from {GAIN_MIN_DB} to {GAIN_MAX_DB}, "
            f"got {gain!r}"
        )
    if "pitched" in values and not isinstance(pitched, bool):
        problems.append(f"{where}.pitched: must be true or false, got {pitched!r}")
    return problems


def unpitched_problems(notes: Sequence[tuple[int, Note]], where: str) -> list[str]:
    """Check an unpitched track's notes, given with their indexes: each pitch selects a key of
    the track's sound, such as a drum in a kit, and a key number has no fraction.
    """
    return [
        (
            f"{where}.notes[{index}].pitch: an unpitched track's pitches are key numbers and "
            f"must be whole, got {note.pitch!r}"
        )
        for index, note in notes
        if not isinstance(note.pitch, int)
    ]


def _is_sound_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_sound_ref(value)
    except ValueError:
        return False
    return True


def notes_problems(notes: object, where: str) -> list[str]:
    """Check that a track's notes are a non-empty list, not the notes themselves."""
    if not isinstance(notes, list | tuple) or not notes:
        return [f"{where}.notes: must list at least one note"]
    return []


def cc_problems(cc: object, where: str) -> list[str]:
    """Check that a track's controller changes are a list, not the changes themselves."""
    if not isinstance(cc, list | tuple):
        return [f"{where}.cc: must be a list of controller changes, got {cc!r}"]
    return []


def cc_order_problems(
    changes: Sequence[tuple[int, ControlChange]], where: str, *, complete: bool
) -> list[str]:
    """Check that controller changes are in time order and that each ramp has a target.

    `changes` pairs each valid change with its index. When `complete` is false, some entries were
    invalid and any of them could be a ramp's target, so ramps are not checked.
    """
    problems: list[str] = []
    for (_, previous), (index, change) in pairwise(changes):
        if change.beat < previous.beat:
            problems.append(
                f"{where}.cc[{index}].beat: must not be earlier than the previous change at beat "
                f"{previous.beat}, got {change.beat}"
            )
    for position, (index, change) in enumerate(changes if complete else []):
        if change.shape != "linear":
            continue
        following = (c for _, c in changes[position + 1 :] if c.control == change.control)
        target = next(following, None)
        if target is None or target.beat <= change.beat:
            problems.append(
                f"{where}.cc[{index}].shape: linear needs a later change to control "
                f"{change.control}"
            )
    return problems


def track_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check every field of a track built in code, whose notes and changes are objects."""
    problems = track_field_problems(values, where)
    notes = values["notes"]
    problems += notes_problems(notes, where)
    if isinstance(notes, list | tuple):
        items = cast("Sequence[object]", notes)
        problems += [
            f"{where}.notes[{index}]: must be a Note, got {note!r}"
            for index, note in enumerate(items)
            if not isinstance(note, Note)
        ]
        if values.get("pitched") is False:
            valid = [(i, note) for i, note in enumerate(items) if isinstance(note, Note)]
            problems += unpitched_problems(valid, where)
    cc = values["cc"]
    problems += cc_problems(cc, where)
    if isinstance(cc, list | tuple):
        items = cast("Sequence[object]", cc)
        problems += [
            f"{where}.cc[{index}]: must be a ControlChange, got {change!r}"
            for index, change in enumerate(items)
            if not isinstance(change, ControlChange)
        ]
        changes = [(i, c) for i, c in enumerate(items) if isinstance(c, ControlChange)]
        problems += cc_order_problems(changes, where, complete=len(changes) == len(items))
    if "effects" in values:
        problems += effects_problems(values["effects"], where)
    return problems


def effects_problems(effects: object, where: str) -> list[str]:
    """Check that a track's effects are a list of recipe names."""
    if not isinstance(effects, list | tuple):
        return [f"{where}.effects: must be a list of effect recipe names, got {effects!r}"]
    return [
        f"{where}.effects[{index}]: must be non-empty text, got {entry!r}"
        for index, entry in enumerate(effects)
        if not (isinstance(entry, str) and entry)
    ]


def song_field_problems(
    values: Mapping[str, object], where: str, *, group: str = "tracks", noun: str = "track"
) -> list[str]:
    """Check a song's bpm and that its members are a non-empty list, not the members themselves."""
    bpm, items = values.get("bpm"), values.get(group)
    problems: list[str] = []
    if "bpm" in values and not (is_number(bpm) and bpm > 0):
        problems.append(f"{field_path(where, 'bpm')}: must be a positive number, got {bpm!r}")
    if group in values and (not isinstance(items, list | tuple) or not items):
        problems.append(f"{field_path(where, group)}: must list at least one {noun}")
    return problems


def duplicate_name_problems(
    values: Sequence[Named], where: str, *, group: str = "tracks", noun: str = "track"
) -> list[str]:
    # Names become filenames, and macOS treats names differing only in case as the same file.
    problems: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value.name.casefold() in seen:
            problems.append(f"{field_path(where, group)}: duplicate {noun} name {value.name!r}")
        seen.add(value.name.casefold())
    return problems


def last_beat(values: Sequence[Timed]) -> float:
    """Return the latest note end or controller change, or 0 when the values hold neither."""
    note_ends = [note.beat + note.length for value in values for note in value.notes]
    return max([*note_ends, *(change.beat for value in values for change in value.cc)], default=0)


def length_problems(bpm: float, values: Sequence[Timed], where: str) -> list[str]:
    """Check that a song or graph with a valid bpm and valid members ends within `MAX_SONG_S`."""
    try:
        seconds = float(last_beat(values) * 60 / bpm)
    except OverflowError:
        seconds = math.inf
    if seconds <= MAX_SONG_S:
        return []
    return [
        (
            f"{where}: must end within {MAX_SONG_S:.0f} seconds, but its last beat is at "
            f"{seconds:.0f} seconds"
        )
    ]


# The plain-data form of the timed values, shared by the song and graph records so both write a
# note in one shape and one spelling of its numbers.


class NoteData(TypedDict):
    """A note's plain-data form, the song format's own shape for it."""

    beat: float
    pitch: float
    length: float
    velocity: int


class ControlData(TypedDict):
    """A controller change's plain-data form; `shape` is present only when it is not `step`."""

    beat: float
    control: int
    value: int
    shape: NotRequired[Shape]


def plain_number(value: float) -> float:
    """Return a number in one spelling, so equal values write equal text: an integral number as an
    int, any other as a float.
    """
    return int(value) if float(value).is_integer() else float(value)


def note_to_data(note: Note) -> NoteData:
    return {
        "beat": plain_number(note.beat),
        "pitch": note.pitch,
        "length": plain_number(note.length),
        "velocity": note.velocity,
    }


def control_to_data(change: ControlChange) -> ControlData:
    data: ControlData = {
        "beat": plain_number(change.beat),
        "control": change.control,
        "value": change.value,
        "shape": change.shape,
    }
    if change.shape == "step":
        del data["shape"]
    return data
