"""Render a song to one aligned WAV per track.

`render-song` is the parent command. Running this module with `python -m` is the per-track worker,
because plugin code runs only in a subprocess. Each written stem gets a render stamp recording the
inputs it came from, and a later run reuses a stem whose stamp still matches.
"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import mido
import numpy as np

from vsti_composer.plugin_check import (
    DEFAULT_CONFIG,
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    INITIALIZATION_TIMEOUT_S,
    NUM_CHANNELS,
    PROJECT_DIR,
    SAMPLE_RATE,
    SILENCE_DBFS,
    WORKER_TIMEOUT_S,
    ConfigError,
    PluginSpec,
    align_rows,
    load_config,
    measure_peak_dbfs,
)
from vsti_composer.presets import PresetError, build_state, preset_files, read_chunk
from vsti_composer.song import Note, Song, SongError, Track, is_int, is_number, load_song
from vsti_composer.sounds import SOUNDS_DIR, SoundError, read_sound, sound_files
from vsti_composer.worker import WorkerCrashedError, WorkerTimedOutError, run_isolated

if TYPE_CHECKING:
    from pedalboard_native import MIDIMessageLike

RENDERS_DIR = PROJECT_DIR / "renders"
# Directories, so a track named `mix` or `analysis` still writes its stem beside them.
MIX_DIR = "mix"
ANALYSIS_DIR = "analysis"
TAIL_S = 2.0
WAV_BIT_DEPTH = 32  # Float WAV, so levels above full scale survive until mixing.
# Raise whenever rendering code changes what a stem contains for the same inputs, so every
# existing stamp stops matching.
RENDER_FORMAT = 1

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


def song_duration_s(song: Song) -> float:
    """Return the length shared by every stem: the latest note end across tracks plus the tail."""
    last_beat = max(note.beat + note.length for track in song.tracks for note in track.notes)
    return song.seconds(last_beat) + TAIL_S


def expected_samples(duration_s: float) -> int:
    """Return the sample count Pedalboard renders, which truncates rather than rounds."""
    return int(duration_s * SAMPLE_RATE)


def track_messages(song: Song, track: Track) -> list[mido.Message]:
    """Build MIDI in time order, with note-off first at equal times so a repeated pitch retriggers.

    Times snap to whole samples first. Otherwise float error, as in beats 0.1 + 0.2 against 0.3,
    can place a note-off just after the note-on it should precede and silence the new note.
    Pedalboard reads `time` as absolute seconds, not mido deltas.
    """
    events = []
    for note in track.notes:
        start = round(song.seconds(note.beat) * SAMPLE_RATE)
        end = max(round(song.seconds(note.beat + note.length) * SAMPLE_RATE), start + 1)
        events.append((start, 1, mido.Message("note_on", note=note.pitch, velocity=note.velocity)))
        events.append((end, 0, mido.Message("note_off", note=note.pitch)))
    events.sort(key=lambda event: event[:2])
    return [message.copy(time=sample / SAMPLE_RATE) for sample, _, message in events]


def song_render_dir(output_dir: Path, song_name: str) -> Path:
    """Return the directory that holds one song's stems."""
    return output_dir / song_name


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
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def json_object(value: object) -> Mapping[str, object] | None:
    """Return parsed JSON as a mapping of untyped values, or None when it is not an object.

    The declared return type makes the type checker require each value to be checked before use.
    """
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return None


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


def clear_results(song_dir: Path) -> None:
    """Delete a song's mix and analysis, so nothing downstream reads results from older audio."""
    shutil.rmtree(song_dir / MIX_DIR, ignore_errors=True)
    shutil.rmtree(song_dir / ANALYSIS_DIR, ignore_errors=True)


def temp_stem_path(stem: Path) -> Path:
    # Pedalboard picks the file format from the extension, so the name must end in .wav.
    return stem.with_name(f".{stem.stem}.tmp.wav")


def worker_command(job_path: Path) -> list[str]:
    return [sys.executable, "-m", "vsti_composer.render", str(job_path)]


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
        },
        "duration_s": duration_s,
        "stem_path": str(stem),
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
            payload: object = run_isolated(worker_command(job_path), timeout_s)
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
    song_name: str,
    specs: list[PluginSpec],
    output_dir: Path = RENDERS_DIR,
    timeout_s: float = WORKER_TIMEOUT_S,
    sounds_dir: Path = SOUNDS_DIR,
    force: bool = False,
) -> list[TrackResult]:
    """Render each track whose inputs changed since its stem was written, or every track if forced.

    A stem is reused when its stamp matches the file and both the track's and the sound's keys.
    """
    duration_s = song_duration_s(song)
    song_dir = song_render_dir(output_dir, song_name)
    song_dir.mkdir(parents=True, exist_ok=True)
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
    parser.add_argument("song", type=Path, help="song file (YAML)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="plugin list (TOML)")
    parser.add_argument("--output-dir", type=Path, default=RENDERS_DIR, help="where stems go")
    parser.add_argument("--sounds-dir", type=Path, default=SOUNDS_DIR, help="saved sound files")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--force", action="store_true", help="render every track, even one with a matching stamp"
    )
    args = parser.parse_args(argv)

    try:
        song = load_song(args.song)
        specs = load_config(args.config)
    except (SongError, ConfigError) as error:
        print(f"render-song: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    results = render_song(
        song, args.song.stem, specs, args.output_dir, sounds_dir=args.sounds_dir, force=args.force
    )
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
    os.replace(temp, path)


def render_stem(job: RenderJob) -> Outcome:
    """Render one track inside the worker process and write its stem."""
    # Imported here so the parent never loads plugin code.
    from pedalboard import AudioUnitPlugin, VST3Plugin, load_plugin

    duration_s = job["duration_s"]
    track_data = job["track"]
    notes = tuple(Note(**note) for note in track_data["notes"])
    track = Track(track_data["name"], track_data["engine"], track_data["sound"], notes)
    song = Song(bpm=job["bpm"], tracks=(track,))

    # Any exception from loading third-party plugin code, or from it rejecting a saved state, is
    # the result being reported.
    try:
        # `load_plugin` is annotated as returning `ExternalPlugin`, which lacks `raw_state`, but it
        # returns one of these two classes.
        plugin = cast(
            VST3Plugin | AudioUnitPlugin,
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
    parser = argparse.ArgumentParser(prog="vsti_composer.render")
    parser.add_argument("job_path", type=Path)
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args(argv)

    # The result goes to a file because plugins may print to stdout.
    job: RenderJob = json.loads(args.job_path.read_text())  # Written by `_run_track_worker`.
    args.result_path.write_text(json.dumps(asdict(render_stem(job))))
    return 0


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv[1:]))
