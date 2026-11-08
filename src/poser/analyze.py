"""Mix a song's rendered stems and report defects an agent can read.

`analyze-song` reads stems, the plugin list, saved sounds, and presets, but loads no plugin, so it
runs in the parent process. Stems
come from the song's track names, never from a directory listing, so a stem left by a removed or
renamed track cannot enter the mix.
"""

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from poser.adapters.measuring import StandardMeasurer
from poser.adapters.mixing import GainMixer
from poser.adapters.song_formats import SONG_FORMATS, YamlSongFormat
from poser.adapters.spectrogram import SpectrogramVisualizer
from poser.adapters.wav import WavAudioFiles
from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    EXIT_SONG_BUSY,
    align_rows,
)
from poser.core.analysis import measure
from poser.core.audio import Audio, stem_samples
from poser.core.cache import StampProblem, track_key
from poser.core.loading import load_song
from poser.core.names import TrackName
from poser.core.ports import AudioFileError, MixInput
from poser.core.report import (
    MIX_NAME,
    AnalysisStatus,
    MixReport,
    Report,
    TrackReport,
    mix_defects,
    report_failed,
    report_to_data,
    silent_defects,
)
from poser.core.song import Song, SongError
from poser.plugin_check import ConfigError, PluginSpec, load_config
from poser.render import (
    ANALYSIS_DIR,
    MIX_DIR,
    Outcome,
    SongBusyError,
    clear_results,
    read_stamp,
    resolve_sound,
    song_lock,
    sound_key,
    stamp_matches_stem,
    stamp_path,
    stem_path,
)
from poser.workspace import WorkspaceError, fill_path_options, song_render_dir

HISTORY_DIR = "history"


class StaleStemError(Exception):
    """A stem exists but does not match the song as it is now."""


def read_stem(path: Path, stamp_file: Path, key: str, sound: str, samples: int) -> Audio:
    """Read a stem and confirm it matches the song. Raises StaleStemError when it does not.

    `key` and `sound` are the track's current `track_key` and `sound_key`. Its render stamp must
    record both, and must still describe the stem file.
    """
    stamp = read_stamp(stamp_file)
    if stamp is None:
        raise StaleStemError(StampProblem.MISSING)
    if not stamp_matches_stem(stamp, path):
        raise StaleStemError(StampProblem.CHANGED)
    if stamp.track_key != key:
        raise StaleStemError("song changed since this stem was rendered")
    if stamp.sound_key != sound:
        raise StaleStemError("sound changed since this stem was rendered")
    try:
        return WavAudioFiles().read(path, samples)
    except AudioFileError as error:
        raise StaleStemError(str(error)) from error


def start_record(song_dir: Path, song: Song) -> tuple[Path, int, str]:
    """Create the next numbered history record holding the song, and return it, its number, and
    the song text's digest."""
    history = song_dir / HISTORY_DIR
    history.mkdir(parents=True, exist_ok=True)
    taken = [int(entry.name) for entry in history.iterdir() if entry.name.isdecimal()]
    run = max(taken, default=0) + 1
    record = history / str(run)
    record.mkdir()
    writer = YamlSongFormat()
    song_file = record / f"song{writer.suffix}"
    temp = record / f".song{writer.suffix}.tmp"
    temp.write_text(writer.dump(song))
    temp.replace(song_file)
    return record, run, hashlib.sha256(song_file.read_bytes()).hexdigest()


def finish_record(record: Path, report: Report) -> None:
    """Write the report into its record, pointing at the record's own copies of the images."""

    def copy(image: str | None) -> str | None:
        if image is None:
            return None
        target = record / Path(image).name
        shutil.copyfile(image, target)
        return str(target)

    tracks = tuple(replace(track, spectrogram=copy(track.spectrogram)) for track in report.tracks)
    mix = report.mix
    if mix is not None:
        # The mix image is copied under its own name, which no track name can take.
        target = record / f"[{MIX_NAME}].png"
        shutil.copyfile(mix.spectrogram, target)
        mix = replace(mix, spectrogram=str(target))
    (record / "report.json").write_text(report_json(replace(report, tracks=tracks, mix=mix)) + "\n")


def analyze_song(
    song: Song,
    song_path: Path,
    specs: list[PluginSpec],
    song_dir: Path,
    sounds_dir: Path,
) -> Report:
    """Mix and measure a song's stems, and keep the analysis as the next history record.

    Raises SongBusyError when another render-song or analyze-song holds the song's lock.
    """
    song_dir = song_dir.resolve()
    with song_lock(song_dir):
        return _analyze(song, song_path, song_dir, specs, sounds_dir)


def _analyze(
    song: Song, song_path: Path, song_dir: Path, specs: list[PluginSpec], sounds_dir: Path
) -> Report:
    mix_dir, analysis_dir = song_dir / MIX_DIR, song_dir / ANALYSIS_DIR
    samples = stem_samples(song)
    measurer, visualizer = StandardMeasurer(), SpectrogramVisualizer()

    keys: dict[str, tuple[str, str | None]] = {}
    stems: dict[str, Audio] = {}
    failures: dict[str, TrackReport] = {}
    for track in song.tracks:
        key = track_key(song, track)
        path = stem_path(song_dir, track.name)
        if not path.is_file():
            keys[track.name] = (key, None)
            failures[track.name] = TrackReport(
                track.name, AnalysisStatus.MISSING_STEM, key, None, gain_db=track.gain
            )
            continue
        resolved = resolve_sound(track.sound, specs, sounds_dir)
        if isinstance(resolved, Outcome):
            keys[track.name] = (key, None)
            failures[track.name] = TrackReport(
                track.name,
                AnalysisStatus.STALE_STEM,
                key,
                None,
                gain_db=track.gain,
                detail=resolved.detail,
            )
            continue
        sound = sound_key(*resolved)
        keys[track.name] = (key, sound)
        try:
            stamp = stamp_path(song_dir, track.name)
            stems[track.name] = read_stem(path, stamp, key, sound, samples)
        except StaleStemError as error:
            failures[track.name] = TrackReport(
                track.name,
                AnalysisStatus.STALE_STEM,
                key,
                sound,
                gain_db=track.gain,
                detail=str(error),
            )

    # Results from older audio must not survive a run that cannot replace them.
    clear_results(song_dir)
    record, run, song_sha256 = start_record(song_dir, song)
    complete = not failures
    if complete:
        mix_dir.mkdir(parents=True)
        analysis_dir.mkdir(parents=True)

    tracks = []
    for track in song.tracks:
        if track.name in failures:
            tracks.append(failures[track.name])
            continue
        audio = stems[track.name]
        measurements = measure(audio, song, measurer)
        spectrogram = None
        if complete:
            spectrogram = analysis_dir / f"{track.name}{visualizer.suffix}"
            visualizer.draw(audio, spectrogram, track.name)
        tracks.append(
            TrackReport(
                track.name,
                AnalysisStatus.ANALYZED,
                *keys[track.name],
                gain_db=track.gain,
                peak_dbfs=measurements.peak_dbfs,
                integrated_lufs=measurements.integrated_lufs,
                clipped_samples=measurements.clipped_samples,
                bands_db=measurements.bands_db,
                bar_dbfs=measurements.bar_dbfs,
                spectrogram=None if spectrogram is None else str(spectrogram),
                defects=tuple(silent_defects(measurements)),
            )
        )

    mix = None
    if complete:
        inputs = [
            MixInput(TrackName(track.name), stems[track.name], track.gain) for track in song.tracks
        ]
        mixed = GainMixer().mix(inputs, samples)
        mix_path = mix_dir / f"{MIX_NAME}{WavAudioFiles().suffix}"
        WavAudioFiles().write(mix_path, mixed)
        mix_png = mix_dir / f"{MIX_NAME}{visualizer.suffix}"
        visualizer.draw(mixed, mix_png, MIX_NAME)
        measurements = measure(mixed, song, measurer)
        mix = MixReport(
            path=str(mix_path),
            peak_dbfs=measurements.peak_dbfs,
            integrated_lufs=measurements.integrated_lufs,
            clipped_samples=measurements.clipped_samples,
            bands_db=measurements.bands_db,
            bar_dbfs=measurements.bar_dbfs,
            spectrogram=str(mix_png),
            defects=tuple(mix_defects(measurements)),
        )

    report = Report(
        song=song_path.stem, run=run, song_sha256=song_sha256, tracks=tuple(tracks), mix=mix
    )
    if complete:
        (analysis_dir / "report.json").write_text(report_json(report) + "\n")
    finish_record(record, report)
    return report


def report_json(report: Report) -> str:
    # allow_nan=False makes a non-finite number an error instead of invalid JSON.
    return json.dumps(report_to_data(report), indent=2, allow_nan=False)


def exit_code(report: Report) -> int:
    return EXIT_SOME_FAILED if report_failed(report) else EXIT_ALL_PASS


def format_table(report: Report) -> str:
    def number(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f}"

    def row(name: str, status: str, values: TrackReport | MixReport) -> tuple[str, ...]:
        clipped = "-" if values.clipped_samples is None else str(values.clipped_samples)
        gain = number(values.gain_db) if isinstance(values, TrackReport) else "-"
        return (
            name,
            status,
            gain,
            number(values.peak_dbfs),
            number(values.integrated_lufs),
            clipped,
            ", ".join(values.defects) or "-",
        )

    rows = [("NAME", "STATUS", "GAIN DB", "PEAK DBFS", "LUFS", "CLIPPED", "DEFECTS")]
    details = []
    for track in report.tracks:
        rows.append(row(track.name, str(track.status), track))
        if track.detail:
            details.append(f"{track.name}: {track.detail}")
    # Brackets cannot appear in a track name, so this row cannot be mistaken for a track.
    if report.mix is None:
        rows.append(("[mix]", "not_written", "-", "-", "-", "-", "-"))
    else:
        rows.append(row("[mix]", "written", report.mix))
    return "\n".join([*align_rows(rows), *details])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analyze-song", description=__doc__)
    parser.add_argument("song", type=Path, help="song file (YAML, or a script defining build())")
    parser.add_argument("--config", type=Path, help="plugin list (TOML)")
    parser.add_argument("--output-dir", type=Path, help="where stems are")
    parser.add_argument("--sounds-dir", type=Path, help="saved sound files")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    try:
        base = fill_path_options(args)
        # Before loading, so a refused song's script never runs.
        song_dir = song_render_dir(args.output_dir, base, args.song)
    except WorkspaceError as error:
        print(f"analyze-song: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    try:
        song = load_song(args.song, SONG_FORMATS)
        specs = load_config(args.config)
    except (SongError, ConfigError) as error:
        print(f"analyze-song: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    try:
        report = analyze_song(song, args.song, specs, song_dir, args.sounds_dir)
    except SongBusyError as error:
        print(f"analyze-song: {error}", file=sys.stderr)
        return EXIT_SONG_BUSY
    print(report_json(report) if args.json else format_table(report))
    return exit_code(report)
