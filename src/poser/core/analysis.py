"""Analysis: mixing a song's current stems, measuring them, and keeping the report as a record.

Each node's stem is found by its render unit's key in the current plan, never from a listing, so a
stem rendered from other inputs cannot enter the mix. Every analysis that runs to the end keeps a
record, even a failed one, and the record becomes the latest only when every node analyzed. Paths
in the report are relative to the record.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from poser.core.audio import Audio, measure_peak_dbfs, stem_samples
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
    StoredUnit,
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

# The report's name in a record.
REPORT_NAME = "report.json"


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
    """A stored stem's audio cannot be read."""


def read_stem(stored: StoredUnit, index: int, audio_files: AudioFiles, frames: int) -> Audio:
    """Read the stem at `index` of a stored unit. Raises StaleStemError when it cannot be read."""
    try:
        return audio_files.read(stored.stem_path(index), frames)
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
    store.sweep()
    keys: dict[NodeName, str | None] = {}
    sound_keys: dict[NodeName, str | None] = {}
    stems: dict[NodeName, Audio] = {}
    failures: dict[NodeName, NodeReport] = {}
    for node in graph.nodes:
        name = NodeName(node.name)
        unit, index = planned.place(name)
        resolved = resolutions[node.name]
        sound = (
            resolved.sound_key
            if isinstance(resolved, ResolvedInstrument | ResolvedEffectNode)
            else None
        )
        key = None if isinstance(unit, Unplanned) else unit.key
        keys[name], sound_keys[name] = key, sound
        detail: str | None = None
        stored = None if isinstance(unit, Unplanned) else store.find(unit.key)
        if isinstance(unit, Unplanned):
            status, detail = AnalysisStatus.STALE_STEM, unit.detail
        elif stored is None:
            status = AnalysisStatus.MISSING_STEM
        else:
            try:
                stems[name] = read_stem(stored, index, audio_files, frames)
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

    song_text = record_writer.dump(loaded.source)
    song_sha256 = hashlib.sha256(song_text.encode("utf-8")).hexdigest()
    complete = not failures
    suffix = tools.visualizer.suffix
    with store.write_record(song_text, record_writer.suffix, latest=complete) as record:
        nodes: list[NodeReport] = []
        for position, node in enumerate(graph.nodes):
            name = NodeName(node.name)
            if name in failures:
                nodes.append(failures[name])
                continue
            audio = stems[name]
            measurements = measure(audio, graph, tools.measurer)
            image = None
            if complete:
                image = record.path(f"images/node-{position}{suffix}")
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
                    spectrogram=None if image is None else record.relative(image),
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
            mix_image = record.path(f"images/{MIX_NAME}{suffix}")
            tools.visualizer.draw(mixed, mix_image, MIX_NAME)
            mix_path = record.path(f"audio/{MIX_NAME}{audio_files.suffix}")
            audio_files.write(mix_path, mixed)
            measurements = measure(mixed, graph, tools.measurer)
            mix = MixReport(
                path=record.relative(mix_path),
                peak_dbfs=measurements.peak_dbfs,
                integrated_lufs=measurements.integrated_lufs,
                clipped_samples=measurements.clipped_samples,
                bands_db=measurements.bands_db,
                bar_dbfs=measurements.bar_dbfs,
                spectrogram=record.relative(mix_image),
                defects=tuple(mix_defects(measurements)),
            )

        report = Report(
            song=song_name, run=record.run, song_sha256=song_sha256, nodes=tuple(nodes), mix=mix
        )
        record.path(REPORT_NAME).write_text(report_json(report), encoding="utf-8")
    return report


def _leaves(graph: Graph) -> tuple[GraphNode, ...]:
    """Return the nodes no wire feeds from: the ones whose stems the mix sums, each at its own
    `gain_db`. Without wires, every node is a leaf, so the mix is the song's stems as before."""
    fed = {wire.from_name for wire in graph.wires}
    return tuple(node for node in graph.nodes if node.name not in fed)
