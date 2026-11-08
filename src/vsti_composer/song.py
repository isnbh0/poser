"""Song data: engine-neutral text, separate from rendering code."""

import math
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, TypeGuard

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


@dataclass(frozen=True)
class Note:
    beat: float
    pitch: int
    length: float
    velocity: int


@dataclass(frozen=True)
class ControlChange:
    beat: float
    control: int
    value: int
    shape: Shape = "step"


@dataclass(frozen=True)
class Track:
    name: str
    engine: str
    sound: str
    notes: tuple[Note, ...]
    gain: float = 0.0
    cc: tuple[ControlChange, ...] = ()


@dataclass(frozen=True)
class Song:
    bpm: float
    tracks: tuple[Track, ...]

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


def _mapping(value: object) -> Mapping[object, object] | None:
    """Return loaded YAML as a mapping of untyped keys and values, or None when it is not one."""
    return value if isinstance(value, dict) else None


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

    before = len(problems)
    beat, pitch = data.get("beat"), data.get("pitch")
    length, velocity = data.get("length"), data.get("velocity")
    if "beat" in data and not (is_number(beat) and beat >= 0):
        problems.append(f"{where}.beat: must be a number that is not negative, got {beat!r}")
    if "pitch" in data and not _is_int_in_range(pitch, 0, 127):
        problems.append(f"{where}.pitch: must be an integer from 0 to 127, got {pitch!r}")
    if "length" in data and not (is_number(length) and length > 0):
        problems.append(f"{where}.length: must be a positive number, got {length!r}")
    if "velocity" in data and not _is_int_in_range(velocity, 1, 127):
        problems.append(f"{where}.velocity: must be an integer from 1 to 127, got {velocity!r}")

    if len(problems) > before:
        return None
    # These fail only when a problem was reported. A missing key reads as None.
    if not (is_number(beat) and is_int(pitch) and is_number(length) and is_int(velocity)):
        return None
    return Note(beat=beat, pitch=pitch, length=length, velocity=velocity)


def _is_shape(value: object) -> TypeGuard[Shape]:
    return value in SHAPES


def _parse_control_change(value: object, where: str, problems: list[str]) -> ControlChange | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, CC_KEYS, where, problems, OPTIONAL_CC_KEYS)

    before = len(problems)
    beat, control, cc_value = data.get("beat"), data.get("control"), data.get("value")
    shape = data.get("shape", "step")
    if "beat" in data and not (is_number(beat) and beat >= 0):
        problems.append(f"{where}.beat: must be a number that is not negative, got {beat!r}")
    if "control" in data and not _is_int_in_range(control, 0, CONTROL_MAX):
        problems.append(
            f"{where}.control: must be an integer from 0 to {CONTROL_MAX}, got {control!r}"
        )
    if "value" in data and not _is_int_in_range(cc_value, 0, 127):
        problems.append(f"{where}.value: must be an integer from 0 to 127, got {cc_value!r}")
    if not _is_shape(shape):
        problems.append(f"{where}.shape: must be step or linear, got {shape!r}")

    if len(problems) > before:
        return None
    # These fail only when a problem was reported. A missing key reads as None.
    if not (is_number(beat) and is_int(control) and is_int(cc_value) and _is_shape(shape)):
        return None
    return ControlChange(beat=beat, control=control, value=cc_value, shape=shape)


def _parse_cc(value: object, where: str, problems: list[str]) -> tuple[ControlChange, ...]:
    """Parse a track's controller changes, which must be in time order."""
    if not isinstance(value, list):
        problems.append(f"{where}: must be a list of controller changes, got {value!r}")
        return ()
    parsed = [
        (index, _parse_control_change(item, f"{where}[{index}]", problems))
        for index, item in enumerate(value)
    ]
    changes = [(index, change) for index, change in parsed if change is not None]
    for (_, previous), (index, change) in pairwise(changes):
        if change.beat < previous.beat:
            problems.append(
                f"{where}[{index}].beat: must not be earlier than the previous change at beat "
                f"{previous.beat}, got {change.beat}"
            )
    # A failed entry could be a ramp's target, so ramps are checked only when every entry parsed.
    for position, (index, change) in enumerate(changes if len(changes) == len(value) else []):
        if change.shape != "linear":
            continue
        following = (c for _, c in changes[position + 1 :] if c.control == change.control)
        target = next(following, None)
        if target is None or target.beat <= change.beat:
            problems.append(
                f"{where}[{index}].shape: linear needs a later change to control {change.control}"
            )
    return tuple(change for _, change in changes)


def _parse_track(value: object, where: str, problems: list[str]) -> Track | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, TRACK_KEYS, where, problems, OPTIONAL_TRACK_KEYS)

    before = len(problems)
    name, engine, sound = data.get("name"), data.get("engine"), data.get("sound")
    if "name" in data and not (isinstance(name, str) and TRACK_NAME.fullmatch(name)):
        problems.append(
            f"{where}.name: must contain only letters, digits, '_', and '-', got {name!r}"
        )
    if "engine" in data and engine not in ENGINES:
        problems.append(f"{where}.engine: unknown engine {engine!r}")
    if "sound" in data and not (isinstance(sound, str) and sound):
        problems.append(f"{where}.sound: must be a sound name, got {sound!r}")
    gain = data.get("gain", 0.0)
    if not (is_number(gain) and GAIN_MIN_DB <= gain <= GAIN_MAX_DB):
        problems.append(
            f"{where}.gain: must be a number of decibels from {GAIN_MIN_DB} to {GAIN_MAX_DB}, "
            f"got {gain!r}"
        )

    raw_notes = data.get("notes")
    notes: list[Note | None] = []
    if "notes" in data:
        if not isinstance(raw_notes, list) or not raw_notes:
            problems.append(f"{where}.notes: must list at least one note")
        else:
            notes = [
                _parse_note(note, f"{where}.notes[{index}]", problems)
                for index, note in enumerate(raw_notes)
            ]

    cc = _parse_cc(data["cc"], f"{where}.cc", problems) if "cc" in data else ()

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
        gain=float(gain),
        cc=cc,
    )


def parse_song(value: object) -> Song:
    """Validate loaded song data, reporting every problem found rather than only the first."""
    data = _mapping(value)
    if data is None:
        raise SongError([f"song must be a mapping, got {value!r}"])

    problems: list[str] = []
    _check_keys(data, SONG_KEYS, "", problems)

    bpm = data.get("bpm")
    if "bpm" in data and not (is_number(bpm) and bpm > 0):
        problems.append(f"bpm: must be a positive number, got {bpm!r}")

    raw_tracks = data.get("tracks")
    tracks: list[Track | None] = []
    if "tracks" in data:
        if not isinstance(raw_tracks, list) or not raw_tracks:
            problems.append("tracks: must list at least one track")
        else:
            tracks = [
                _parse_track(track, f"tracks[{index}]", problems)
                for index, track in enumerate(raw_tracks)
            ]

    # Names become filenames, and macOS treats names differing only in case as the same file.
    seen: set[str] = set()
    for track in tracks:
        if track is not None:
            if track.name.casefold() in seen:
                problems.append(f"tracks: duplicate track name {track.name!r}")
            seen.add(track.name.casefold())

    # `bpm` fails `is_number` only when a problem was reported.
    if problems or not is_number(bpm):
        raise SongError(problems)
    return Song(bpm=bpm, tracks=tuple(t for t in tracks if t is not None))


def load_song(path: Path) -> Song:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise SongError([f"cannot read {path}: {error}"]) from error
    return parse_song(data)


def _control_to_data(change: ControlChange) -> ControlData:
    data: ControlData = {
        "beat": change.beat,
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
        "gain": float(track.gain),
        "notes": [
            {"beat": n.beat, "pitch": n.pitch, "length": n.length, "velocity": n.velocity}
            for n in track.notes
        ],
        "cc": [_control_to_data(change) for change in track.cc],
    }
    # Defaults are left out, so a song without gains or controller changes keeps its old text.
    if track.gain == 0:
        del data["gain"]
    if not track.cc:
        del data["cc"]
    return data


def song_to_data(song: Song) -> SongData:
    """Return a song as plain data in the song format's key order, the inverse of `parse_song`."""
    return {"bpm": song.bpm, "tracks": [_track_to_data(track) for track in song.tracks]}


def write_song(song: Song, path: Path) -> None:
    """Validate a song and write it as YAML, replacing `path` only once the file is complete.

    A symlinked `path` keeps its link and the target is replaced. The file keeps an existing
    target's permissions, or gets the usual umask-based ones when new.
    """
    data = song_to_data(song)
    parse_song(data)
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
