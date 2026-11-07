"""Song data: engine-neutral text, separate from rendering code."""

import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ENGINES = {"pedalboard"}
TRACK_NAME = re.compile(r"[A-Za-z0-9_-]+")

SONG_KEYS = {"bpm", "tracks"}
TRACK_KEYS = {"name", "engine", "sound", "notes"}
NOTE_KEYS = {"beat", "pitch", "length", "velocity"}


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
class Track:
    name: str
    engine: str
    sound: str
    notes: tuple[Note, ...]


@dataclass(frozen=True)
class Song:
    bpm: float
    tracks: tuple[Track, ...]

    def seconds(self, beat: float) -> float:
        return beat * 60 / self.bpm


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_int_in_range(value: Any, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _check_keys(data: dict[Any, Any], allowed: set[str], where: str, problems: list[str]) -> None:
    prefix = f"{where}: " if where else ""
    for key in data:
        if key not in allowed:
            problems.append(f"{prefix}unknown key {key!r}")
    for key in sorted(allowed - set(data)):
        problems.append(f"{prefix}missing key {key!r}")


def _parse_note(data: Any, where: str, problems: list[str]) -> Note | None:
    if not isinstance(data, dict):
        problems.append(f"{where}: must be a mapping, got {data!r}")
        return None
    _check_keys(data, NOTE_KEYS, where, problems)

    before = len(problems)
    beat, pitch = data.get("beat"), data.get("pitch")
    length, velocity = data.get("length"), data.get("velocity")
    if "beat" in data and not (_is_number(beat) and beat >= 0):
        problems.append(f"{where}.beat: must be a number that is not negative, got {beat!r}")
    if "pitch" in data and not _is_int_in_range(pitch, 0, 127):
        problems.append(f"{where}.pitch: must be an integer from 0 to 127, got {pitch!r}")
    if "length" in data and not (_is_number(length) and length > 0):
        problems.append(f"{where}.length: must be a positive number, got {length!r}")
    if "velocity" in data and not _is_int_in_range(velocity, 1, 127):
        problems.append(f"{where}.velocity: must be an integer from 1 to 127, got {velocity!r}")

    if len(problems) > before or not NOTE_KEYS <= set(data):
        return None
    return Note(beat=beat, pitch=pitch, length=length, velocity=velocity)


def _parse_track(data: Any, where: str, problems: list[str]) -> Track | None:
    if not isinstance(data, dict):
        problems.append(f"{where}: must be a mapping, got {data!r}")
        return None
    _check_keys(data, TRACK_KEYS, where, problems)

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

    if len(problems) > before or not TRACK_KEYS <= set(data):
        return None
    return Track(
        name=name, engine=engine, sound=sound, notes=tuple(n for n in notes if n is not None)
    )


def parse_song(data: Any) -> Song:
    """Validate loaded song data, reporting every problem found rather than only the first."""
    if not isinstance(data, dict):
        raise SongError([f"song must be a mapping, got {data!r}"])

    problems: list[str] = []
    _check_keys(data, SONG_KEYS, "", problems)

    bpm = data.get("bpm")
    if "bpm" in data and not (_is_number(bpm) and bpm > 0):
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

    if problems:
        raise SongError(problems)
    return Song(bpm=bpm, tracks=tuple(t for t in tracks if t is not None))


def load_song(path: Path) -> Song:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise SongError([f"cannot read {path}: {error}"]) from error
    return parse_song(data)


def song_to_data(song: Song) -> dict[str, Any]:
    """Return a song as plain data in the song format's key order, the inverse of `parse_song`."""
    return {
        "bpm": song.bpm,
        "tracks": [
            {
                "name": track.name,
                "engine": track.engine,
                "sound": track.sound,
                "notes": [
                    {
                        "beat": note.beat,
                        "pitch": note.pitch,
                        "length": note.length,
                        "velocity": note.velocity,
                    }
                    for note in track.notes
                ],
            }
            for track in song.tracks
        ],
    }


def write_song(song: Song, path: Path) -> None:
    """Validate a song and write it as YAML, replacing `path` only once the file is complete.

    A symlinked `path` keeps its link and the target is replaced. The file keeps an existing
    target's permissions, or gets the usual umask-based ones when new.
    """
    data = song_to_data(song)
    parse_song(data)
    text = yaml.safe_dump(data, sort_keys=False)
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
