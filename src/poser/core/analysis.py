"""Analysis: mixing a song's current stems, measuring them, and keeping the report as a record.

Each node's stem is found by its render unit's key in the current plan, never from a listing, so a
stem rendered from other inputs cannot enter the mix. Every analysis that runs to the end keeps a
record, even a failed one, and the record becomes the latest only when every node analyzed. Paths
in the report are relative to the record.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
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
    RecordDraft,
    RecordWriter,
    RenderStore,
    SongStore,
    Visualizer,
)
from poser.core.rendering import require_stem_format
from poser.core.report import (
    MIX_NAME,
    AnalyzedNode,
    CompleteReport,
    IncompleteReport,
    LevelSpan,
    MeasuredNode,
    Measurements,
    MixReport,
    Report,
    UnplannedNode,
    UnrenderedNode,
    level_spans,
    mix_defects,
    report_to_data,
    silent_defects,
    span_dbfs,
)
from poser.core.session import song_session
from poser.core.statuses import UnrenderedStatus

# The report's name in a record.
REPORT_NAME = "report.json"
NOT_RENDERED_DETAIL = "render-song has not rendered this node's current inputs"


def measure(
    audio: Audio, graph: Graph, measurer: Measurer, spans: Sequence[LevelSpan]
) -> Measurements:
    """Measure audio: its peak, clipping, and the level of each span here, and its loudness and
    bands through the measurer. `spans` are the graph's `level_spans`."""
    samples = audio.samples
    levels = measurer.measure(audio)
    bounds = [(graph.seconds(span.beat), graph.seconds(span.end)) for span in spans]
    return Measurements(
        peak_dbfs=measure_peak_dbfs(samples),
        integrated_lufs=levels.integrated_lufs,
        clipped_samples=int(np.count_nonzero(np.abs(samples) > 1.0)),
        bands_db=levels.bands_db,
        span_dbfs=tuple(span_dbfs(samples, bounds)),
    )


@dataclass(frozen=True)
class AnalysisTools:
    """The analysis adapters, carried as one argument."""

    mixer: Mixer
    measurer: Measurer
    visualizer: Visualizer


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
    spans = level_spans(graph)
    resolutions = resolve_all(graph, instruments, effects)
    planned = plan(graph, resolutions)
    store.sweep()
    keys: dict[NodeName, str] = {}
    sound_keys: dict[NodeName, str | None] = {}
    stems: dict[NodeName, Audio] = {}
    failures: dict[NodeName, UnrenderedNode | UnplannedNode] = {}
    for node in graph.nodes:
        name = NodeName(node.name)
        upstream = tuple(wire.from_name for wire in graph.incoming(node.name))
        resolved = resolutions[node.name]
        sound = (
            resolved.sound_key
            if isinstance(resolved, ResolvedInstrument | ResolvedEffectNode)
            else None
        )
        sound_keys[name] = sound
        unit, index = planned.place(name)
        if isinstance(unit, Unplanned):
            status, detail = unit.failure_of(name)
            failures[name] = UnplannedNode(
                node.name, node.kind, upstream, node.gain_db, sound, status, detail
            )
            continue
        keys[name] = unit.key
        stored = store.find(unit.key)
        if stored is None:
            status, detail = UnrenderedStatus.NOT_RENDERED, NOT_RENDERED_DETAIL
        else:
            try:
                stems[name] = audio_files.read(stored.stem_path(index), frames)
                continue
            except AudioFileError as error:
                status, detail = UnrenderedStatus.UNREADABLE_STEM, str(error)
        failures[name] = UnrenderedNode(
            node.name, node.kind, upstream, node.gain_db, unit.key, sound, status, detail
        )

    song_text = record_writer.dump(loaded.source)
    song_sha256 = hashlib.sha256(song_text.encode("utf-8")).hexdigest()
    complete = not failures
    suffix = tools.visualizer.suffix
    report: Report
    with store.write_record(song_text, record_writer.suffix, latest=complete) as record:
        analyzed: list[AnalyzedNode] = []
        incomplete: list[MeasuredNode | UnrenderedNode | UnplannedNode] = []
        for position, node in enumerate(graph.nodes):
            name = NodeName(node.name)
            if name in failures:
                incomplete.append(failures[name])
                continue
            audio = stems[name]
            measurements = measure(audio, graph, tools.measurer, spans)
            upstream = tuple(wire.from_name for wire in graph.incoming(node.name))
            defects = tuple(silent_defects(measurements))
            if complete:
                image = record.path(f"images/node-{position}{suffix}")
                tools.visualizer.draw(audio, image, node.name)
                analyzed.append(
                    AnalyzedNode(
                        node.name,
                        node.kind,
                        upstream,
                        node.gain_db,
                        keys[name],
                        sound_keys[name],
                        measurements,
                        defects,
                        record.relative(image),
                    )
                )
            else:
                incomplete.append(
                    MeasuredNode(
                        node.name,
                        node.kind,
                        upstream,
                        node.gain_db,
                        keys[name],
                        sound_keys[name],
                        measurements,
                        defects,
                    )
                )

        if complete:
            mix = _mix(graph, stems, frames, spans, audio_files, tools, record)
            report = CompleteReport(song_name, record.run, song_sha256, spans, tuple(analyzed), mix)
        else:
            report = IncompleteReport(song_name, record.run, song_sha256, spans, tuple(incomplete))
        record.path(REPORT_NAME).write_text(report_json(report), encoding="utf-8")
    return report


def _mix(
    graph: Graph,
    stems: Mapping[NodeName, Audio],
    frames: int,
    spans: tuple[LevelSpan, ...],
    audio_files: AudioFiles,
    tools: AnalysisTools,
    record: RecordDraft,
) -> MixReport:
    """Mix the leaf stems into the record, draw the mix, and measure it."""
    inputs = tuple(
        MixInput(NodeName(node.name), stems[NodeName(node.name)], node.gain_db)
        for node in _leaves(graph)
    )
    mixed = tools.mixer.mix(inputs, frames)
    if mixed.frames != frames:
        raise ValueError(f"the mixer returned {mixed.frames} frames, expected {frames}")
    mix_image = record.path(f"images/{MIX_NAME}{tools.visualizer.suffix}")
    tools.visualizer.draw(mixed, mix_image, MIX_NAME)
    mix_path = record.path(f"audio/{MIX_NAME}{audio_files.suffix}")
    audio_files.write(mix_path, mixed)
    measurements = measure(mixed, graph, tools.measurer, spans)
    return MixReport(
        record.relative(mix_path),
        measurements,
        record.relative(mix_image),
        tuple(mix_defects(measurements)),
    )


def _leaves(graph: Graph) -> tuple[GraphNode, ...]:
    """Return the nodes no wire feeds from: the ones whose stems the mix sums, each at its own
    `gain_db`. Without wires, every node is a leaf, so the mix is the song's stems as before."""
    fed = {wire.from_name for wire in graph.wires}
    return tuple(node for node in graph.nodes if node.name not in fed)
