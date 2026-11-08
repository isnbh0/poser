"""Songs: the composition as values, independent of how it is stored or rendered."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal, NotRequired, TypedDict, TypeGuard, cast

from poser.core.names import Identifier, TrackName
from poser.core.sounds import SoundRef, parse_sound_ref

TRACK_NAME = TrackName.PATTERN

SONG_KEYS = {"bpm", "tracks"}
TRACK_KEYS = {"name", "sound", "notes"}
OPTIONAL_TRACK_KEYS = {"gain", "cc"}
NOTE_KEYS = {"beat", "pitch", "length", "velocity"}
CC_KEYS = {"beat", "control", "value"}
OPTIONAL_CC_KEYS = {"shape"}

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


@dataclass(frozen=True)
class Track:
    name: str
    sound: str
    notes: tuple[Note, ...]
    gain: float = 0.0
    cc: tuple[ControlChange, ...] = ()

    def __post_init__(self) -> None:
        values = {
            "name": self.name,
            "sound": self.sound,
            "gain": self.gain,
            "notes": self.notes,
            "cc": self.cc,
        }
        if problems := _track_problems(values, "Track"):
            raise SongError(problems)
        # A list becomes a tuple, so a track built from lists equals one loaded from YAML.
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(self, "cc", tuple(self.cc))
        object.__setattr__(self, "gain", float(self.gain))

    @property
    def sound_ref(self) -> SoundRef:
        """The track's sound as a reference; the constructor checked that it parses."""
        return parse_sound_ref(self.sound)

    @property
    def track_name(self) -> TrackName:
        """The track's name as a name; the constructor checked that it is one."""
        return TrackName(self.name)


@dataclass(frozen=True)
class Song:
    bpm: float
    tracks: tuple[Track, ...]

    def __post_init__(self) -> None:
        if problems := _song_problems({"bpm": self.bpm, "tracks": self.tracks}, "Song"):
            raise SongError(problems)
        object.__setattr__(self, "tracks", tuple(self.tracks))

    def seconds(self, beat: float) -> float:
        return beat * 60 / self.bpm


def song_last_beat(song: Song) -> float:
    """Return the latest note end or controller change across tracks."""
    return _last_beat(song.tracks)


def _last_beat(tracks: Sequence[Track]) -> float:
    note_ends = [note.beat + note.length for track in tracks for note in track.notes]
    return max([*note_ends, *(change.beat for track in tracks for change in track.cc)])


class NoteData(TypedDict):
    beat: float
    pitch: int
    length: float
    velocity: int


class ControlData(TypedDict):
    beat: float
    control: int
    value: int
    shape: NotRequired[Shape]


class TrackData(TypedDict):
    name: str
    sound: str
    gain: NotRequired[float]
    notes: list[NoteData]
    cc: NotRequired[list[ControlData]]


class SongData(TypedDict):
    bpm: float
    tracks: list[TrackData]


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
# checks only the fields present in `values`, so the parser can leave out missing keys.


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


def _song_field_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check a song's bpm and that its tracks are a non-empty list, not the tracks themselves."""
    bpm, tracks = values.get("bpm"), values.get("tracks")
    problems: list[str] = []
    if "bpm" in values and not (is_number(bpm) and bpm > 0):
        problems.append(f"{_at(where, 'bpm')}: must be a positive number, got {bpm!r}")
    if "tracks" in values and (not isinstance(tracks, list | tuple) or not tracks):
        problems.append(f"{_at(where, 'tracks')}: must list at least one track")
    return problems


def _duplicate_name_problems(tracks: Sequence[Track], where: str) -> list[str]:
    # Names become filenames, and macOS treats names differing only in case as the same file.
    problems: list[str] = []
    seen: set[str] = set()
    for track in tracks:
        if track.name.casefold() in seen:
            problems.append(f"{_at(where, 'tracks')}: duplicate track name {track.name!r}")
        seen.add(track.name.casefold())
    return problems


def _length_problems(bpm: float, tracks: Sequence[Track], where: str) -> list[str]:
    """Check that a song with a valid bpm and valid tracks ends within `MAX_SONG_S`."""
    try:
        seconds = float(_last_beat(tracks) * 60 / bpm)
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


def _song_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check every field of a song built in code, whose tracks are objects."""
    problems = _song_field_problems(values, where)
    tracks = values["tracks"]
    if isinstance(tracks, list | tuple):
        items = cast("Sequence[object]", tracks)
        problems += [
            f"{_at(where, 'tracks')}[{index}]: must be a Track, got {track!r}"
            for index, track in enumerate(items)
            if not isinstance(track, Track)
        ]
        problems += _duplicate_name_problems([t for t in items if isinstance(t, Track)], where)
        bpm = values["bpm"]
        if not problems and is_number(bpm):
            problems += _length_problems(bpm, [t for t in items if isinstance(t, Track)], where)
    return problems


def _mapping(value: object) -> Mapping[object, object] | None:
    """Return loaded YAML as a mapping of untyped keys and values, or None when it is not one."""
    return value if isinstance(value, dict) else None


def _present(data: Mapping[object, object], keys: set[str]) -> dict[str, object]:
    """Return the values of the song-format keys present in loaded data."""
    return {key: data[key] for key in keys if key in data}


def _check_keys(
    data: Mapping[object, object],
    required: set[str],
    where: str,
    problems: list[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> None:
    prefix = f"{where}: " if where else ""
    problems.extend(
        f"{prefix}unknown key {key!r}"
        for key in data
        if key not in required and key not in optional
    )
    problems.extend(f"{prefix}missing key {key!r}" for key in sorted(required - set(data)))


def _parse_note(value: object, where: str, problems: list[str]) -> Note | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, NOTE_KEYS, where, problems)

    field_problems = _note_problems(_present(data, NOTE_KEYS), where)
    problems += field_problems
    beat, pitch = data.get("beat"), data.get("pitch")
    length, velocity = data.get("length"), data.get("velocity")
    # These fail only when a problem was reported. A missing key reads as None.
    if field_problems or not (
        is_number(beat) and is_int(pitch) and is_number(length) and is_int(velocity)
    ):
        return None
    return Note(beat=beat, pitch=pitch, length=length, velocity=velocity)


def _parse_control_change(value: object, where: str, problems: list[str]) -> ControlChange | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, CC_KEYS, where, problems, OPTIONAL_CC_KEYS)

    field_problems = _control_change_problems(_present(data, CC_KEYS | OPTIONAL_CC_KEYS), where)
    problems += field_problems
    beat, control, cc_value = data.get("beat"), data.get("control"), data.get("value")
    shape = data.get("shape", "step")
    # These fail only when a problem was reported. A missing key reads as None.
    if field_problems or not (
        is_number(beat) and is_int(control) and is_int(cc_value) and is_shape(shape)
    ):
        return None
    return ControlChange(beat=beat, control=control, value=cc_value, shape=shape)


def _parse_cc(value: object, where: str, problems: list[str]) -> tuple[ControlChange, ...]:
    """Parse a track's controller changes, which must be in time order."""
    container_problems = _cc_problems(value, where)
    problems += container_problems
    if container_problems or not isinstance(value, list):
        return ()
    parsed = [
        (index, _parse_control_change(item, f"{where}.cc[{index}]", problems))
        for index, item in enumerate(value)
    ]
    changes = [(index, change) for index, change in parsed if change is not None]
    problems += _cc_order_problems(changes, where, complete=len(changes) == len(value))
    return tuple(change for _, change in changes)


def _parse_track(value: object, where: str, problems: list[str]) -> Track | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, TRACK_KEYS, where, problems, OPTIONAL_TRACK_KEYS)

    before = len(problems)
    problems += _track_field_problems(_present(data, TRACK_KEYS | OPTIONAL_TRACK_KEYS), where)

    raw_notes = data.get("notes")
    notes: list[Note | None] = []
    if "notes" in data:
        notes_problems = _notes_problems(raw_notes, where)
        problems += notes_problems
        if not notes_problems and isinstance(raw_notes, list):
            notes = [
                _parse_note(note, f"{where}.notes[{index}]", problems)
                for index, note in enumerate(raw_notes)
            ]

    cc = _parse_cc(data["cc"], where, problems) if "cc" in data else ()

    name, sound = data.get("name"), data.get("sound")
    gain = data.get("gain", 0.0)
    # Each of these fails only when a problem was reported. A missing key reads as None.
    if len(problems) > before or not is_number(gain) or "notes" not in data:
        return None
    if not (isinstance(name, str) and isinstance(sound, str)):
        return None
    return Track(
        name=name,
        sound=sound,
        notes=tuple(n for n in notes if n is not None),
        gain=gain,
        cc=cc,
    )


def parse_song(value: object) -> Song:
    """Validate loaded song data, reporting every problem found rather than only the first."""
    data = _mapping(value)
    if data is None:
        raise SongError([f"song must be a mapping, got {value!r}"])

    problems: list[str] = []
    _check_keys(data, SONG_KEYS, "", problems)
    problems += _song_field_problems(_present(data, SONG_KEYS), "")

    raw_tracks = data.get("tracks")
    tracks: list[Track] = []
    if isinstance(raw_tracks, list):
        parsed = [
            _parse_track(track, f"tracks[{index}]", problems)
            for index, track in enumerate(raw_tracks)
        ]
        tracks = [track for track in parsed if track is not None]
    problems += _duplicate_name_problems(tracks, "")

    bpm = data.get("bpm")
    # `bpm` fails `is_number` only when a problem was reported.
    if problems or not is_number(bpm):
        raise SongError(problems)
    if problems := _length_problems(bpm, tracks, "song"):
        raise SongError(problems)
    return Song(bpm=bpm, tracks=tuple(tracks))


def _plain(value: float) -> float:
    """Return a number in one spelling, so equal songs write equal text: an integral number as an
    int, any other as a float.
    """
    return int(value) if float(value).is_integer() else float(value)


def _note_to_data(note: Note) -> NoteData:
    return {
        "beat": _plain(note.beat),
        "pitch": note.pitch,
        "length": _plain(note.length),
        "velocity": note.velocity,
    }


def _control_to_data(change: ControlChange) -> ControlData:
    data: ControlData = {
        "beat": _plain(change.beat),
        "control": change.control,
        "value": change.value,
        "shape": change.shape,
    }
    if change.shape == "step":
        del data["shape"]
    return data


def _track_to_data(track: Track) -> TrackData:
    data: TrackData = {
        "name": track.name,
        "sound": track.sound,
        "gain": track.gain,
        "notes": [_note_to_data(note) for note in track.notes],
        "cc": [_control_to_data(change) for change in track.cc],
    }
    # Defaults are left out, so a song without gains or controller changes keeps its old text.
    if track.gain == 0:
        del data["gain"]
    if not track.cc:
        del data["cc"]
    return data


def song_to_data(song: Song) -> SongData:
    """Return a song as plain data in the song format's key order, the inverse of `parse_song`.

    Numbers are written in one spelling, so equal songs give equal data.
    """
    return {
        "bpm": _plain(song.bpm),
        "tracks": [_track_to_data(track) for track in song.tracks],
    }
