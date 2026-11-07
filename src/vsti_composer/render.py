"""Render a song to one aligned WAV per track.

`render-song` is the parent command. Running this module with `python -m` is the per-track worker,
because plugin code runs only in a subprocess.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

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
from vsti_composer.song import Note, Song, SongError, Track, load_song
from vsti_composer.worker import WorkerCrashedError, WorkerTimedOutError, run_isolated

RENDERS_DIR = PROJECT_DIR / "renders"
# Directories, so a track named `mix` or `analysis` still writes its stem beside them.
MIX_DIR = "mix"
ANALYSIS_DIR = "analysis"
TAIL_S = 2.0
WAV_BIT_DEPTH = 32  # Float WAV, so levels above full scale survive until mixing.

WORKER_FIELDS = {"status", "peak_dbfs", "detail"}


class TrackStatus(StrEnum):
    RENDERED = "rendered"
    SILENT = "silent"
    UNKNOWN_SOUND = "unknown_sound"
    LOAD_FAILED = "load_failed"
    CRASHED = "crashed"
    TIMED_OUT = "timed_out"


STEM_WRITTEN = {TrackStatus.RENDERED, TrackStatus.SILENT}


@dataclass(frozen=True)
class TrackResult:
    track: str
    status: TrackStatus
    peak_dbfs: float | None = None
    stem: str | None = None
    detail: str | None = None


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


def clear_results(song_dir: Path) -> None:
    """Delete a song's mix and analysis, so nothing downstream reads results from older audio."""
    shutil.rmtree(song_dir / MIX_DIR, ignore_errors=True)
    shutil.rmtree(song_dir / ANALYSIS_DIR, ignore_errors=True)


def temp_stem_path(stem: Path) -> Path:
    # Pedalboard picks the file format from the extension, so the name must end in .wav.
    return stem.with_name(f".{stem.stem}.tmp.wav")


def worker_command(job_path: Path) -> list[str]:
    return [sys.executable, "-m", "vsti_composer.render", str(job_path)]


def _run_track_worker(
    song: Song, track: Track, spec: PluginSpec, stem: Path, duration_s: float, timeout_s: float
) -> dict[str, Any]:
    job = {
        "plugin_path": spec.path,
        "plugin_name": spec.plugin_name,
        "bpm": song.bpm,
        "track": asdict(track),
        "duration_s": duration_s,
        "stem_path": str(stem),
    }
    with tempfile.TemporaryDirectory() as directory:
        job_path = Path(directory) / "job.json"
        job_path.write_text(json.dumps(job))
        try:
            payload = run_isolated(worker_command(job_path), timeout_s)
        except WorkerTimedOutError as error:
            return {"status": TrackStatus.TIMED_OUT, "detail": str(error)}
        except WorkerCrashedError as error:
            return {"status": TrackStatus.CRASHED, "detail": str(error)}

    if not isinstance(payload, dict) or not set(payload) <= WORKER_FIELDS:
        return {"status": TrackStatus.CRASHED, "detail": "worker wrote a malformed result"}
    try:
        payload["status"] = TrackStatus(payload.get("status"))
    except ValueError:
        return {"status": TrackStatus.CRASHED, "detail": "worker wrote an unknown status"}
    if payload["status"] in STEM_WRITTEN and not stem.exists():
        return {"status": TrackStatus.CRASHED, "detail": "worker reported a stem it did not write"}
    return payload


def render_song(
    song: Song,
    song_name: str,
    specs: list[PluginSpec],
    output_dir: Path = RENDERS_DIR,
    timeout_s: float = WORKER_TIMEOUT_S,
) -> list[TrackResult]:
    specs_by_name = {spec.name: spec for spec in specs}
    duration_s = song_duration_s(song)
    song_dir = song_render_dir(output_dir, song_name)
    song_dir.mkdir(parents=True, exist_ok=True)
    clear_results(song_dir)

    results = []
    for track in song.tracks:
        stem = stem_path(song_dir, track.name)
        spec = specs_by_name.get(track.sound)
        payload: dict[str, Any]
        if spec is None:
            payload = {
                "status": TrackStatus.UNKNOWN_SOUND,
                "detail": f"no sound named {track.sound!r} in the plugin list",
            }
        else:
            payload = _run_track_worker(song, track, spec, stem, duration_s, timeout_s)

        temp_stem_path(stem).unlink(missing_ok=True)
        written = payload["status"] in STEM_WRITTEN
        if not written:
            # A failed track leaves no earlier stem behind for later steps to use by mistake.
            stem.unlink(missing_ok=True)
        results.append(
            TrackResult(track=track.name, stem=str(stem) if written else None, **payload)
        )
    return results


def exit_code(results: list[TrackResult]) -> int:
    all_rendered = all(result.status is TrackStatus.RENDERED for result in results)
    return EXIT_ALL_PASS if all_rendered else EXIT_SOME_FAILED


def format_table(results: list[TrackResult]) -> str:
    rows = [("TRACK", "STATUS", "PEAK DBFS", "STEM")]
    details = []
    for result in results:
        peak = "-" if result.peak_dbfs is None else f"{result.peak_dbfs:.1f}"
        rows.append((result.track, str(result.status), peak, result.stem or "-"))
        if result.detail:
            details.append(f"{result.track}: {result.detail}")
    return "\n".join([*align_rows(rows), *details])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render-song", description=__doc__)
    parser.add_argument("song", type=Path, help="song file (YAML)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="plugin list (TOML)")
    parser.add_argument("--output-dir", type=Path, default=RENDERS_DIR, help="where stems go")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    try:
        song = load_song(args.song)
        specs = load_config(args.config)
    except (SongError, ConfigError) as error:
        print(f"render-song: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    results = render_song(song, args.song.stem, specs, args.output_dir)
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(format_table(results))
    return exit_code(results)


def render_stem(job: dict[str, Any]) -> dict[str, Any]:
    """Render one track inside the worker process and write its stem."""
    from pedalboard import load_plugin  # Imported here so the parent never loads plugin code.
    from pedalboard.io import WriteableAudioFile

    duration_s = job["duration_s"]
    track_data = job["track"]
    notes = tuple(Note(**note) for note in track_data["notes"])
    track = Track(track_data["name"], track_data["engine"], track_data["sound"], notes)
    song = Song(bpm=job["bpm"], tracks=(track,))

    # Any exception from loading third-party plugin code is the result being reported.
    try:
        plugin: Any = load_plugin(
            job["plugin_path"],
            plugin_name=job["plugin_name"],
            initialization_timeout=INITIALIZATION_TIMEOUT_S,
        )
    except Exception as error:  # noqa: BLE001
        return {"status": TrackStatus.LOAD_FAILED, "detail": str(error)}

    audio = plugin(
        track_messages(song, track),
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

    stem = Path(job["stem_path"])
    temp = temp_stem_path(stem)
    # Pedalboard's stubs mark `__new__` as a classmethod, which ty cannot match to any call.
    with WriteableAudioFile(  # ty: ignore[no-matching-overload]
        str(temp), samplerate=SAMPLE_RATE, num_channels=NUM_CHANNELS, bit_depth=WAV_BIT_DEPTH
    ) as output:
        output.write(audio)
    os.replace(temp, stem)

    peak_dbfs = measure_peak_dbfs(audio)
    status = TrackStatus.SILENT if peak_dbfs < SILENCE_DBFS else TrackStatus.RENDERED
    return {"status": status, "peak_dbfs": peak_dbfs}


def worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="vsti_composer.render")
    parser.add_argument("job_path", type=Path)
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args(argv)

    # The result goes to a file because plugins may print to stdout.
    result = render_stem(json.loads(args.job_path.read_text()))
    args.result_path.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv[1:]))
