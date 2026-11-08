"""Render a song to one aligned WAV per track.

`render-song` is the parent command. Running this module with `python -m` is the per-track worker,
because plugin code runs only in a subprocess. Each written stem gets a render stamp recording the
inputs it came from, and a later run reuses a stem whose stamp still matches.
"""

import argparse
import base64
import fcntl
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from poser.adapters.pedalboard.formats import PresetError, read_chunk
from poser.adapters.pedalboard.state import build_state
from poser.adapters.pedalboard.worker import BUFFER_SIZE, performance_messages, render_duration_s
from poser.adapters.song_formats import SONG_FORMATS
from poser.adapters.wav import WavAudioFiles
from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    EXIT_SONG_BUSY,
    RENDER_TIMEOUT_S,
    align_rows,
)
from poser.core.audio import (
    NUM_CHANNELS,
    SAMPLE_RATE,
    SILENCE_DBFS,
    AudioProblem,
    make_audio,
    measure_peak_dbfs,
)
from poser.core.cache import track_key
from poser.core.jsonvalue import JsonValue, digest, json_object
from poser.core.loading import load_song
from poser.core.performance import perform, performance_from_data, performance_to_data
from poser.core.song import Song, SongError, Track, is_int, is_number
from poser.core.statuses import RenderFailureStatus, RenderStatus, SoundFailureStatus, StemStatus
from poser.plugin_check import INITIALIZATION_TIMEOUT_S, ConfigError, PluginSpec, load_config
from poser.presets import preset_files
from poser.sounds import SoundError, read_sound, sound_files
from poser.worker import (
    MalformedJobError,
    WorkerCrashedError,
    WorkerTimedOutError,
    job_fields,
    optional_str,
    run_isolated,
)
from poser.workspace import WorkspaceError, fill_path_options, song_render_dir

if TYPE_CHECKING:
    from pedalboard_native import MIDIMessageLike

# Directories, so a track named `mix` or `analysis` still writes its stem beside them.
MIX_DIR = "mix"
ANALYSIS_DIR = "analysis"
LOCK_NAME = ".lock"

WORKER_FIELDS = {"status", "peak_dbfs", "detail"}
STAMP_FIELDS = {"track_key", "sound_key", "status", "peak_dbfs", "stem_size", "stem_mtime_ns"}


def stem_status(value: object) -> StemStatus | None:
    """Return the value as a stem-writing status, or None for any other value."""
    return StemStatus(value) if isinstance(value, str) and value in StemStatus else None


def worker_status(value: object) -> StemStatus | RenderFailureStatus | None:
    """Return the value as a status a render worker reports, or None for any other value."""
    if isinstance(value, str) and value in RenderFailureStatus:
        return RenderFailureStatus(value)
    return stem_status(value)


@dataclass(frozen=True)
class Outcome:
    """What happened when one track was resolved and rendered, as the worker reports it."""

    status: RenderStatus
    peak_dbfs: float | None = None
    detail: str | None = None


class RenderJob(TypedDict):
    """Everything the worker renders from, sent as JSON. Gain is not sent; it applies at mixdown."""

    plugin_path: str
    plugin_name: str | None
    state: str | None
    performance: JsonValue
    stem_path: str


@dataclass(frozen=True)
class TrackResult:
    track: str
    status: RenderStatus
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


def stem_path(song_dir: Path, track_name: str) -> Path:
    """Return where a track's stem lives. Later steps find stems only through this rule."""
    return song_dir / f"{track_name}{WavAudioFiles().suffix}"


def sound_key(spec: PluginSpec, state: str | None) -> str:
    """Return a digest of a resolved sound, so editing a saved sound or preset changes it."""
    return digest({"plugin_path": spec.path, "plugin_name": spec.plugin_name, "state": state})


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


def remove_temporary_stems(stem: Path) -> None:
    """Remove the temporary files a killed worker could leave while writing the stem."""
    # Track names hold no dots, so this matches only this stem's temporary files.
    for leftover in stem.parent.glob(f".{stem.stem}.*{stem.suffix}"):
        leftover.unlink(missing_ok=True)


def worker_command(job_path: Path) -> list[str]:
    return [sys.executable, "-m", "poser.render", str(job_path)]


def render_job(
    song: Song, track: Track, spec: PluginSpec, state: str | None, stem: Path
) -> RenderJob:
    return {
        "plugin_path": spec.path,
        "plugin_name": spec.plugin_name,
        "state": state,
        "performance": performance_to_data(perform(song, track)),
        "stem_path": str(stem),
    }


def parse_render_job(value: object) -> RenderJob:
    """Return a render job read back from JSON, raising MalformedJobError if it is malformed.

    The parent writes every job with `render_job`, so a malformed one is a bug.
    """
    keys = set(RenderJob.__annotations__)
    fields = job_fields(value, keys, "render job")
    plugin_path, plugin_name, state = fields["plugin_path"], fields["plugin_name"], fields["state"]
    performance, stem = fields["performance"], fields["stem_path"]
    if not (isinstance(plugin_path, str) and optional_str(plugin_name) and optional_str(state)):
        raise MalformedJobError("render job", value)
    if not isinstance(stem, str):
        raise MalformedJobError("render job", value)
    try:
        parsed = performance_to_data(performance_from_data(performance))
    except ValueError as error:
        raise MalformedJobError("render job", value) from error
    return {
        "plugin_path": plugin_path,
        "plugin_name": plugin_name,
        "state": state,
        "performance": parsed,
        "stem_path": stem,
    }


def parse_worker_result(payload: object, stem: Path) -> Outcome:
    """Turn the worker's JSON result into an outcome, reporting anything malformed as a crash."""
    malformed = Outcome(RenderFailureStatus.CRASHED, detail="worker wrote a malformed result")
    fields = json_object(payload)
    if fields is None or not set(fields) <= WORKER_FIELDS:
        return malformed
    status = worker_status(fields.get("status"))
    peak, detail = fields.get("peak_dbfs"), fields.get("detail")
    if status is None:
        return Outcome(RenderFailureStatus.CRASHED, detail="worker wrote an unknown status")
    if not (peak is None or is_number(peak)) or not (detail is None or isinstance(detail, str)):
        return malformed
    outcome = Outcome(status, None if peak is None else float(peak), detail)
    if isinstance(outcome.status, StemStatus) and not stem.exists():
        return Outcome(
            RenderFailureStatus.CRASHED, detail="worker reported a stem it did not write"
        )
    return outcome


def _run_track_worker(job: RenderJob, timeout_s: float) -> Outcome:
    with tempfile.TemporaryDirectory() as directory:
        job_path = Path(directory) / "job.json"
        job_path.write_text(json.dumps(job))
        try:
            payload = run_isolated(worker_command(job_path), timeout_s)
        except WorkerTimedOutError as error:
            return Outcome(RenderFailureStatus.TIMED_OUT, detail=str(error))
        except WorkerCrashedError as error:
            return Outcome(RenderFailureStatus.CRASHED, detail=str(error))
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
        SoundFailureStatus.UNKNOWN_SOUND,
        detail=f"no plugin, saved sound, or preset named {sound!r}",
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
            return Outcome(SoundFailureStatus.LOAD_FAILED, detail=f"preset {sound!r}: {error}")
        return spec, base64.b64encode(state).decode("ascii")
    path = sound_files(sounds_dir).get(sound)
    if path is None:
        return unknown
    try:
        saved = read_sound(path, specs)
    except SoundError as error:
        return Outcome(SoundFailureStatus.LOAD_FAILED, detail=f"saved sound {sound!r}: {error}")
    return specs_by_name[saved.plugin], saved.state


def render_song(
    song: Song,
    song_dir: Path,
    specs: list[PluginSpec],
    *,
    sounds_dir: Path,
    timeout_s: float = RENDER_TIMEOUT_S,
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
        job = render_job(song, track, spec, state, stem)
        outcome = _run_track_worker(job, timeout_s)
        remove_temporary_stems(stem)
        written = outcome.status if isinstance(outcome.status, StemStatus) else None
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
    all_rendered = all(result.status is StemStatus.RENDERED for result in results)
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
        song = load_song(args.song, SONG_FORMATS)
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


def render_stem(job: RenderJob) -> Outcome:
    """Render one track inside the worker process and write its stem."""
    # Imported here so the parent never loads plugin code.
    from pedalboard import AudioUnitPlugin, VST3Plugin, load_plugin

    # `parse_render_job` checked the performance.
    performance = performance_from_data(job["performance"])

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
        return Outcome(RenderFailureStatus.LOAD_FAILED, detail=str(error))

    # Pedalboard's reset rebuilds an instrument by saving its state, recreating it, and restoring
    # that state, so a restored sound survives. Serum renders differently without the reset.
    # Pedalboard's stubs take a `list` of its message union, and `list` is invariant.
    messages: list[MIDIMessageLike] = [*performance_messages(performance)]
    rendered = plugin(
        messages,
        duration=render_duration_s(performance.samples),
        sample_rate=SAMPLE_RATE,
        num_channels=NUM_CHANNELS,
        buffer_size=BUFFER_SIZE,
        reset=True,
    )
    audio = make_audio(rendered, performance.samples)
    if isinstance(audio, AudioProblem):
        return Outcome(RenderFailureStatus.CRASHED, detail=audio.message)

    WavAudioFiles().write(Path(job["stem_path"]), audio)

    peak_dbfs = measure_peak_dbfs(audio.samples)
    status = StemStatus.SILENT if peak_dbfs < SILENCE_DBFS else StemStatus.RENDERED
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
