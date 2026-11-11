"""Analysis: mixing a song's current stems, measuring them, and keeping the report as a record.

Stems come from the song's node names, never from a listing, so a stem left by a removed or
renamed node cannot enter the mix. Every analysis that runs to the end keeps a record, even a
failed one; the latest results are written only when every node analyzed.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

import numpy as np

from poser.core.audio import Audio, measure_peak_dbfs, stem_samples
from poser.core.cache import StampProblem
from poser.core.graph import Graph, Node
from poser.core.names import Identifier, NodeName, SongId
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
    ReportSection,
    SectionInput,
    SectionResult,
    SongStore,
    SoundFailure,
    Visualizer,
)
from poser.core.rendering import (
    EMPTY_EFFECTS,
    ResolvedDelay,
    ResolvedNode,
    cycle_groups,
    node_cache_key,
    require_stem_format,
    resolve_node,
)
from poser.core.report import (
    BAR_BEATS,
    MIX_NAME,
    AnalysisStatus,
    Measurements,
    MixReport,
    NodeReport,
    Report,
    SectionData,
    SectionError,
    SectionReport,
    bar_dbfs,
    mix_defects,
    report_to_data,
    silent_defects,
)
from poser.core.session import song_session
from poser.core.song import Song, song_graph, song_last_beat


def measure(audio: Audio, song: Song | Graph, measurer: Measurer) -> Measurements:
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
    node: NodeName,
    keys: tuple[str, str],
    audio_files: AudioFiles,
    frames: int,
) -> Audio:
    """Read a node's stem and confirm it matches the song. Raises StaleStemError when it does not.

    `keys` are the node's current `node_key` and `sound_key`. Its stamp must still describe the
    stem file and record both.
    """
    stamp = store.read_stamp(node)
    if isinstance(stamp, StampProblem):
        raise StaleStemError(stamp.value)
    # The sound is checked first, because the node key digests it and would mask which input
    # changed.
    if stamp.sound_key != keys[1]:
        raise StaleStemError("sound changed since this stem was rendered")
    if stamp.track_key != keys[0]:
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
    song: Song | Graph,
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

    The song is a Song, which compiles to its graph, or a Graph, whose effect references resolve
    against the configured recipes first. `song_name` is the song file's name without its
    suffix. Raises SongError for a node naming an unknown effect recipe, SongBusyError when
    another run holds the song's lock, and ValueError when the store's stem suffix differs from
    the audio files' or the mixer returns the wrong length. An OSError from the store, the audio
    files, the mixer, or the visualizer propagates after the lock is released.
    """
    require_stem_format(renders, audio_files)
    with song_session(renders, song_id) as store:
        return _analyze(
            song, song_name, store, instruments, effects, audio_files, record_writer, tools
        )


def _analyze(
    song: Song | Graph,
    song_name: str,
    store: SongStore,
    instruments: Mapping[Identifier, Instrument],
    effects: Mapping[Identifier, Effect],
    audio_files: AudioFiles,
    record_writer: RecordWriter,
    tools: AnalysisTools,
) -> Report:
    graph = song_graph(song, [*effects])
    frames = stem_samples(song)
    keys: dict[NodeName, str] = {}
    sound_keys: dict[NodeName, str] = {}
    stems: dict[NodeName, Audio] = {}
    failures: dict[NodeName, NodeReport] = {}
    groups = cycle_groups(graph)
    resolved_of: dict[str, ResolvedDelay | ResolvedNode | SoundFailure] = {}
    identity_of: dict[str, str | None] = {}
    for node in graph.nodes:
        resolved = resolve_node(node, instruments, effects)
        resolved_of[node.name] = resolved
        identity_of[node.name] = (
            None
            if isinstance(resolved, SoundFailure)
            else (resolved.identity if isinstance(resolved, ResolvedDelay) else resolved.sound_key)
        )
    # Keys are computed in the graph's cycle-group order, because a node's key digests its
    # upstream keys and a cycle group's digest reads the identities of every member. Report rows
    # stay positionally aligned with the graph's nodes, which compilation sets to song order.
    for node in [member for group in graph.groups() for member in group]:
        name = NodeName(node.name)
        resolved = resolved_of[node.name]
        identity = identity_of[node.name]
        keys[name] = node_cache_key(graph, node, identity_of, keys, groups.get(node.name))
        if not store.stem_path(name).is_file():
            failures[name] = NodeReport(
                node.name,
                AnalysisStatus.MISSING_STEM,
                keys[name],
                identity,
                node.kind,
                _upstream(graph, node),
                gain_db=node.gain_db,
            )
            continue
        if isinstance(resolved, SoundFailure):
            failures[name] = NodeReport(
                node.name,
                AnalysisStatus.STALE_STEM,
                keys[name],
                None,
                node.kind,
                _upstream(graph, node),
                gain_db=node.gain_db,
                detail=resolved.detail,
            )
            continue
        sound_keys[name] = cast("str", identity)
        try:
            stems[name] = read_stem(
                store, name, (keys[name], cast("str", identity)), audio_files, frames
            )
        except StaleStemError as error:
            failures[name] = NodeReport(
                node.name,
                AnalysisStatus.STALE_STEM,
                keys[name],
                identity,
                node.kind,
                _upstream(graph, node),
                gain_db=node.gain_db,
                detail=str(error),
            )

    # Results from older audio must not survive a run that cannot replace them.
    store.clear_latest()
    song_text = record_writer.dump(song)
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
        measurements = measure(audio, song, tools.measurer)
        sections = _sections(tools.sections, SectionInput(song, node, audio, measurements))
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
                _upstream(graph, node),
                gain_db=node.gain_db,
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


def _upstream(graph: Graph, node: Node) -> tuple[str, ...]:
    """Return the names of the nodes whose wires feed this node, in wire order."""
    return tuple(wire.from_name for wire in graph.wires if wire.to_name == node.name)


def _upstream_keys(
    graph: Graph, node: Node, keys: Mapping[NodeName, str]
) -> tuple[tuple[str, str, float], ...]:
    """Return each feeding wire's upstream key, wire kind, and wire gain, in wire order.

    Keys are computed in the graph's topological order, so every upstream's key exists when this
    node's is computed.
    """
    return tuple(
        (keys[NodeName(wire.from_name)], wire.kind, wire.gain_db)
        for wire in graph.wires
        if wire.to_name == node.name
    )


def _leaves(graph: Graph) -> tuple[Node, ...]:
    """Return the nodes no wire feeds from: the ones whose stems the mix sums, each at its own
    `gain_db`. Without wires, every node is a leaf, so the mix is the song's stems as before."""
    fed = {wire.from_name for wire in graph.wires}
    return tuple(node for node in graph.nodes if node.name not in fed)


def _sections(
    sections: Sequence[ReportSection], request: SectionInput
) -> Mapping[str, SectionReport]:
    """Run every section in order on one track or the mix, recording each failure as its error."""
    return {section.name: _section(section, request) for section in sections}


def _section(section: ReportSection, request: SectionInput) -> SectionReport:
    version = section.version
    try:
        result = section.mix(request) if request.node is None else section.track(request)
        if not isinstance(result, SectionResult):
            raise TypeError(f"returned {type(result).__name__}, not a SectionResult")  # noqa: TRY301
    except Exception as error:  # noqa: BLE001 -- a section's failure is reported, not raised.
        return SectionError(version, f"{type(error).__name__}: {error}")
    return SectionData(version, result.data, result.defects)
