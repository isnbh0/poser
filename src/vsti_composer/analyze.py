"""Mix a song's rendered stems and report defects an agent can read.

`analyze-song` reads WAV files only, so it runs in the parent process and loads no plugin. Stems
come from the song's track names, never from a directory listing, so a stem left by a removed or
renamed track cannot enter the mix.
"""

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

import numpy as np
import pyloudnorm
from matplotlib import mlab
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from pedalboard.io import ReadableAudioFile, WriteableAudioFile

from vsti_composer.plugin_check import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    NUM_CHANNELS,
    SAMPLE_RATE,
    SILENCE_DBFS,
    align_rows,
    measure_peak_dbfs,
)
from vsti_composer.render import (
    ANALYSIS_DIR,
    MIX_DIR,
    RENDERS_DIR,
    WAV_BIT_DEPTH,
    clear_results,
    expected_samples,
    read_stamp,
    song_duration_s,
    song_render_dir,
    stamp_matches_stem,
    stamp_path,
    stem_path,
    track_key,
)
from vsti_composer.song import Song, SongError, load_song

MIX_NAME = "mix"

SPECGRAM_NFFT = 2048
SPECGRAM_NOVERLAP = 1536
POWER_FLOOR = 1e-12  # -120 dB, which keeps the spectrogram's logarithm finite for silence.


class TrackStatus(StrEnum):
    ANALYZED = "analyzed"
    MISSING_STEM = "missing_stem"
    STALE_STEM = "stale_stem"


class Defect(StrEnum):
    SILENT = "silent"
    CLIPPING = "clipping"


@dataclass(frozen=True)
class Measurements:
    peak_dbfs: float
    integrated_lufs: float | None
    clipped_samples: int


@dataclass(frozen=True)
class TrackReport:
    name: str
    status: TrackStatus
    gain_db: float = 0.0
    peak_dbfs: float | None = None
    integrated_lufs: float | None = None
    clipped_samples: int | None = None
    spectrogram: str | None = None
    defects: list[Defect] = field(default_factory=list)
    detail: str | None = None


@dataclass(frozen=True)
class MixReport:
    path: str
    peak_dbfs: float
    integrated_lufs: float | None
    clipped_samples: int
    spectrogram: str
    defects: list[Defect]


@dataclass(frozen=True)
class Report:
    song: str
    tracks: list[TrackReport]
    mix: MixReport | None


class StaleStemError(Exception):
    """A stem exists but does not match the song as it is now."""


def read_stem(path: Path, stamp_file: Path, key: str, samples: int) -> np.ndarray:
    """Read a stem and confirm it matches the song. Raises StaleStemError when it does not.

    `key` is the track's current `track_key`. Its render stamp must record it, and must still
    describe the stem file. The sound's key is not checked, because analysis reads no plugin list.
    """
    stamp = read_stamp(stamp_file)
    if stamp is None:
        raise StaleStemError("stem has no render stamp")
    if not stamp_matches_stem(stamp, path):
        raise StaleStemError("stem changed after it was rendered")
    if stamp.track_key != key:
        raise StaleStemError("song changed since this stem was rendered")
    # The reader takes the frame count from the header and pads missing data with zeros, so a
    # truncated file would otherwise pass as quieter audio.
    if path.stat().st_size < samples * NUM_CHANNELS * WAV_BIT_DEPTH // 8:
        raise StaleStemError("stem file is shorter than its audio data")
    try:
        # Pedalboard's stubs mark `__new__` as a classmethod, which ty cannot match to any call.
        with ReadableAudioFile(str(path)) as stem:  # ty: ignore[no-matching-overload]
            rate, channels, frames = stem.samplerate, stem.num_channels, stem.frames
            if rate != SAMPLE_RATE:
                raise StaleStemError(f"sample rate {rate:g} Hz, expected {SAMPLE_RATE}")
            if channels != NUM_CHANNELS:
                raise StaleStemError(f"{channels} channels, expected {NUM_CHANNELS}")
            if frames != samples:
                raise StaleStemError(f"{frames} samples, expected {samples}")
            audio = stem.read(frames)
    except (OSError, ValueError, RuntimeError) as error:
        raise StaleStemError(f"cannot read stem: {error}") from error
    if audio.shape != (NUM_CHANNELS, samples):
        raise StaleStemError(f"read {audio.shape}, expected {(NUM_CHANNELS, samples)}")
    # Peak and clipping checks skip NaN, so a stem holding it would otherwise report clean.
    if not np.isfinite(audio).all():
        raise StaleStemError("stem contains NaN or infinity")
    return audio


def integrated_lufs(audio: np.ndarray) -> float | None:
    """Return loudness per ITU-R BS.1770, or None for silence, because JSON has no infinity."""
    # Pyloudnorm takes samples-first arrays, and stems are channels-first.
    loudness = pyloudnorm.Meter(SAMPLE_RATE).integrated_loudness(audio.T.astype(np.float64))
    return float(loudness) if math.isfinite(loudness) else None


def measure(audio: np.ndarray) -> Measurements:
    return Measurements(
        peak_dbfs=measure_peak_dbfs(audio),
        integrated_lufs=integrated_lufs(audio),
        clipped_samples=int(np.count_nonzero(np.abs(audio) > 1.0)),
    )


def write_spectrogram(audio: np.ndarray, path: Path, title: str) -> None:
    figure = Figure(figsize=(10, 4))
    FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    # This follows `Axes.specgram`, which takes the logarithm of the power directly, so silent
    # bins would give -inf and a warning. The floor goes on the power for that reason.
    power, freqs, times = (
        np.asarray(result, dtype=np.float64)
        for result in mlab.specgram(
            audio.sum(axis=0, dtype=np.float64),
            NFFT=SPECGRAM_NFFT,
            Fs=SAMPLE_RATE,
            noverlap=SPECGRAM_NOVERLAP,
        )
    )
    pad = (SPECGRAM_NFFT - SPECGRAM_NOVERLAP) / SAMPLE_RATE / 2
    extent = (float(times[0]) - pad, float(times[-1]) + pad, float(freqs[0]), float(freqs[-1]))
    axes.imshow(
        np.flipud(10.0 * np.log10(power + POWER_FLOOR)),
        extent=extent,
        origin="upper",  # As `Axes.specgram` sets it, independent of the rcParams default.
        aspect="auto",
    )
    axes.set_xlabel("Time (s)")
    axes.set_ylabel("Frequency (Hz)")
    axes.set_title(title)
    figure.savefig(path, format="png")


def write_wav(audio: np.ndarray, path: Path) -> None:
    """Write in the stem format through a temporary file, so a crash leaves no partial file."""
    # Pedalboard picks the file format from the extension, so the name must end in .wav.
    temp = path.with_name(f".{path.stem}.tmp.wav")
    try:
        with WriteableAudioFile(  # ty: ignore[no-matching-overload]
            str(temp), samplerate=SAMPLE_RATE, num_channels=NUM_CHANNELS, bit_depth=WAV_BIT_DEPTH
        ) as output:
            output.write(audio)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def silent_defects(measurements: Measurements) -> list[Defect]:
    return [Defect.SILENT] if measurements.peak_dbfs < SILENCE_DBFS else []


def analyze_song(song: Song, song_path: Path, output_dir: Path = RENDERS_DIR) -> Report:
    song_dir = song_render_dir(output_dir.resolve(), song_path.stem)
    mix_dir, analysis_dir = song_dir / MIX_DIR, song_dir / ANALYSIS_DIR
    samples = expected_samples(song_duration_s(song))

    stems: dict[str, np.ndarray] = {}
    failures: dict[str, TrackReport] = {}
    for track in song.tracks:
        path = stem_path(song_dir, track.name)
        if not path.is_file():
            failures[track.name] = TrackReport(
                track.name, TrackStatus.MISSING_STEM, gain_db=track.gain
            )
            continue
        try:
            stamp = stamp_path(song_dir, track.name)
            stems[track.name] = read_stem(path, stamp, track_key(song, track), samples)
        except StaleStemError as error:
            failures[track.name] = TrackReport(
                track.name, TrackStatus.STALE_STEM, gain_db=track.gain, detail=str(error)
            )

    # Results from older audio must not survive a run that cannot replace them.
    clear_results(song_dir)
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
        measurements = measure(audio)
        spectrogram = None
        if complete:
            spectrogram = analysis_dir / f"{track.name}.png"
            write_spectrogram(audio, spectrogram, track.name)
        tracks.append(
            TrackReport(
                name=track.name,
                status=TrackStatus.ANALYZED,
                gain_db=track.gain,
                **asdict(measurements),
                spectrogram=None if spectrogram is None else str(spectrogram),
                defects=silent_defects(measurements),
            )
        )

    mix = None
    if complete:
        # Each stem is scaled by its track's gain, then summed, with no limiter or normalization.
        # Accumulating in place keeps one float64 buffer, not a float64 copy of every stem.
        total = np.zeros((NUM_CHANNELS, samples), dtype=np.float64)
        for track in song.tracks:
            total += stems[track.name].astype(np.float64) * 10 ** (track.gain / 20)
        mixed = total.astype(np.float32)
        mix_path = mix_dir / f"{MIX_NAME}.wav"
        write_wav(mixed, mix_path)
        mix_png = mix_dir / f"{MIX_NAME}.png"
        write_spectrogram(mixed, mix_png, MIX_NAME)
        measurements = measure(mixed)
        defects = silent_defects(measurements)
        if measurements.clipped_samples > 0:
            defects.append(Defect.CLIPPING)
        mix = MixReport(
            path=str(mix_path), **asdict(measurements), spectrogram=str(mix_png), defects=defects
        )

    report = Report(song=song_path.stem, tracks=tracks, mix=mix)
    if complete:
        (analysis_dir / "report.json").write_text(report_json(report) + "\n")
    return report


def report_json(report: Report) -> str:
    # allow_nan=False makes a non-finite number an error instead of invalid JSON.
    return json.dumps(asdict(report), indent=2, allow_nan=False)


def exit_code(report: Report) -> int:
    clean = report.mix is not None and not report.mix.defects
    clean = clean and all(
        track.status is TrackStatus.ANALYZED and not track.defects for track in report.tracks
    )
    return EXIT_ALL_PASS if clean else EXIT_SOME_FAILED


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
    parser.add_argument("song", type=Path, help="song file (YAML)")
    parser.add_argument("--output-dir", type=Path, default=RENDERS_DIR, help="where stems are")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    try:
        song = load_song(args.song)
    except SongError as error:
        print(f"analyze-song: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    report = analyze_song(song, args.song, args.output_dir)
    print(report_json(report) if args.json else format_table(report))
    return exit_code(report)
