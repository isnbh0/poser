"""The rules every song and graph is checked against, and the timed values they govern."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal, Protocol, TypeGuard, cast

from poser.core.names import Identifier, TrackName
from poser.core.sounds import parse_sound_ref

TRACK_NAME = TrackName.PATTERN

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
    pitch: int
    length: float
    velocity: int

    def __post_init__(self) -> None:
        values = {
            "beat": self.beat,
            "pitch": self.pitch,
            "length": self.length,
            "velocity": self.velocity,
        }
        if problems := _note_problems(values, "Note"):
            raise SongError(problems)


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
        if problems := _control_change_problems(values, "ControlChange"):
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


def _is_int_in_range(value: object, low: int, high: int) -> bool:
    return is_int(value) and low <= value <= high


def _at(where: str, field: str) -> str:
    """Return the location of a field below `where`, or the field alone at the top level."""
    return f"{where}.{field}" if where else field


# Each class's rules live in one function, used by its constructor (locations such as
# `Note.pitch`) and by `parse_song` (locations such as `tracks[0].notes[2].pitch`). A function
# checks only the fields present in `values`, so the parser can leave out missing keys. Songs and
# graphs share them, so a value never passes one rule as a track and another as a node.


def _note_problems(values: Mapping[str, object], where: str) -> list[str]:
    beat, pitch = values.get("beat"), values.get("pitch")
    length, velocity = values.get("length"), values.get("velocity")
    problems: list[str] = []
    if "beat" in values and not (is_number(beat) and beat >= 0):
        problems.append(f"{where}.beat: must be a number that is not negative, got {beat!r}")
    if "pitch" in values and not _is_int_in_range(pitch, 0, 127):
        problems.append(f"{where}.pitch: must be an integer from 0 to 127, got {pitch!r}")
    if "length" in values and not (is_number(length) and length > 0):
        problems.append(f"{where}.length: must be a positive number, got {length!r}")
    if "velocity" in values and not _is_int_in_range(velocity, 1, 127):
        problems.append(f"{where}.velocity: must be an integer from 1 to 127, got {velocity!r}")
    return problems


def is_shape(value: object) -> TypeGuard[Shape]:
    return value in SHAPES


def _control_change_problems(values: Mapping[str, object], where: str) -> list[str]:
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


def _track_field_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check a track's name, sound, and gain. `_track_problems` checks the rest."""
    name, sound, gain = values.get("name"), values.get("sound"), values.get("gain")
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
    return problems


def _is_sound_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_sound_ref(value)
    except ValueError:
        return False
    return True


def _notes_problems(notes: object, where: str) -> list[str]:
    """Check that a track's notes are a non-empty list, not the notes themselves."""
    if not isinstance(notes, list | tuple) or not notes:
        return [f"{where}.notes: must list at least one note"]
    return []


def _cc_problems(cc: object, where: str) -> list[str]:
    """Check that a track's controller changes are a list, not the changes themselves."""
    if not isinstance(cc, list | tuple):
        return [f"{where}.cc: must be a list of controller changes, got {cc!r}"]
    return []


def _cc_order_problems(
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


def _track_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check every field of a track built in code, whose notes and changes are objects."""
    problems = _track_field_problems(values, where)
    notes = values["notes"]
    problems += _notes_problems(notes, where)
    if isinstance(notes, list | tuple):
        problems += [
            f"{where}.notes[{index}]: must be a Note, got {note!r}"
            for index, note in enumerate(cast("Sequence[object]", notes))
            if not isinstance(note, Note)
        ]
    cc = values["cc"]
    problems += _cc_problems(cc, where)
    if isinstance(cc, list | tuple):
        items = cast("Sequence[object]", cc)
        problems += [
            f"{where}.cc[{index}]: must be a ControlChange, got {change!r}"
            for index, change in enumerate(items)
            if not isinstance(change, ControlChange)
        ]
        changes = [(i, c) for i, c in enumerate(items) if isinstance(c, ControlChange)]
        problems += _cc_order_problems(changes, where, complete=len(changes) == len(items))
    return problems


def _song_field_problems(
    values: Mapping[str, object], where: str, *, group: str = "tracks", noun: str = "track"
) -> list[str]:
    """Check a song's bpm and that its members are a non-empty list, not the members themselves."""
    bpm, items = values.get("bpm"), values.get(group)
    problems: list[str] = []
    if "bpm" in values and not (is_number(bpm) and bpm > 0):
        problems.append(f"{_at(where, 'bpm')}: must be a positive number, got {bpm!r}")
    if group in values and (not isinstance(items, list | tuple) or not items):
        problems.append(f"{_at(where, group)}: must list at least one {noun}")
    return problems


def _duplicate_name_problems(
    values: Sequence[Named], where: str, *, group: str = "tracks", noun: str = "track"
) -> list[str]:
    # Names become filenames, and macOS treats names differing only in case as the same file.
    problems: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value.name.casefold() in seen:
            problems.append(f"{_at(where, group)}: duplicate {noun} name {value.name!r}")
        seen.add(value.name.casefold())
    return problems


def _last_beat(values: Sequence[Timed]) -> float:
    note_ends = [note.beat + note.length for value in values for note in value.notes]
    return max([*note_ends, *(change.beat for value in values for change in value.cc)])


def _length_problems(bpm: float, values: Sequence[Timed], where: str) -> list[str]:
    """Check that a song or graph with a valid bpm and valid members ends within `MAX_SONG_S`."""
    try:
        seconds = float(_last_beat(values) * 60 / bpm)
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
