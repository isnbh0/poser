"""Song data: engine-neutral text, separate from rendering code."""

import math
import os
import re
import runpy
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, TypeGuard, cast

import yaml

ENGINES = {"pedalboard"}
TRACK_NAME = re.compile(r"[A-Za-z0-9_-]+")

SONG_KEYS = {"bpm", "tracks"}
TRACK_KEYS = {"name", "engine", "sound", "notes"}
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
    engine: str
    sound: str
    notes: tuple[Note, ...]
    gain: float = 0.0
    cc: tuple[ControlChange, ...] = ()

    def __post_init__(self) -> None:
        values = {
            "name": self.name,
            "engine": self.engine,
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
    engine: str
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


def _is_shape(value: object) -> TypeGuard[Shape]:
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
    if "shape" in values and not _is_shape(shape):
        problems.append(f"{where}.shape: must be step or linear, got {shape!r}")
    return problems


def _track_field_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check a track's name, engine, sound, and gain. `_track_problems` checks the rest."""
    name, engine = values.get("name"), values.get("engine")
    sound, gain = values.get("sound"), values.get("gain")
    problems: list[str] = []
    if "name" in values and not (isinstance(name, str) and TRACK_NAME.fullmatch(name)):
        problems.append(
            f"{where}.name: must contain only letters, digits, '_', and '-', got {name!r}"
        )
    if "engine" in values and engine not in ENGINES:
        problems.append(f"{where}.engine: unknown engine {engine!r}")
    if "sound" in values and not (isinstance(sound, str) and sound):
        problems.append(f"{where}.sound: must be a sound name, got {sound!r}")
    if "gain" in values and not (is_number(gain) and GAIN_MIN_DB <= gain <= GAIN_MAX_DB):
        problems.append(
            f"{where}.gain: must be a number of decibels from {GAIN_MIN_DB} to {GAIN_MAX_DB}, "
            f"got {gain!r}"
        )
    return problems


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
    for key in data:
        if key not in required and key not in optional:
            problems.append(f"{prefix}unknown key {key!r}")
    for key in sorted(required - set(data)):
        problems.append(f"{prefix}missing key {key!r}")


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
        is_number(beat) and is_int(control) and is_int(cc_value) and _is_shape(shape)
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

    name, engine, sound = data.get("name"), data.get("engine"), data.get("sound")
    gain = data.get("gain", 0.0)
    # Each of these fails only when a problem was reported. A missing key reads as None.
    if len(problems) > before or not is_number(gain) or "notes" not in data:
        return None
    if not (isinstance(name, str) and isinstance(engine, str) and isinstance(sound, str)):
        return None
    return Track(
        name=name,
        engine=engine,
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
    return Song(bpm=bpm, tracks=tuple(tracks))


YAML_SUFFIXES = (".yaml", ".yml")
SCRIPT_SUFFIX = ".py"


def load_song(path: Path) -> Song:
    """Load a song from YAML, or from a script whose `build()` returns one."""
    suffix = path.suffix
    if suffix not in (*YAML_SUFFIXES, SCRIPT_SUFFIX):
        raise SongError([f"cannot read {path}: a song is a .yaml, .yml, or .py file"])
    if suffix == SCRIPT_SUFFIX:
        return _build_song(path)
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise SongError([f"cannot read {path}: {error}"]) from error
    return parse_song(data)


def _build_song(path: Path) -> Song:
    # A song script is the owner's or agent's own code, run like any project script.
    try:
        build = runpy.run_path(str(path)).get("build")
    except SongError as error:
        raise _in_script(path, error) from error
    except (Exception, SystemExit) as error:
        raise SongError([f"cannot run {path}: {type(error).__name__}: {error}"]) from error
    if not callable(build):
        raise SongError([f"{path}: must define a function build() that returns a Song"])
    try:
        song = build()
    except SongError as error:
        raise _in_script(path, error) from error
    except (Exception, SystemExit) as error:
        raise SongError([f"{path}: build() raised {type(error).__name__}: {error}"]) from error
    if not isinstance(song, Song):
        raise SongError([f"{path}: build() must return a Song, got {type(song).__name__}"])
    return song


def _in_script(path: Path, error: SongError) -> SongError:
    """Return a script's song error with the script's path before each problem."""
    return SongError([f"{path}: {problem}" for problem in error.problems])


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
        "engine": track.engine,
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


def write_song(song: Song, path: Path) -> None:
    """Write a song as YAML, replacing `path` only once the file is complete.

    A symlinked `path` keeps its link and the target is replaced. The file keeps an existing
    target's permissions, or gets the usual umask-based ones when new.
    """
    data = song_to_data(song)
    try:
        text = yaml.safe_dump(data, sort_keys=False)
    except yaml.YAMLError as error:
        raise SongError([f"cannot write {path}: {error}"]) from error
    target = Path(path).resolve()
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    except OSError as error:
        raise SongError([f"cannot write {path}: {error}"]) from error
    try:
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    except OSError as error:
        raise SongError([f"cannot write {path}: {error}"]) from error
    temp = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException as error:
        temp.unlink(missing_ok=True)
        if isinstance(error, OSError):
            raise SongError([f"cannot write {path}: {error}"]) from error
        raise
