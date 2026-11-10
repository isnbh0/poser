"""Analysis: mixing a song's current stems, measuring them, and keeping the report as a record.

Stems come from the song's track names, never from a listing, so a stem left by a removed or
renamed track cannot enter the mix. Every analysis that runs to the end keeps a record, even a
failed one; the latest results are written only when every track analyzed.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

import numpy as np

from poser.core.audio import Audio, measure_peak_dbfs, stem_samples
from poser.core.cache import StampProblem, track_key
from poser.core.names import Identifier, SongId, TrackName
from poser.core.ports import (
    AudioFileError,
    AudioFiles,
    Instrument,
    Measurer,
    Mixer,
    MixInput,
    RecordWriter,
    RenderStore,
    ReportSection,
    SectionInput,
    SectionResult,
    SongStore,
    SoundFailure,
    Visualizer,
)
from poser.core.rendering import require_stem_format, resolve_track
from poser.core.report import (
    BAR_BEATS,
    MIX_NAME,
    AnalysisStatus,
    Measurements,
    MixReport,
    Report,
    SectionData,
    SectionError,
    SectionReport,
    TrackReport,
    bar_dbfs,
    mix_defects,
    report_to_data,
    silent_defects,
)
from poser.core.song import Song, song_last_beat


def measure(audio: Audio, song: Song, measurer: Measurer) -> Measurements:
    """Measure audio: its peak, clipping, and bar levels here, and its loudness and bands through
    the measurer."""
    samples = audio.samples
    levels = measurer.measure(audio)
    # Counted in beats, so a bar starting exactly at the last beat is never included.
    bars = math.ceil(song_last_beat(song) / BAR_BEATS)
    return Measurements(
        peak_dbfs=measure_peak_dbfs(samples),
        integrated_lufs=levels.integrated_lufs,
        clipped_samples=int(np.count_nonzero(np.abs(samples) > 1.0)),
        bands_db=levels.bands_db,
        bar_dbfs=tuple(bar_dbfs(samples, song.seconds(BAR_BEATS), bars)),
    )


@dataclass(frozen=True)
class AnalysisTools:
    """The analysis adapters, carried as one argument."""

    mixer: Mixer
    measurer: Measurer
    visualizer: Visualizer
    sections: tuple[ReportSection, ...] = ()

    def __post_init__(self) -> None:
        sections = self.sections
        if not isinstance(sections, Sequence) or isinstance(sections, str):
            raise ValueError(f"AnalysisTools.sections must be a sequence, got {sections!r}")
        sections = tuple(cast("Sequence[object]", sections))
        names: set[str] = set()
        for section in sections:
            if not isinstance(section, ReportSection):
                raise ValueError(f"AnalysisTools.sections must hold sections, got {section!r}")
            if not isinstance(section.name, Identifier):
                raise ValueError(f"a section's name must be an Identifier, got {section.name!r}")
            if section.name in names:
                raise ValueError(f"two report sections are named {section.name!r}")
            names.add(section.name)
        object.__setattr__(self, "sections", sections)


class StaleStemError(Exception):
    """A stem exists but does not match the song as it is now."""


def read_stem(
    store: SongStore,
    track: TrackName,
    keys: tuple[str, str],
    audio_files: AudioFiles,
    frames: int,
) -> Audio:
    """Read a track's stem and confirm it matches the song. Raises StaleStemError when it does not.

    `keys` are the track's current `track_key` and `sound_key`. Its stamp must still describe the
    stem file and record both.
    """
    stamp = store.read_stamp(track)
    if isinstance(stamp, StampProblem):
        raise StaleStemError(stamp.value)
    if stamp.track_key != keys[0]:
        raise StaleStemError("song changed since this stem was rendered")
    if stamp.sound_key != keys[1]:
        raise StaleStemError("sound changed since this stem was rendered")
    try:
        return audio_files.read(store.stem_path(track), frames)
    except AudioFileError as error:
        raise StaleStemError(str(error)) from error


def report_json(report: Report) -> str:
    """Return the report as the JSON text kept in files."""
    # allow_nan=False makes a non-finite number an error instead of invalid JSON.
    return json.dumps(report_to_data(report), indent=2, allow_nan=False) + "\n"


def analyze_song(
    song: Song,
    song_name: str,
    renders: RenderStore,
    song_id: SongId,
    instruments: Mapping[Identifier, Instrument],
    audio_files: AudioFiles,
    record_writer: RecordWriter,
    tools: AnalysisTools,
) -> Report:
    """Mix and measure a song's stems, and keep the analysis as the next record.

    `song_name` is the song file's name without its suffix. Raises SongBusyError when another run
    holds the song's lock, and ValueError when the store's stem suffix differs from the audio
    files' or the mixer returns the wrong length. An OSError from the store, the audio files, the
    mixer, or the visualizer propagates after the lock is released.
    """
    require_stem_format(renders, audio_files)
    store = renders.song(song_id)
    with store.lock():
        return _analyze(song, song_name, store, instruments, audio_files, record_writer, tools)


def _analyze(
    song: Song,
    song_name: str,
    store: SongStore,
    instruments: Mapping[Identifier, Instrument],
    audio_files: AudioFiles,
    record_writer: RecordWriter,
    tools: AnalysisTools,
) -> Report:
    frames = stem_samples(song)
    sound_keys: dict[TrackName, str] = {}
    stems: dict[TrackName, Audio] = {}
    failures: dict[TrackName, TrackReport] = {}
    for track in song.tracks:
        name = track.track_name
        key = track_key(song, track)
        if not store.stem_path(name).is_file():
            failures[name] = TrackReport(
                track.name, AnalysisStatus.MISSING_STEM, key, None, gain_db=track.gain
            )
            continue
        resolved = resolve_track(track, instruments)
        if isinstance(resolved, SoundFailure):
            failures[name] = TrackReport(
                track.name,
                AnalysisStatus.STALE_STEM,
                key,
                None,
                gain_db=track.gain,
                detail=resolved.detail,
            )
            continue
        sound_keys[name] = resolved.sound_key
        try:
            stems[name] = read_stem(store, name, (key, resolved.sound_key), audio_files, frames)
        except StaleStemError as error:
            failures[name] = TrackReport(
                track.name,
                AnalysisStatus.STALE_STEM,
                key,
                resolved.sound_key,
                gain_db=track.gain,
                detail=str(error),
            )

    # Results from older audio must not survive a run that cannot replace them.
    store.clear_latest()
    song_text = record_writer.dump(song)
    record = store.start_record(song_text, record_writer.suffix)
    song_sha256 = hashlib.sha256(song_text.encode("utf-8")).hexdigest()
    complete = not failures
    suffix = tools.visualizer.suffix

    tracks: list[TrackReport] = []
    for track in song.tracks:
        name = track.track_name
        if name in failures:
            tracks.append(failures[name])
            continue
        audio = stems[name]
        measurements = measure(audio, song, tools.measurer)
        sections = _sections(tools.sections, SectionInput(song, track, audio, measurements))
        image = None
        if complete:
            image = store.image_path(name, suffix)
            tools.visualizer.draw(audio, image, track.name)
        tracks.append(
            TrackReport(
                track.name,
                AnalysisStatus.ANALYZED,
                track_key(song, track),
                sound_keys[name],
                gain_db=track.gain,
                peak_dbfs=measurements.peak_dbfs,
                integrated_lufs=measurements.integrated_lufs,
                clipped_samples=measurements.clipped_samples,
                bands_db=measurements.bands_db,
                bar_dbfs=measurements.bar_dbfs,
                spectrogram=None if image is None else str(image),
                defects=tuple(silent_defects(measurements)),
                sections=sections,
            )
        )

    mix = None
    if complete:
        inputs = tuple(
            MixInput(track.track_name, stems[track.track_name], track.gain) for track in song.tracks
        )
        mixed = tools.mixer.mix(inputs, frames)
        if mixed.frames != frames:
            raise ValueError(f"the mixer returned {mixed.frames} frames, expected {frames}")
        mix_path = store.mix_path()
        audio_files.write(mix_path, mixed)
        mix_image = store.image_path(None, suffix)
        tools.visualizer.draw(mixed, mix_image, MIX_NAME)
        measurements = measure(mixed, song, tools.measurer)
        mix = MixReport(
            path=str(mix_path),
            peak_dbfs=measurements.peak_dbfs,
            integrated_lufs=measurements.integrated_lufs,
            clipped_samples=measurements.clipped_samples,
            bands_db=measurements.bands_db,
            bar_dbfs=measurements.bar_dbfs,
            spectrogram=str(mix_image),
            defects=tuple(mix_defects(measurements)),
            sections=_sections(tools.sections, SectionInput(song, None, mixed, measurements)),
        )

    report = Report(
        song=song_name, run=record.run, song_sha256=song_sha256, tracks=tuple(tracks), mix=mix
    )
    if complete:
        store.report_path().write_text(report_json(report), encoding="utf-8")

    # The record's report points at the record's own copies of the images.
    kept = tuple(
        report_track
        if report_track.spectrogram is None
        else replace(
            report_track,
            spectrogram=str(store.keep_image(record, track.track_name, suffix)),
        )
        for track, report_track in zip(song.tracks, report.tracks, strict=True)
    )
    kept_mix = (
        None
        if report.mix is None
        else replace(report.mix, spectrogram=str(store.keep_image(record, None, suffix)))
    )
    store.finish_record(record, report_json(replace(report, tracks=kept, mix=kept_mix)))
    return report


def _sections(
    sections: Sequence[ReportSection], request: SectionInput
) -> Mapping[str, SectionReport]:
    """Run every section in order on one track or the mix, recording each failure as its error."""
    return {section.name: _section(section, request) for section in sections}


def _section(section: ReportSection, request: SectionInput) -> SectionReport:
    version = section.version
    try:
        result = section.mix(request) if request.track is None else section.track(request)
        if not isinstance(result, SectionResult):
            raise TypeError(f"returned {type(result).__name__}, not a SectionResult")  # noqa: TRY301
    except Exception as error:  # noqa: BLE001 -- a section's failure is reported, not raised.
        return SectionError(version, f"{type(error).__name__}: {error}")
    return SectionData(version, result.data, result.defects)
