"""Analysis: mixing a song's current stems, measuring them, and keeping the report as a record.

Stems come from the song's node names, never from a listing, so a stem left by a removed or
renamed node cannot enter the mix. Every analysis that runs to the end keeps a record, even a
failed one; the latest results are written only when every node analyzed.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace

import numpy as np

from poser.core.audio import Audio, measure_peak_dbfs, stem_samples
from poser.core.cache import StampProblem
from poser.core.graph import Graph, GraphNode
from poser.core.loading import LoadedSong
from poser.core.names import Identifier, NodeName, SongId
from poser.core.planning import (
    EMPTY_EFFECTS,
    ResolvedEffectNode,
    ResolvedInstrument,
    Unplanned,
    plan,
    resolve_all,
)
from poser.core.ports import (
    AudioFileError,
    AudioFiles,
    Effect,
    Instrument,
    Measurer,
    Mixer,
    MixInput,
    RecordWriter,
    RenderStore,
    SongStore,
    Visualizer,
)
from poser.core.rendering import require_stem_format
from poser.core.report import (
    BAR_BEATS,
    MIX_NAME,
    AnalysisStatus,
    Measurements,
    MixReport,
    NodeReport,
    Report,
    bar_dbfs,
    mix_defects,
    report_to_data,
    silent_defects,
)
from poser.core.session import song_session
from poser.core.song import song_last_beat


def measure(audio: Audio, graph: Graph, measurer: Measurer) -> Measurements:
    """Measure audio: its peak, clipping, and bar levels here, and its loudness and bands through
    the measurer."""
    samples = audio.samples
    levels = measurer.measure(audio)
    # Counted in beats, so a bar starting exactly at the last beat is never included.
    bars = math.ceil(song_last_beat(graph) / BAR_BEATS)
    return Measurements(
        peak_dbfs=measure_peak_dbfs(samples),
        integrated_lufs=levels.integrated_lufs,
        clipped_samples=int(np.count_nonzero(np.abs(samples) > 1.0)),
        bands_db=levels.bands_db,
        bar_dbfs=tuple(bar_dbfs(samples, graph.seconds(BAR_BEATS), bars)),
    )


@dataclass(frozen=True)
class AnalysisTools:
    """The analysis adapters, carried as one argument."""

    mixer: Mixer
    measurer: Measurer
    visualizer: Visualizer


class StaleStemError(Exception):
    """A stem exists but does not match the song as it is now."""


def read_stem(
    store: SongStore,
    node: NodeName,
    key: str,
    sound_key: str | None,
    audio_files: AudioFiles,
    frames: int,
) -> Audio:
    """Read a node's stem and confirm it matches the song. Raises StaleStemError when it does not.

    `key` is the key of the node's render unit in the current plan, and `sound_key` its resolved
    sound or recipe, or None for a node without one. Its stamp must still describe the stem file
    and record both.
    """
    stamp = store.read_stamp(node)
    if isinstance(stamp, StampProblem):
        raise StaleStemError(stamp.value)
    # The sound is checked first, because the unit key digests it and would mask which input
    # changed.
    if sound_key is not None and stamp.sound_key != sound_key:
        raise StaleStemError("sound changed since this stem was rendered")
    if stamp.track_key != key:
        raise StaleStemError("song changed since this stem was rendered")
    try:
        return audio_files.read(store.stem_path(node), frames)
    except AudioFileError as error:
        raise StaleStemError(str(error)) from error


def report_json(report: Report) -> str:
    """Return the report as the JSON text kept in files."""
    # allow_nan=False makes a non-finite number an error instead of invalid JSON.
    return json.dumps(report_to_data(report), indent=2, allow_nan=False) + "\n"


def analyze_song(
    loaded: LoadedSong,
    song_name: str,
    renders: RenderStore,
    song_id: SongId,
    instruments: Mapping[Identifier, Instrument],
    audio_files: AudioFiles,
    record_writer: RecordWriter,
    tools: AnalysisTools,
    *,
    effects: Mapping[Identifier, Effect] = EMPTY_EFFECTS,
) -> Report:
    """Mix and measure a song's stems, and keep the analysis as the next record.

    The song's effect references are checked against the configured recipes first. `song_name`
    is the song file's name without its suffix. Raises SongError for a track or node naming an
    unknown effect recipe, SongBusyError when another run holds the song's lock, and ValueError
    when the store's stem suffix differs from the audio files' or the mixer returns the wrong
    length. An OSError from the store, the audio files, the mixer, or the visualizer propagates
    after the lock is released.
    """
    require_stem_format(renders, audio_files)
    with song_session(renders, song_id) as store:
        return _analyze(
            loaded, song_name, store, instruments, effects, audio_files, record_writer, tools
        )


def _analyze(
    loaded: LoadedSong,
    song_name: str,
    store: SongStore,
    instruments: Mapping[Identifier, Instrument],
    effects: Mapping[Identifier, Effect],
    audio_files: AudioFiles,
    record_writer: RecordWriter,
    tools: AnalysisTools,
) -> Report:
    loaded.check_effects([*effects])
    graph = loaded.graph
    frames = stem_samples(graph)
    resolutions = resolve_all(graph, instruments, effects)
    planned = plan(graph, resolutions)
    keys: dict[NodeName, str | None] = {}
    sound_keys: dict[NodeName, str | None] = {}
    stems: dict[NodeName, Audio] = {}
    failures: dict[NodeName, NodeReport] = {}
    for node in graph.nodes:
        name = NodeName(node.name)
        unit, _ = planned.place(name)
        resolved = resolutions[node.name]
        sound = (
            resolved.sound_key
            if isinstance(resolved, ResolvedInstrument | ResolvedEffectNode)
            else None
        )
        key = None if isinstance(unit, Unplanned) else unit.key
        keys[name], sound_keys[name] = key, sound
        detail: str | None = None
        if not store.stem_path(name).is_file():
            status = AnalysisStatus.MISSING_STEM
        elif isinstance(unit, Unplanned):
            status, detail = AnalysisStatus.STALE_STEM, unit.detail
        else:
            try:
                stems[name] = read_stem(store, name, unit.key, sound, audio_files, frames)
                continue
            except StaleStemError as error:
                status, detail = AnalysisStatus.STALE_STEM, str(error)
        failures[name] = NodeReport(
            node.name,
            status,
            key,
            sound,
            node.kind,
            tuple(wire.from_name for wire in graph.incoming(node.name)),
            gain_db=node.gain_db,
            detail=detail,
        )

    # Results from older audio must not survive a run that cannot replace them.
    store.clear_latest()
    song_text = record_writer.dump(loaded.source)
    record = store.start_record(song_text, record_writer.suffix)
    song_sha256 = hashlib.sha256(song_text.encode("utf-8")).hexdigest()
    complete = not failures
    suffix = tools.visualizer.suffix

    nodes: list[NodeReport] = []
    for node in graph.nodes:
        name = NodeName(node.name)
        if name in failures:
            nodes.append(failures[name])
            continue
        audio = stems[name]
        measurements = measure(audio, graph, tools.measurer)
        image = None
        if complete:
            image = store.image_path(name, suffix)
            tools.visualizer.draw(audio, image, node.name)
        nodes.append(
            NodeReport(
                node.name,
                AnalysisStatus.ANALYZED,
                keys[name],
                sound_keys[name],
                node.kind,
                tuple(wire.from_name for wire in graph.incoming(node.name)),
                gain_db=node.gain_db,
                peak_dbfs=measurements.peak_dbfs,
                integrated_lufs=measurements.integrated_lufs,
                clipped_samples=measurements.clipped_samples,
                bands_db=measurements.bands_db,
                bar_dbfs=measurements.bar_dbfs,
                spectrogram=None if image is None else str(image),
                defects=tuple(silent_defects(measurements)),
            )
        )

    mix = None
    if complete:
        inputs = tuple(
            MixInput(NodeName(node.name), stems[NodeName(node.name)], node.gain_db)
            for node in _leaves(graph)
        )
        mixed = tools.mixer.mix(inputs, frames)
        if mixed.frames != frames:
            raise ValueError(f"the mixer returned {mixed.frames} frames, expected {frames}")
        mix_path = store.mix_path()
        audio_files.write(mix_path, mixed)
        mix_image = store.image_path(None, suffix)
        tools.visualizer.draw(mixed, mix_image, MIX_NAME)
        measurements = measure(mixed, graph, tools.measurer)
        mix = MixReport(
            path=str(mix_path),
            peak_dbfs=measurements.peak_dbfs,
            integrated_lufs=measurements.integrated_lufs,
            clipped_samples=measurements.clipped_samples,
            bands_db=measurements.bands_db,
            bar_dbfs=measurements.bar_dbfs,
            spectrogram=str(mix_image),
            defects=tuple(mix_defects(measurements)),
        )

    report = Report(
        song=song_name, run=record.run, song_sha256=song_sha256, nodes=tuple(nodes), mix=mix
    )
    if complete:
        store.report_path().write_text(report_json(report), encoding="utf-8")

    # The record's report points at the record's own copies of the images.
    kept = tuple(
        report_node
        if report_node.spectrogram is None
        else replace(
            report_node,
            spectrogram=str(store.keep_image(record, NodeName(node.name), suffix)),
        )
        for node, report_node in zip(graph.nodes, report.nodes, strict=True)
    )
    kept_mix = (
        None
        if report.mix is None
        else replace(report.mix, spectrogram=str(store.keep_image(record, None, suffix)))
    )
    store.finish_record(record, report_json(replace(report, nodes=kept, mix=kept_mix)))
    return report


def _leaves(graph: Graph) -> tuple[GraphNode, ...]:
    """Return the nodes no wire feeds from: the ones whose stems the mix sums, each at its own
    `gain_db`. Without wires, every node is a leaf, so the mix is the song's stems as before."""
    fed = {wire.from_name for wire in graph.wires}
    return tuple(node for node in graph.nodes if node.name not in fed)
