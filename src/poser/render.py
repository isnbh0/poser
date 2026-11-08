"""Render a song to one aligned WAV per track.

`render-song` is the parent command. Running this module with `python -m` is the per-track worker,
because plugin code runs only in a subprocess. Each written stem gets a render stamp recording the
inputs it came from, and a later run reuses a stem whose stamp still matches.
"""

import argparse
import base64
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import mido
import numpy as np

from poser.plugin_check import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    INITIALIZATION_TIMEOUT_S,
    NUM_CHANNELS,
    SAMPLE_RATE,
    SILENCE_DBFS,
    WORKER_TIMEOUT_S,
    ConfigError,
    PluginSpec,
    align_rows,
    load_config,
    measure_peak_dbfs,
)
from poser.presets import PresetError, build_state, preset_files, read_chunk
from poser.song import (
    ControlChange,
    ControlData,
    Note,
    Song,
    SongError,
    Track,
    is_int,
    is_number,
    is_shape,
    load_song,
)
from poser.sounds import SoundError, read_sound, sound_files
from poser.worker import (
    MalformedJobError,
    WorkerCrashedError,
    WorkerTimedOutError,
    job_fields,
    json_object,
    optional_str,
    run_isolated,
)
from poser.workspace import WorkspaceError, fill_path_options, song_render_dir

if TYPE_CHECKING:
    from pedalboard_native import MIDIMessageLike

# Directories, so a track named `mix` or `analysis` still writes its stem beside them.
MIX_DIR = "mix"
ANALYSIS_DIR = "analysis"
TAIL_S = 2.0
WAV_BIT_DEPTH = 32  # Float WAV, so levels above full scale survive until mixing.
# Serum applies a controller change at the start of the processing block that contains it, so
# the block size bounds controller timing. 512 samples is 10.7 ms at 48 kHz.
BUFFER_SIZE = 512
# Raise whenever rendering code changes what a stem contains for the same inputs, so every
# existing stamp stops matching.
RENDER_FORMAT = 2

# Exit code when another render-song or analyze-song holds the song's lock.
EXIT_SONG_BUSY = 3
LOCK_NAME = ".lock"

WORKER_FIELDS = {"status", "peak_dbfs", "detail"}
STAMP_FIELDS = {"track_key", "sound_key", "status", "peak_dbfs", "stem_size", "stem_mtime_ns"}


class TrackStatus(StrEnum):
    RENDERED = "rendered"
    SILENT = "silent"
    UNKNOWN_SOUND = "unknown_sound"
    LOAD_FAILED = "load_failed"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"


# The statuses of a track that wrote a stem, the only ones a stamp can record.
type StemStatus = Literal[TrackStatus.RENDERED, TrackStatus.SILENT]


def stem_status(value: object) -> StemStatus | None:
    """Return the status as a stem-writing status, or None for any other value."""
    if value == TrackStatus.RENDERED:
        return TrackStatus.RENDERED
    if value == TrackStatus.SILENT:
        return TrackStatus.SILENT
    return None


@dataclass(frozen=True)
class Outcome:
    """What happened when one track was resolved and rendered, as the worker reports it."""

    status: TrackStatus
    peak_dbfs: float | None = None
    detail: str | None = None


class NoteJob(TypedDict):
    beat: float
    pitch: int
    length: float
    velocity: int


class TrackJob(TypedDict):
    name: str
    engine: str
    sound: str
    notes: list[NoteJob]
    cc: list[ControlData]


class RenderJob(TypedDict):
    """Everything the worker renders from, sent as JSON. Gain is not sent; it applies at mixdown."""

    plugin_path: str
    plugin_name: str | None
    state: str | None
    bpm: float
    track: TrackJob
    duration_s: float
    stem_path: str


@dataclass(frozen=True)
class TrackResult:
    track: str
    status: TrackStatus
    peak_dbfs: float | None = None
    stem: str | None = None
    detail: str | None = None
    cached: bool = False


@dataclass(frozen=True)
class Stamp:
    """The inputs a stem was rendered from, and the stem file as it was when written."""

    track_key: str
    sound_key: str
    status: StemStatus
    peak_dbfs: float | None
    stem_size: int
    stem_mtime_ns: int


def song_last_beat(song: Song) -> float:
    """Return the latest note end or controller change across tracks."""
    note_ends = [note.beat + note.length for track in song.tracks for note in track.notes]
    return max([*note_ends, *(change.beat for track in song.tracks for change in track.cc)])


def song_duration_s(song: Song) -> float:
    """Return the length shared by every stem: the song's last beat plus the tail. Pedalboard
    ignores messages after the end, so changes must fall within.
    """
    return song.seconds(song_last_beat(song)) + TAIL_S


def expected_samples(duration_s: float) -> int:
    """Return the sample count Pedalboard renders, which truncates rather than rounds."""
    return int(duration_s * SAMPLE_RATE)


def _sample(song: Song, beat: float) -> int:
    return round(song.seconds(beat) * SAMPLE_RATE)


def _ramp_events(
    song: Song, start: ControlChange, end: ControlChange
) -> list[tuple[int, int, int]]:
    """Return one event per value along a linear segment, at the beat where the line rounds to it.

    The segment's start value is the start entry's own event, so values run from one past it.
    """
    span = abs(end.value - start.value)
    direction = 1 if end.value > start.value else -1
    return [
        (
            _sample(song, start.beat + (step - 0.5) / span * (end.beat - start.beat)),
            start.control,
            start.value + direction * step,
        )
        for step in range(1, span + 1)
    ]


def control_events(song: Song, track: Track) -> list[tuple[int, int, int]]:
    """Return a track's controller changes as `(sample, control, value)` events in time order.

    Each controller holds its first value from the start of the song, as automation does in a
    DAW, so a change at a note's start is already in place when the note begins. Linear segments
    expand to one event per value. Per controller, only the last event at a sample is kept, and an
    event that repeats the controller's current value is dropped.
    """
    events: list[tuple[int, int, int]] = []
    seen: set[int] = set()
    for index, change in enumerate(track.cc):
        events.append((_sample(song, change.beat), change.control, change.value))
        if change.control not in seen:
            seen.add(change.control)
            events.append((0, change.control, change.value))
        if change.shape == "linear":
            # `Track` guarantees a later change to the same controller.
            target = next(c for c in track.cc[index + 1 :] if c.control == change.control)
            events.extend(_ramp_events(song, change, target))
    events.sort(key=lambda event: event[0])

    last_at_sample = {
        (sample, control): position for position, (sample, control, _) in enumerate(events)
    }
    kept: list[tuple[int, int, int]] = []
    current: dict[int, int] = {}
    for position, (sample, control, value) in enumerate(events):
        if last_at_sample[(sample, control)] != position or current.get(control) == value:
            continue
        current[control] = value
        kept.append((sample, control, value))
    return kept


def track_messages(song: Song, track: Track) -> list[mido.Message]:
    """Build MIDI in time order. At one sample, note-offs come first, then controller changes,
    then note-ons, so a repeated pitch retriggers.

    Times snap to whole samples first. Otherwise float error, as in beats 0.1 + 0.2 against 0.3,
    can place a note-off just after the note-on it should precede and silence the new note.
    Pedalboard reads `time` as absolute seconds, not mido deltas, converts it to a 32-bit float,
    and truncates `time * sample_rate` to a sample, so each time sits half a sample past its
    sample. That lands exactly for the first 128 seconds; later, float32 rounding can move a
    message by a sample or more, without reordering messages.
    """
    events = []
    for note in track.notes:
        start = _sample(song, note.beat)
        end = max(_sample(song, note.beat + note.length), start + 1)
        events.append((start, 2, mido.Message("note_on", note=note.pitch, velocity=note.velocity)))
        events.append((end, 0, mido.Message("note_off", note=note.pitch)))
    for sample, control, value in control_events(song, track):
        events.append((sample, 1, mido.Message("control_change", control=control, value=value)))
    events.sort(key=lambda event: event[:2])
    return [message.copy(time=(sample + 0.5) / SAMPLE_RATE) for sample, _, message in events]


def stem_path(song_dir: Path, track_name: str) -> Path:
    """Return where a track's stem lives. Later steps find stems only through this rule."""
    return song_dir / f"{track_name}.wav"


def _digest(data: object) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def track_key(song: Song, track: Track) -> str:
    """Return a digest of everything from the song that reaches the track's worker.

    Gain is left out, because it applies only at mixdown. The song's duration is in, because every
    stem shares it. Numbers are normalized so that `1` and `1.0` give the same key.
    """
    return _digest(
        {
            "format": RENDER_FORMAT,
            "sample_rate": SAMPLE_RATE,
            "channels": NUM_CHANNELS,
            "bit_depth": WAV_BIT_DEPTH,
            "tail_s": TAIL_S,
            "bpm": float(song.bpm),
            "duration_s": song_duration_s(song),
            "name": track.name,
            "engine": track.engine,
            "sound": track.sound,
            "notes": [
                [float(note.beat), int(note.pitch), float(note.length), int(note.velocity)]
                for note in track.notes
            ],
            "cc": [
                [float(change.beat), int(change.control), int(change.value), change.shape]
                for change in track.cc
            ],
        }
    )


def sound_key(spec: PluginSpec, state: str | None) -> str:
    """Return a digest of a resolved sound, so editing a saved sound or preset changes it."""
    return _digest({"plugin_path": spec.path, "plugin_name": spec.plugin_name, "state": state})


def stamp_path(song_dir: Path, track_name: str) -> Path:
    """Return where a track's render stamp lives, beside its stem."""
    return song_dir / f"{track_name}.json"


def write_stamp(
    path: Path,
    stem: Path,
    track_key: str,
    sound_key: str,
    status: StemStatus,
    peak_dbfs: float | None,
) -> None:
    """Record a stem just written, through a temporary file that then replaces the target."""
    info = stem.stat()
    data = {
        "track_key": track_key,
        "sound_key": sound_key,
        "status": str(status),
        "peak_dbfs": peak_dbfs,
        "stem_size": info.st_size,
        "stem_mtime_ns": info.st_mtime_ns,
    }
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps(data, allow_nan=False))
        Path(temp).replace(path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def read_stamp(path: Path) -> Stamp | None:
    """Return a stamp, or None when it is missing, unreadable, or malformed."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    fields = json_object(data)
    if fields is None or set(fields) != STAMP_FIELDS:
        return None
    track_key, sound_key = fields["track_key"], fields["sound_key"]
    status, peak = stem_status(fields["status"]), fields["peak_dbfs"]
    size, mtime = fields["stem_size"], fields["stem_mtime_ns"]
    if not (isinstance(track_key, str) and isinstance(sound_key, str)) or status is None:
        return None
    if not (peak is None or is_number(peak)) or not (is_int(size) and is_int(mtime)):
        return None
    return Stamp(track_key, sound_key, status, None if peak is None else float(peak), size, mtime)


def stamp_matches_stem(stamp: Stamp, stem: Path) -> bool:
    """Return whether the stem is still the file the stamp recorded."""
    try:
        info = stem.stat()
    except OSError:
        return False
    return (info.st_size, info.st_mtime_ns) == (stamp.stem_size, stamp.stem_mtime_ns)


class SongBusyError(Exception):
    """Another render-song or analyze-song is running on the same song."""


@contextmanager
def song_lock(song_dir: Path) -> Generator[None]:
    """Hold a song's lock for a run, or raise SongBusyError at once if another run holds it.

    Two runs on one song would stamp each other's stems and number the same history record. The
    operating system releases the lock when its descriptor closes, even if the process dies, so
    the lock file left on disk means nothing by itself.
    """
    song_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(song_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SongBusyError(
                f"another render-song or analyze-song is running on {song_dir.name}"
            ) from error
        yield
    finally:
        os.close(fd)


def clear_results(song_dir: Path) -> None:
    """Delete a song's mix and analysis, so nothing downstream reads results from older audio."""
    shutil.rmtree(song_dir / MIX_DIR, ignore_errors=True)
    shutil.rmtree(song_dir / ANALYSIS_DIR, ignore_errors=True)


def temp_stem_path(stem: Path) -> Path:
    # Pedalboard picks the file format from the extension, so the name must end in .wav.
    return stem.with_name(f".{stem.stem}.tmp.wav")


def worker_command(job_path: Path) -> list[str]:
    return [sys.executable, "-m", "poser.render", str(job_path)]


def render_job(
    song: Song, track: Track, spec: PluginSpec, state: str | None, stem: Path, duration_s: float
) -> RenderJob:
    return {
        "plugin_path": spec.path,
        "plugin_name": spec.plugin_name,
        "state": state,
        "bpm": song.bpm,
        "track": {
            "name": track.name,
            "engine": track.engine,
            "sound": track.sound,
            "notes": [
                {"beat": n.beat, "pitch": n.pitch, "length": n.length, "velocity": n.velocity}
                for n in track.notes
            ],
            "cc": [
                {"beat": c.beat, "control": c.control, "value": c.value, "shape": c.shape}
                for c in track.cc
            ],
        },
        "duration_s": duration_s,
        "stem_path": str(stem),
    }


def _parse_note_job(value: object) -> NoteJob:
    fields = job_fields(value, {"beat", "pitch", "length", "velocity"}, "note")
    beat, pitch, length, velocity = (fields[k] for k in ("beat", "pitch", "length", "velocity"))
    if not (is_number(beat) and is_int(pitch) and is_number(length) and is_int(velocity)):
        raise MalformedJobError("note", value)
    return {"beat": beat, "pitch": pitch, "length": length, "velocity": velocity}


def _parse_change_job(value: object) -> ControlData:
    fields = job_fields(value, {"beat", "control", "value", "shape"}, "controller change")
    beat, control, level, shape = (fields[k] for k in ("beat", "control", "value", "shape"))
    if not (is_number(beat) and is_int(control) and is_int(level) and is_shape(shape)):
        raise MalformedJobError("controller change", value)
    return {"beat": beat, "control": control, "value": level, "shape": shape}


def _parse_track_job(value: object) -> TrackJob:
    fields = job_fields(value, {"name", "engine", "sound", "notes", "cc"}, "track")
    name, engine, sound = fields["name"], fields["engine"], fields["sound"]
    notes, cc = fields["notes"], fields["cc"]
    if not (isinstance(name, str) and isinstance(engine, str) and isinstance(sound, str)):
        raise MalformedJobError("track", value)
    if not (isinstance(notes, list) and isinstance(cc, list)):
        raise MalformedJobError("track", value)
    return {
        "name": name,
        "engine": engine,
        "sound": sound,
        "notes": [_parse_note_job(note) for note in notes],
        "cc": [_parse_change_job(change) for change in cc],
    }


def parse_render_job(value: object) -> RenderJob:
    """Return a render job read back from JSON, raising MalformedJobError if it is malformed.

    The parent writes every job with `render_job`, so a malformed one is a bug.
    """
    keys = set(RenderJob.__annotations__)
    fields = job_fields(value, keys, "render job")
    plugin_path, plugin_name, state = fields["plugin_path"], fields["plugin_name"], fields["state"]
    bpm, duration_s, stem = fields["bpm"], fields["duration_s"], fields["stem_path"]
    if not (isinstance(plugin_path, str) and optional_str(plugin_name) and optional_str(state)):
        raise MalformedJobError("render job", value)
    if not (is_number(bpm) and is_number(duration_s) and isinstance(stem, str)):
        raise MalformedJobError("render job", value)
    return {
        "plugin_path": plugin_path,
        "plugin_name": plugin_name,
        "state": state,
        "bpm": bpm,
        "track": _parse_track_job(fields["track"]),
        "duration_s": duration_s,
        "stem_path": stem,
    }


def parse_worker_result(payload: object, stem: Path) -> Outcome:
    """Turn the worker's JSON result into an outcome, reporting anything malformed as a crash."""
    malformed = Outcome(TrackStatus.CRASHED, detail="worker wrote a malformed result")
    fields = json_object(payload)
    if fields is None or not set(fields) <= WORKER_FIELDS:
        return malformed
    status, peak, detail = fields.get("status"), fields.get("peak_dbfs"), fields.get("detail")
    if not isinstance(status, str) or status not in TrackStatus:
        return Outcome(TrackStatus.CRASHED, detail="worker wrote an unknown status")
    if not (peak is None or is_number(peak)) or not (detail is None or isinstance(detail, str)):
        return malformed
    outcome = Outcome(TrackStatus(status), None if peak is None else float(peak), detail)
    if stem_status(outcome.status) is not None and not stem.exists():
        return Outcome(TrackStatus.CRASHED, detail="worker reported a stem it did not write")
    return outcome


def _run_track_worker(job: RenderJob, timeout_s: float) -> Outcome:
    with tempfile.TemporaryDirectory() as directory:
        job_path = Path(directory) / "job.json"
        job_path.write_text(json.dumps(job))
        try:
            payload = run_isolated(worker_command(job_path), timeout_s)
        except WorkerTimedOutError as error:
            return Outcome(TrackStatus.TIMED_OUT, detail=str(error))
        except WorkerCrashedError as error:
            return Outcome(TrackStatus.CRASHED, detail=str(error))
    return parse_worker_result(payload, Path(job["stem_path"]))


def resolve_sound(
    sound: str, specs: list[PluginSpec], sounds_dir: Path
) -> tuple[PluginSpec, str | None] | Outcome:
    """Return a track's plugin and base64 state, or the failed outcome to report.

    A plugin name means its default state, with no state to restore. Otherwise the name must be a
    saved sound, matched exactly against the sound files, or `<plugin>:<path>` naming one of that
    plugin's installed presets, matched exactly against its preset files.
    """
    specs_by_name = {spec.name: spec for spec in specs}
    if sound in specs_by_name:
        return specs_by_name[sound], None
    unknown = Outcome(
        TrackStatus.UNKNOWN_SOUND, detail=f"no plugin, saved sound, or preset named {sound!r}"
    )
    plugin_name, colon, _ = sound.partition(":")
    if colon:
        spec = specs_by_name.get(plugin_name)
        if spec is None:
            return unknown
        try:
            preset = preset_files(spec).get(sound)
            if preset is None:
                return unknown
            state = build_state(read_chunk(preset))
        except PresetError as error:
            return Outcome(TrackStatus.LOAD_FAILED, detail=f"preset {sound!r}: {error}")
        return spec, base64.b64encode(state).decode("ascii")
    path = sound_files(sounds_dir).get(sound)
    if path is None:
        return unknown
    try:
        saved = read_sound(path, specs)
    except SoundError as error:
        return Outcome(TrackStatus.LOAD_FAILED, detail=f"saved sound {sound!r}: {error}")
    return specs_by_name[saved.plugin], saved.state


def render_song(
    song: Song,
    song_dir: Path,
    specs: list[PluginSpec],
    *,
    sounds_dir: Path,
    timeout_s: float = WORKER_TIMEOUT_S,
    force: bool = False,
) -> list[TrackResult]:
    """Render each track whose inputs changed since its stem was written, or every track if forced.

    A stem is reused when its stamp matches the file and both the track's and the sound's keys.
    """
    with song_lock(song_dir):
        return _render_tracks(
            song, song_dir, specs, timeout_s=timeout_s, sounds_dir=sounds_dir, force=force
        )


def _render_tracks(
    song: Song,
    song_dir: Path,
    specs: list[PluginSpec],
    *,
    timeout_s: float,
    sounds_dir: Path,
    force: bool,
) -> list[TrackResult]:
    duration_s = song_duration_s(song)
    clear_results(song_dir)

    results = []
    for track in song.tracks:
        stem = stem_path(song_dir, track.name)
        stamp = stamp_path(song_dir, track.name)
        resolved = resolve_sound(track.sound, specs, sounds_dir)
        if isinstance(resolved, Outcome):
            # A track whose sound cannot be resolved leaves no stem or stamp for later steps.
            stem.unlink(missing_ok=True)
            stamp.unlink(missing_ok=True)
            results.append(TrackResult(track.name, resolved.status, detail=resolved.detail))
            continue

        spec, state = resolved
        keys = (track_key(song, track), sound_key(spec, state))
        saved = None if force else read_stamp(stamp)
        if (
            saved is not None
            and (saved.track_key, saved.sound_key) == keys
            and stamp_matches_stem(saved, stem)
        ):
            results.append(
                TrackResult(track.name, saved.status, saved.peak_dbfs, str(stem), cached=True)
            )
            continue

        # Remove the old stem and stamp first, so only a stem this worker writes can be stamped.
        stamp.unlink(missing_ok=True)
        stem.unlink(missing_ok=True)
        job = render_job(song, track, spec, state, stem, duration_s)
        outcome = _run_track_worker(job, timeout_s)
        temp_stem_path(stem).unlink(missing_ok=True)
        written = stem_status(outcome.status)
        if written is not None:
            write_stamp(stamp, stem, *keys, written, outcome.peak_dbfs)
        else:
            # A failed track leaves no earlier stem behind for later steps to use by mistake.
            stem.unlink(missing_ok=True)
        results.append(
            TrackResult(
                track.name,
                outcome.status,
                outcome.peak_dbfs,
                str(stem) if written is not None else None,
                outcome.detail,
            )
        )
    return results


def exit_code(results: list[TrackResult]) -> int:
    all_rendered = all(result.status is TrackStatus.RENDERED for result in results)
    return EXIT_ALL_PASS if all_rendered else EXIT_SOME_FAILED


def format_table(results: list[TrackResult]) -> str:
    rows = [("TRACK", "STATUS", "CACHED", "PEAK DBFS", "STEM")]
    details = []
    for result in results:
        peak = "-" if result.peak_dbfs is None else f"{result.peak_dbfs:.1f}"
        cached = "yes" if result.cached else "-"
        rows.append((result.track, str(result.status), cached, peak, result.stem or "-"))
        if result.detail:
            details.append(f"{result.track}: {result.detail}")
    return "\n".join([*align_rows(rows), *details])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render-song", description=__doc__)
    parser.add_argument("song", type=Path, help="song file (YAML, or a script defining build())")
    parser.add_argument("--config", type=Path, help="plugin list (TOML)")
    parser.add_argument("--output-dir", type=Path, help="where stems go")
    parser.add_argument("--sounds-dir", type=Path, help="saved sound files")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--force", action="store_true", help="render every track, even one with a matching stamp"
    )
    args = parser.parse_args(argv)

    try:
        base = fill_path_options(args)
        # Before loading, so a refused song's script never runs.
        song_dir = song_render_dir(args.output_dir, base, args.song)
    except WorkspaceError as error:
        print(f"render-song: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    try:
        song = load_song(args.song)
        specs = load_config(args.config)
    except (SongError, ConfigError) as error:
        print(f"render-song: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    try:
        results = render_song(
            song,
            song_dir,
            specs,
            sounds_dir=args.sounds_dir,
            force=args.force,
        )
    except SongBusyError as error:
        print(f"render-song: {error}", file=sys.stderr)
        return EXIT_SONG_BUSY
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(format_table(results))
    return exit_code(results)


def write_stem(path: Path, audio: np.ndarray) -> None:
    """Write audio in the stem format through a temporary file that then replaces the target."""
    from pedalboard.io import WriteableAudioFile

    temp = temp_stem_path(path)
    # Pedalboard's stubs mark `__new__` as a classmethod, which ty cannot match to any call.
    with WriteableAudioFile(  # ty: ignore[no-matching-overload]
        str(temp), samplerate=SAMPLE_RATE, num_channels=NUM_CHANNELS, bit_depth=WAV_BIT_DEPTH
    ) as output:
        output.write(audio)
    temp.replace(path)


def render_stem(job: RenderJob) -> Outcome:
    """Render one track inside the worker process and write its stem."""
    # Imported here so the parent never loads plugin code.
    from pedalboard import AudioUnitPlugin, VST3Plugin, load_plugin

    duration_s = job["duration_s"]
    track_data = job["track"]
    notes = tuple(Note(**note) for note in track_data["notes"])
    cc = tuple(ControlChange(**change) for change in track_data["cc"])
    track = Track(track_data["name"], track_data["engine"], track_data["sound"], notes, cc=cc)
    song = Song(bpm=job["bpm"], tracks=(track,))

    # Any exception from loading third-party plugin code, or from it rejecting a saved state, is
    # the result being reported.
    try:
        # `load_plugin` is annotated as returning `ExternalPlugin`, which lacks `raw_state`, but it
        # returns one of these two classes.
        plugin = cast(
            "VST3Plugin | AudioUnitPlugin",
            load_plugin(
                job["plugin_path"],
                plugin_name=job["plugin_name"],
                initialization_timeout=INITIALIZATION_TIMEOUT_S,
            ),
        )
        if job["state"] is not None:
            plugin.raw_state = base64.b64decode(job["state"])
    except Exception as error:  # noqa: BLE001
        return Outcome(TrackStatus.LOAD_FAILED, detail=str(error))

    # Pedalboard's reset rebuilds an instrument by saving its state, recreating it, and restoring
    # that state, so a restored sound survives. Serum renders differently without the reset.
    # Pedalboard's stubs take a `list` of its message union, and `list` is invariant.
    messages: list[MIDIMessageLike] = [*track_messages(song, track)]
    audio = plugin(
        messages,
        duration=duration_s,
        sample_rate=SAMPLE_RATE,
        num_channels=NUM_CHANNELS,
        buffer_size=BUFFER_SIZE,
        reset=True,
    )
    expected = (NUM_CHANNELS, expected_samples(duration_s))
    if audio.shape != expected:
        raise RuntimeError(f"render shape {audio.shape}, expected {expected}")
    if not np.isfinite(audio).all():
        raise RuntimeError("render contains NaN or infinity")

    write_stem(Path(job["stem_path"]), audio)

    peak_dbfs = measure_peak_dbfs(audio)
    status = TrackStatus.SILENT if peak_dbfs < SILENCE_DBFS else TrackStatus.RENDERED
    return Outcome(status, peak_dbfs)


def worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="poser.render")
    parser.add_argument("job_path", type=Path)
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args(argv)

    # The result goes to a file because plugins may print to stdout.
    job = parse_render_job(json.loads(args.job_path.read_text()))  # From `_run_track_worker`.
    args.result_path.write_text(json.dumps(asdict(render_stem(job))))
    return 0


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv[1:]))
