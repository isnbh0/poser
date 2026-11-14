"""Analyzing a song's stems, on fakes."""

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import override

import numpy as np
import pytest
from fakes import (
    FakeEffect,
    FakeInstrument,
    FakeMeasurer,
    FakeMixer,
    FakeRecordWriter,
    FakeRenderStore,
    FakeSongStore,
    FakeSound,
    FakeVisualizer,
    NpyAudioFiles,
)

from poser.core.analysis import AnalysisTools, analyze_song, measure
from poser.core.audio import Audio, measure_peak_dbfs, stem_samples, write_raw_audio
from poser.core.graph import DelayNode, EffectNode, Graph, TrackNode, Wire
from poser.core.loading import LoadedSong
from poser.core.names import Identifier, SongId, TrackName
from poser.core.planning import Unplanned, plan, resolve_all
from poser.core.ports import (
    Effect,
    Instrument,
    Mixer,
    MixInput,
    SoundFailure,
)
from poser.core.rendering import Rendered, render_song
from poser.core.report import (
    BANDS,
    AnalyzedNode,
    CompleteReport,
    Defect,
    IncompleteReport,
    Levels,
    MeasuredNode,
    Report,
    UnplannedNode,
    UnrenderedNode,
    bar_dbfs,
    report_failed,
    report_to_data,
)
from poser.core.song import Note, Song, Track
from poser.core.statuses import (
    SkipStatus,
    SoundFailureStatus,
    StemStatus,
    UnrenderedStatus,
)

SONG_ID = SongId("songs/demo.yaml")
LEAD = TrackName("lead")
BASS = TrackName("bass")
BAR = (Note(beat=0, pitch=60, length=4, velocity=100),)


def make_song(
    lead: str = "fake", bass: str = "fake:pad", *, gain: float = 0.0, pitch: int = 60
) -> Song:
    bass_note = (Note(beat=0, pitch=pitch, length=4, velocity=100),)
    return Song(
        bpm=120,
        tracks=(Track("lead", lead, BAR, gain=gain), Track("bass", bass, bass_note)),
    )


def make_instrument(
    pad_identity: str = "pad", *, lead: float = 0.5, pad: float = 0.2
) -> FakeInstrument:
    """The instrument of make_song: the lead plays the default sound, the bass the pad."""
    return FakeInstrument(
        "fake", {None: FakeSound(amplitude=lead), "pad": FakeSound("a pad", pad_identity, pad)}
    )


class Setup:
    """Fakes for every port the analysis uses, with the song's stems rendered."""

    def __init__(
        self,
        tmp_path: Path,
        mixer: Mixer | None = None,
        visualizer: FakeVisualizer | None = None,
        song: Song | Graph | None = None,
        instrument: FakeInstrument | None = None,
        effects: Sequence[FakeEffect] = (),
    ) -> None:
        self.instrument = instrument or make_instrument()
        self.effects: Mapping[Identifier, Effect] = {each.name: each for each in effects}
        self.renders = FakeRenderStore(tmp_path / "renders")
        self.store: FakeSongStore = self.renders.song(SONG_ID)
        self.audio_files = NpyAudioFiles()
        self.visualizer = visualizer or FakeVisualizer()
        self.tools = AnalysisTools(mixer or FakeMixer(), FakeMeasurer(), self.visualizer)
        results = render_song(
            LoadedSong(song or make_song()),
            self.renders,
            SONG_ID,
            self.instruments,
            self.audio_files,
            effects=self.effects,
            timeout_s=10.0,
            force=False,
        )
        assert all(isinstance(result, Rendered) for result in results)

    @property
    def instruments(self) -> dict[Identifier, Instrument]:
        return {self.instrument.name: self.instrument}

    def analyze(self, song: Song | Graph | None = None) -> Report:
        return analyze_song(
            LoadedSong(song or make_song()),
            "demo",
            self.renders,
            SONG_ID,
            self.instruments,
            self.audio_files,
            FakeRecordWriter(),
            self.tools,
            effects=self.effects,
        )

    def analyze_complete(self, song: Song | Graph | None = None) -> CompleteReport:
        report = self.analyze(song)
        assert isinstance(report, CompleteReport), report
        return report

    def record(self, run: int) -> Path:
        return self.store.record_path(run)

    def mix_path(self) -> Path:
        """The latest record's mix."""
        latest = self.store.latest()
        assert latest is not None, "no latest record"
        return latest / f"audio/mix{self.audio_files.suffix}"

    def stored_stem(self, song: Song, index: int) -> Path:
        """The stored stem of the song's index'th node."""
        key = node_key_of(song, index, self)
        assert key is not None
        stored = self.store.find(key)
        assert stored is not None
        return stored.stem_path(0)

    def forget(self, song: Song, index: int) -> None:
        """Drop the object holding the song's index'th node, as if it was never rendered."""
        key = node_key_of(song, index, self)
        assert key is not None
        del self.store.state.units[key]


def tree(directory: Path) -> list[str]:
    return sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*"))


def node_key_of(song: Song, index: int, setup: Setup) -> str | None:
    """The key the report records for the song's index'th node: its unit's key in the render
    plan, even when the stem itself is missing, or None when the unit cannot be planned."""
    graph = song.graph()
    unit, _ = plan(graph, resolve_all(graph, setup.instruments, setup.effects)).place(
        graph.nodes[index].name
    )
    return None if isinstance(unit, Unplanned) else unit.key


# Measuring


def test_measure_takes_peak_clipping_and_bars_from_the_audio_and_levels_from_the_measurer() -> None:
    song = Song(bpm=120, tracks=(Track("lead", "fake", (Note(0, 60, 8, 100),)),))
    samples = np.zeros((2, stem_samples(song.graph())), dtype=np.float32)
    samples[:, :96000] = 0.5
    samples[:, 96000:192000] = 0.25
    samples[0, 10] = 1.5
    samples[1, 20] = -1.25
    levels = Levels(-9.0, dict.fromkeys(BANDS, -3.0))

    measurements = measure(Audio(samples), song.graph(), FakeMeasurer(levels))

    assert measurements.peak_dbfs == measure_peak_dbfs(samples)
    assert measurements.clipped_samples == 2
    assert measurements.bar_dbfs == tuple(bar_dbfs(samples, 2.0, 2))
    assert measurements.integrated_lufs == -9.0
    assert measurements.bands_db == levels.bands_db


def test_analysis_refuses_a_store_whose_stems_the_audio_files_cannot_read(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path)
    wav = FakeRenderStore(tmp_path / "renders", ".wav")

    with setup.store.lock(), pytest.raises(ValueError, match=r"stem suffix \.wav differs"):
        analyze_song(
            LoadedSong(make_song()),
            "demo",
            wav,
            SONG_ID,
            setup.instruments,
            setup.audio_files,
            FakeRecordWriter(),
            setup.tools,
        )


def test_full_analysis_writes_latest_results_and_a_record(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    report = setup.analyze_complete()

    assert report.run == 1
    assert report.song == "demo"
    assert len(report.nodes) == 2
    assert all(isinstance(node, AnalyzedNode) for node in report.nodes)
    assert not report_failed(report)
    record = setup.record(1)
    assert setup.store.latest() == record
    assert report.mix.path == "audio/mix.npy"
    images = [node.spectrogram for node in report.nodes] + [report.mix.spectrogram]
    assert images == ["images/node-0.png", "images/node-1.png", "images/mix.png"]
    for relative in (report.mix.path, *images):
        assert not Path(relative).is_absolute()
        assert (record / relative).is_file()
    assert tree(record) == [
        "audio",
        "audio/mix.npy",
        "images",
        "images/mix.png",
        "images/node-0.png",
        "images/node-1.png",
        "report.json",
        "song.json",
    ]
    kept = json.loads((record / "report.json").read_text())
    assert kept == report_to_data(report)


def test_mix_is_the_gain_weighted_sum_of_the_stems(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    report = setup.analyze_complete(make_song(gain=-6.0))

    mix = setup.audio_files.read(setup.mix_path(), stem_samples(make_song().graph()))
    assert np.allclose(mix.samples, 0.5 * 10 ** (-6 / 20) + 0.2)
    lead, bass = report.nodes
    assert lead.gain_db == -6.0
    assert lead.measurements.peak_dbfs == pytest.approx(20 * np.log10(0.5))
    assert bass.gain_db == 0.0


def test_stray_stem_does_not_enter_the_mix(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    with setup.store.write_unit("f" * 64) as draft:
        frames = stem_samples(make_song().graph())
        write_raw_audio(draft.stem_path(0), np.full((2, frames), 0.25, dtype=np.float32))
        draft.describe(0, StemStatus.RENDERED, -12.0)

    setup.analyze()

    mix = setup.audio_files.read(setup.mix_path(), stem_samples(make_song().graph()))
    assert np.allclose(mix.samples, 0.7)


def test_report_keys_are_the_keys_of_the_stored_objects(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    lead, bass = setup.analyze_complete().nodes

    for index, report_node in enumerate((lead, bass)):
        assert report_node.node_key == node_key_of(make_song(), index, setup)
        assert setup.store.find(report_node.node_key) is not None
        assert report_node.sound_key is not None


def spoil(setup: Setup, case: str) -> Song:
    """Spoil the bass stem one way and return the song to analyze."""
    stem = setup.stored_stem(make_song(), 1)
    frames = stem_samples(make_song().graph())
    match case:
        case "missing" | "missing_and_unresolved":
            setup.forget(make_song(), 1)
        case "sound_changed" | "song_and_sound_changed":
            setup.instrument = make_instrument(pad_identity="pad, edited")
        case "wrong_length":
            write_raw_audio(stem, np.full((2, frames - 1), 0.2, dtype=np.float32))
        case "not_finite":
            samples = np.full((2, frames), 0.2, dtype=np.float32)
            samples[:, 1000] = np.nan
            write_raw_audio(stem, samples)
    if case in {"song_changed", "song_and_sound_changed"}:
        return make_song(pitch=62)
    if case == "unknown_instrument":
        return make_song(bass="other")
    if case in {"unresolved", "missing_and_unresolved"}:
        return make_song(bass="fake:gone")
    return make_song()


FRAMES = stem_samples(make_song().graph())


NOT_RENDERED = "render-song has not rendered this node's current inputs"


@pytest.mark.parametrize(
    ("case", "status", "detail"),
    [
        ("missing", UnrenderedStatus.NOT_RENDERED, NOT_RENDERED),
        # A changed input changes the key, so no object holds the node's stem.
        ("song_changed", UnrenderedStatus.NOT_RENDERED, NOT_RENDERED),
        ("song_and_sound_changed", UnrenderedStatus.NOT_RENDERED, NOT_RENDERED),
        ("sound_changed", UnrenderedStatus.NOT_RENDERED, NOT_RENDERED),
        (
            "wrong_length",
            UnrenderedStatus.UNREADABLE_STEM,
            f"{FRAMES - 1} samples, expected {FRAMES}",
        ),
        ("not_finite", UnrenderedStatus.UNREADABLE_STEM, "audio contains NaN or infinity"),
    ],
)
def test_unusable_stem_fails_the_run_and_removes_earlier_results(
    tmp_path: Path, case: str, status: UnrenderedStatus, detail: str
) -> None:
    setup = Setup(tmp_path)
    setup.analyze_complete()
    song = spoil(setup, case)

    report = setup.analyze(song)

    assert isinstance(report, IncompleteReport)
    lead, bass = report.nodes
    assert isinstance(lead, MeasuredNode)
    assert lead.measurements.peak_dbfs == pytest.approx(20 * np.log10(0.5))
    assert isinstance(bass, UnrenderedNode)
    assert (bass.status, bass.detail) == (status, detail)
    assert bass.node_key == node_key_of(song, 1, setup)
    assert bass.sound_key is not None
    assert (bass.gain_db, bass.kind, bass.upstream) == (0.0, "track", ())
    assert report_failed(report)
    assert setup.store.latest() is None
    assert report.run == 2
    assert tree(setup.record(2)) == ["report.json", "song.json"]
    kept = json.loads((setup.record(2) / "report.json").read_text())
    assert kept["status"] == "incomplete"
    assert "mix" not in kept
    assert "spectrogram" not in kept["nodes"][0]


@pytest.mark.parametrize(
    ("case", "status", "detail"),
    [
        # A node that cannot be planned has no key to look up, so the plan's reason comes first.
        ("missing_and_unresolved", SoundFailureStatus.UNKNOWN_SOUND, "no sound named 'gone'"),
        ("unresolved", SoundFailureStatus.UNKNOWN_SOUND, "no sound named 'gone'"),
        (
            "unknown_instrument",
            SoundFailureStatus.UNKNOWN_SOUND,
            "no instrument 'other' in poser.toml",
        ),
    ],
)
def test_an_unplannable_node_reports_render_songs_status(
    tmp_path: Path, case: str, status: SoundFailureStatus, detail: str
) -> None:
    setup = Setup(tmp_path)
    song = spoil(setup, case)

    report = setup.analyze(song)

    assert isinstance(report, IncompleteReport)
    _, bass = report.nodes
    assert bass == UnplannedNode("bass", "track", (), 0.0, None, status, detail)
    assert setup.store.latest() is None


def test_a_failed_sound_reports_load_failed_and_its_downstream_upstream_failed(
    tmp_path: Path,
) -> None:
    setup = graph_setup(tmp_path)
    failure = SoundFailure(SoundFailureStatus.LOAD_FAILED, "the preset is corrupt")
    setup.instrument = FakeInstrument(
        "fake",
        {
            None: FakeSound(amplitude=0.5),
            "pad": FakeSound("a pad", "pad", 0.2),
            "broken": FakeSound(sound_failure=failure),
        },
    )

    report = setup.analyze(graph_song(lead="fake:broken"))

    assert isinstance(report, IncompleteReport)
    lead, driven, bass = report.nodes
    assert isinstance(lead, UnplannedNode)
    assert (lead.status, lead.detail) == (SoundFailureStatus.LOAD_FAILED, "the preset is corrupt")
    assert isinstance(driven, UnplannedNode)
    assert (driven.status, driven.detail) == (
        SkipStatus.UPSTREAM_FAILED,
        "upstream 'lead' did not render",
    )
    assert isinstance(bass, MeasuredNode)


def test_a_fractional_pitch_on_a_whole_pitch_instrument_reports_unsupported_pitch(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path)
    song = make_song()
    fractional = (Note(beat=0, pitch=60.5, length=4, velocity=100),)
    song = dataclasses.replace(
        song, tracks=(dataclasses.replace(song.tracks[0], notes=fractional), song.tracks[1])
    )

    report = setup.analyze(song)

    assert isinstance(report, IncompleteReport)
    lead, _ = report.nodes
    assert isinstance(lead, UnplannedNode)
    assert lead.status is SkipStatus.UNSUPPORTED_PITCH


def test_records_number_on_and_hold_the_song_and_its_digest(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    runs = [setup.analyze().run, setup.analyze(make_song(gain=-3.0)).run]
    setup.forget(make_song(), 1)
    failed = setup.analyze()

    assert [*runs, failed.run] == [1, 2, 3]
    for run in (1, 2, 3):
        song_file = setup.record(run) / "song.json"
        assert song_file.is_file()
    assert (
        failed.song_sha256
        == hashlib.sha256((setup.record(3) / "song.json").read_bytes()).hexdigest()
    )
    assert json.loads((setup.record(2) / "song.json").read_text())["tracks"][0]["gain"] == -3


def test_report_nodes_stay_aligned_with_the_graph(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    report = setup.analyze_complete()

    for report_node, node in zip(report.nodes, make_song().graph().nodes, strict=True):
        assert report_node.name == node.name
        assert report_node.kind == node.kind


# Defects


def test_silent_stem_reports_null_loudness_and_a_silent_defect(tmp_path: Path) -> None:
    song = Song(bpm=120, tracks=(Track("quiet", "fake", BAR),))
    setup = Setup(tmp_path, song=song, instrument=make_instrument(lead=0.0))

    report = setup.analyze_complete(song)

    (node,) = report.nodes
    assert node.measurements.integrated_lufs is None
    assert node.defects == (Defect.SILENT,)
    assert report.mix.defects == (Defect.SILENT,)
    assert report_failed(report)


def test_silent_track_in_an_audible_mix_fails(tmp_path: Path) -> None:
    setup = Setup(tmp_path, instrument=make_instrument(lead=0.25, pad=0.0))

    report = setup.analyze_complete()

    assert [node.defects for node in report.nodes] == [(), (Defect.SILENT,)]
    assert report.mix.defects == ()
    assert report_failed(report)


def test_mix_over_full_scale_is_a_clipping_defect(tmp_path: Path) -> None:
    setup = Setup(tmp_path, instrument=make_instrument(lead=0.6, pad=0.6))

    report = setup.analyze_complete()

    assert [node.defects for node in report.nodes] == [(), ()]
    assert report.mix.defects == (Defect.CLIPPING, Defect.HEADROOM)
    assert report.mix.measurements.clipped_samples == 2 * FRAMES
    assert report_failed(report)


def test_stem_over_full_scale_is_not_a_defect(tmp_path: Path) -> None:
    setup = Setup(tmp_path, instrument=make_instrument(lead=1.2, pad=-0.6))

    report = setup.analyze_complete()

    lead, bass = report.nodes
    assert lead.measurements.clipped_samples == 2 * FRAMES
    assert lead.defects == bass.defects == ()
    assert report.mix.measurements.clipped_samples == 0
    assert report.mix.defects == ()
    assert not report_failed(report)


def test_gain_turns_a_clipping_mix_clean(tmp_path: Path) -> None:
    setup = Setup(tmp_path, instrument=make_instrument(lead=0.6, pad=0.6))
    song = make_song()
    quiet = dataclasses.replace(
        song, tracks=tuple(dataclasses.replace(track, gain=-6.0) for track in song.tracks)
    )

    report = setup.analyze_complete(quiet)

    assert report.mix.defects == ()
    mix = setup.audio_files.read(setup.mix_path(), FRAMES)
    assert np.allclose(mix.samples, 0.6 * 10 ** (-6 / 20) * 2, rtol=1e-6)
    assert not report_failed(report)


# Adapter failures


class ShortMixer(FakeMixer):
    @override
    def mix(self, stems: Sequence[MixInput], frames: int) -> Audio:
        return Audio(super().mix(stems, frames).samples[:, :-1])


def test_mixer_returning_the_wrong_length_raises(tmp_path: Path) -> None:
    setup = Setup(tmp_path, mixer=ShortMixer())

    with pytest.raises(ValueError, match=f"the mixer returned {FRAMES - 1} frames"):
        setup.analyze()

    with setup.store.lock():
        pass


def test_visualizer_error_propagates_and_publishes_no_record(tmp_path: Path) -> None:
    setup = Setup(tmp_path, visualizer=FakeVisualizer(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        setup.analyze()

    with setup.store.lock():
        pass
    assert not setup.record(1).exists()
    assert setup.store.latest() is None
    assert not list((setup.store.directory / "drafts").glob("*"))
    setup.visualizer.raises = None
    assert setup.analyze().run == 1


# Graph songs (C17): the record holds the wiring and mode, and digests it as the song text.


def graph_song(*, rewired: bool = False, lead: str = "fake") -> Graph:
    """A lead through one drive, wired by hand, and an untouched bass."""
    lead_node = TrackNode("lead", sound=lead, notes=BAR)
    drive = EffectNode("lead.drive", effect="drive", gain_db=-6.0)
    bass = TrackNode("bass", sound="fake:pad", notes=BAR)
    send = Wire("lead", "lead.drive", "audio", gain_db=-12.0 if rewired else 0.0)
    return Graph(120, (lead_node, drive, bass), (send,))


def graph_setup(tmp_path: Path) -> Setup:
    drive = FakeEffect("drive", identity="drive", amount=1.0)
    return Setup(tmp_path, song=graph_song(), effects=(drive,))


def test_a_graph_song_is_analyzed_and_its_record_holds_the_wiring(tmp_path: Path) -> None:
    setup = graph_setup(tmp_path)

    report = setup.analyze_complete(graph_song())

    assert [node.name for node in report.nodes] == ["lead", "lead.drive", "bass"]
    driven = report.nodes[1]
    assert driven.kind == "effect"
    assert driven.upstream == ("lead",)
    mix = setup.audio_files.read(setup.mix_path(), FRAMES)
    # The mix sums the leaves: the driven lead and the dry bass, each at its own gain.
    assert np.allclose(mix.samples, 0.5 * 10 ** (-6 / 20) + 0.2)

    record = json.loads((setup.record(1) / "song.json").read_text())
    assert record["mode"] == "acyclic"
    assert record["wires"] == [
        {"from_name": "lead", "to_name": "lead.drive", "kind": "audio", "gain_db": 0}
    ]
    assert (
        report.song_sha256
        == hashlib.sha256((setup.record(1) / "song.json").read_bytes()).hexdigest()
    )


def test_rewiring_changes_the_record_digest_and_reanalysis_keeps_it(tmp_path: Path) -> None:
    setup = graph_setup(tmp_path)
    first = setup.analyze(graph_song())

    rewired = setup.analyze(graph_song(rewired=True))

    assert rewired.song_sha256 != first.song_sha256
    assert rewired.run == 2
    assert json.loads((setup.record(2) / "song.json").read_text())["wires"][0]["gain_db"] == -12

    again = setup.analyze(graph_song(rewired=True))

    assert again.song_sha256 == rewired.song_sha256
    assert again.run == 3


# The feedback regime: cycle members' keys come from the group digest, so analysis finds the same
# object rendering published.


def loop_graph() -> Graph:
    """RC5: a track into a sum, through a delay and a drive back into the sum, and a leaf."""
    nodes = (
        TrackNode("track", sound="fake", notes=BAR),
        TrackNode("bass", sound="fake:pad", notes=BAR),
        EffectNode("loop", effect="sum"),
        DelayNode("late", delay_samples=512),
        EffectNode("loop.drive", effect="drive"),
        EffectNode("out", effect="sum"),
    )
    wires = (
        Wire("track", "loop", "audio"),
        Wire("loop", "late", "audio"),
        Wire("late", "loop.drive", "audio"),
        Wire("loop.drive", "loop", "audio"),
        Wire("loop", "out", "audio"),
    )
    return Graph(120, nodes, wires, "feedback")


def test_a_feedback_graph_analyzes_through_its_group_keys(tmp_path: Path) -> None:
    """The analysis computes each member's key from the group digest, exactly what rendering
    published, so a rendered feedback graph reads as current and the report stays in song order."""
    graph = loop_graph()
    setup = Setup(
        tmp_path,
        song=graph,
        effects=[
            FakeEffect("sum", identity="sum", amount=1.0),
            FakeEffect("drive", identity="drive", amount=1.0),
        ],
    )

    report = setup.analyze_complete(graph)

    nodes = {node.name: node for node in report.nodes}
    assert [node.name for node in report.nodes] == [node.name for node in graph.nodes]
    assert all(isinstance(node, AnalyzedNode) for node in report.nodes)
    assert nodes["late"].kind == "delay"
    assert nodes["late"].upstream == ("loop",)
    assert nodes["loop"].upstream == ("track", "loop.drive")
    # The members render as one unit, so they share its key.
    keys = [nodes[name].node_key for name in ("loop", "late", "loop.drive")]
    assert len(set(keys)) == 1
    assert keys[0] is not None

    render_song(
        LoadedSong(graph),
        setup.renders,
        SONG_ID,
        setup.instruments,
        setup.audio_files,
        effects=setup.effects,
        timeout_s=10.0,
        force=False,
    )
    again = setup.analyze_complete(graph)

    assert all(isinstance(node, AnalyzedNode) for node in again.nodes)
