"""Analyzing a song's stems, on fakes."""

import dataclasses
import hashlib
import json
import os
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
from poser.core.cache import Stamp
from poser.core.graph import DelayNode, EffectNode, Graph, TrackNode, Wire
from poser.core.loading import LoadedSong
from poser.core.names import Identifier, SongId, TrackName
from poser.core.planning import Unplanned, plan, resolve_all
from poser.core.ports import (
    Effect,
    Instrument,
    Mixer,
    MixInput,
)
from poser.core.rendering import render_song
from poser.core.report import (
    BANDS,
    AnalysisStatus,
    Defect,
    Levels,
    Report,
    bar_dbfs,
    report_failed,
)
from poser.core.song import Note, Song, Track
from poser.core.statuses import StemStatus

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
        assert all(result.status in (StemStatus.RENDERED, StemStatus.SILENT) for result in results)

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

    def record(self, run: int) -> Path:
        return self.store.records() / str(run)

    def latest(self) -> list[Path]:
        """The latest results of a full analysis of the two-track song."""
        images = [
            self.store.image_path(name, self.visualizer.suffix) for name in (LEAD, BASS, None)
        ]
        return [self.store.report_path(), self.store.mix_path(), *images]


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

    report = setup.analyze()

    assert report.run == 1
    assert report.song == "demo"
    assert len(report.nodes) == 2
    assert all(node.status is AnalysisStatus.ANALYZED for node in report.nodes)
    assert not report_failed(report)
    assert report.mix is not None
    assert report.mix.path == str(setup.store.mix_path())
    images = [node.spectrogram for node in report.nodes] + [report.mix.spectrogram]
    assert images == [str(path) for path in setup.latest()[2:]]
    for path in setup.latest():
        assert path.is_file()
    latest = json.loads(setup.store.report_path().read_text())
    assert latest["run"] == 1
    assert latest["mix"]["spectrogram"] == report.mix.spectrogram

    record = setup.record(1)
    assert tree(record) == [
        "images",
        "images/[mix].png",
        "images/bass.png",
        "images/lead.png",
        "report.json",
        "song.json",
    ]
    kept = json.loads((record / "report.json").read_text())
    kept_images = [node["spectrogram"] for node in kept["nodes"]] + [kept["mix"]["spectrogram"]]
    assert kept_images == [
        str(record / "images" / name) for name in ("lead.png", "bass.png", "[mix].png")
    ]
    assert {**kept, "nodes": None, "mix": None} == {**latest, "nodes": None, "mix": None}


def test_mix_is_the_gain_weighted_sum_of_the_stems(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    report = setup.analyze(make_song(gain=-6.0))

    mix = setup.audio_files.read(setup.store.mix_path(), stem_samples(make_song().graph()))
    assert np.allclose(mix.samples, 0.5 * 10 ** (-6 / 20) + 0.2)
    lead, bass = report.nodes
    assert lead.gain_db == -6.0
    assert lead.peak_dbfs == pytest.approx(20 * np.log10(0.5))
    assert bass.gain_db == 0.0


def test_stray_stem_does_not_enter_the_mix(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    stray = setup.store.stem_path(TrackName("removed"))
    write_raw_audio(stray, np.full((2, stem_samples(make_song().graph())), 0.25, dtype=np.float32))

    setup.analyze()

    mix = setup.audio_files.read(setup.store.mix_path(), stem_samples(make_song().graph()))
    assert np.allclose(mix.samples, 0.7)


def test_report_keys_are_the_stamp_keys(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    lead, bass = setup.analyze().nodes

    for report_node, name in ((lead, LEAD), (bass, BASS)):
        stamp = setup.store.read_stamp(name)
        assert isinstance(stamp, Stamp)
        assert (report_node.node_key, report_node.sound_key) == (stamp.track_key, stamp.sound_key)


def spoil(setup: Setup, case: str) -> Song:
    """Spoil the bass stem one way and return the song to analyze."""
    stem = setup.store.stem_path(BASS)
    frames = stem_samples(make_song().graph())
    match case:
        case "missing" | "missing_and_unresolved":
            stem.unlink()
        case "no_stamp":
            setup.store.forget_stamp(BASS)
        case "garbage_stamp":
            setup.store.spoil_stamp(BASS)
        case "stem_changed" | "stem_and_song_changed":
            info = stem.stat()
            write_raw_audio(stem, np.full((2, frames), 0.3, dtype=np.float32))
            os.utime(stem, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))
        case "sound_changed" | "song_and_sound_changed":
            setup.instrument = make_instrument(pad_identity="pad, edited")
        case "wrong_length" | "wrong_length_and_sound_changed":
            _restamp(setup, np.full((2, frames - 1), 0.2, dtype=np.float32))
        case "not_finite":
            samples = np.full((2, frames), 0.2, dtype=np.float32)
            samples[:, 1000] = np.nan
            _restamp(setup, samples)
    if case == "wrong_length_and_sound_changed":
        setup.instrument = make_instrument(pad_identity="pad, edited")
    if case in {"song_changed", "stem_and_song_changed", "song_and_sound_changed"}:
        return make_song(pitch=62)
    if case == "unknown_instrument":
        return make_song(bass="other")
    if case in {"unresolved", "missing_and_unresolved"}:
        return make_song(bass="fake:gone")
    return make_song()


def _restamp(setup: Setup, samples: np.ndarray) -> None:
    """Replace the bass stem and stamp it with its current keys, as a render would."""
    stamp = setup.store.read_stamp(BASS)
    assert isinstance(stamp, Stamp)
    write_raw_audio(setup.store.stem_path(BASS), samples)
    setup.store.write_stamp(BASS, stamp)


FRAMES = stem_samples(make_song().graph())


@pytest.mark.parametrize(
    ("case", "status", "detail"),
    [
        ("missing", AnalysisStatus.MISSING_STEM, None),
        ("missing_and_unresolved", AnalysisStatus.MISSING_STEM, None),
        ("no_stamp", AnalysisStatus.STALE_STEM, "stem has no render stamp"),
        ("garbage_stamp", AnalysisStatus.STALE_STEM, "stem has no render stamp"),
        ("stem_changed", AnalysisStatus.STALE_STEM, "stem changed after it was rendered"),
        ("stem_and_song_changed", AnalysisStatus.STALE_STEM, "stem changed after it was rendered"),
        ("song_changed", AnalysisStatus.STALE_STEM, "song changed since this stem was rendered"),
        # The node key digests the sound, so a changed sound is named before the song is.
        (
            "song_and_sound_changed",
            AnalysisStatus.STALE_STEM,
            "sound changed since this stem was rendered",
        ),
        ("sound_changed", AnalysisStatus.STALE_STEM, "sound changed since this stem was rendered"),
        (
            "wrong_length_and_sound_changed",
            AnalysisStatus.STALE_STEM,
            "sound changed since this stem was rendered",
        ),
        ("unresolved", AnalysisStatus.STALE_STEM, "no sound named 'gone'"),
        ("unknown_instrument", AnalysisStatus.STALE_STEM, "no instrument 'other' in poser.toml"),
        ("wrong_length", AnalysisStatus.STALE_STEM, f"{FRAMES - 1} samples, expected {FRAMES}"),
        ("not_finite", AnalysisStatus.STALE_STEM, "audio contains NaN or infinity"),
    ],
)
def test_unusable_stem_fails_the_run_and_removes_earlier_results(
    tmp_path: Path, case: str, status: AnalysisStatus, detail: str | None
) -> None:
    setup = Setup(tmp_path)
    assert setup.analyze().mix is not None
    song = spoil(setup, case)

    report = setup.analyze(song)

    lead, bass = report.nodes
    assert lead.status is AnalysisStatus.ANALYZED
    assert lead.spectrogram is None
    assert bass.status is status
    assert bass.detail == detail
    assert bass.node_key == node_key_of(song, 1, setup)
    if case in {"missing_and_unresolved", "unresolved", "unknown_instrument"}:
        assert bass.sound_key is None
    else:
        assert bass.sound_key is not None
    assert bass.gain_db == 0.0
    assert bass.kind == "track"
    assert bass.upstream == ()
    assert (bass.peak_dbfs, bass.integrated_lufs, bass.bands_db, bass.bar_dbfs) == (None,) * 4
    assert report.mix is None
    assert report_failed(report)
    assert not [path for path in setup.latest() if path.exists()]
    assert report.run == 2
    assert tree(setup.record(2)) == ["report.json", "song.json"]
    assert json.loads((setup.record(2) / "report.json").read_text())["mix"] is None


def test_records_number_on_and_hold_the_song_and_its_digest(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    runs = [setup.analyze().run, setup.analyze(make_song(gain=-3.0)).run]
    setup.store.discard(BASS)
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

    report = setup.analyze()

    for report_node, node in zip(report.nodes, make_song().graph().nodes, strict=True):
        assert report_node.name == node.name
        assert report_node.kind == node.kind


# Defects


def test_silent_stem_reports_null_loudness_and_a_silent_defect(tmp_path: Path) -> None:
    song = Song(bpm=120, tracks=(Track("quiet", "fake", BAR),))
    setup = Setup(tmp_path, song=song, instrument=make_instrument(lead=0.0))

    report = setup.analyze(song)

    (node,) = report.nodes
    assert node.integrated_lufs is None
    assert node.defects == (Defect.SILENT,)
    assert report.mix is not None
    assert report.mix.defects == (Defect.SILENT,)
    assert report_failed(report)


def test_silent_track_in_an_audible_mix_fails(tmp_path: Path) -> None:
    setup = Setup(tmp_path, instrument=make_instrument(lead=0.25, pad=0.0))

    report = setup.analyze()

    assert [node.defects for node in report.nodes] == [(), (Defect.SILENT,)]
    assert report.mix is not None
    assert report.mix.defects == ()
    assert report_failed(report)


def test_mix_over_full_scale_is_a_clipping_defect(tmp_path: Path) -> None:
    setup = Setup(tmp_path, instrument=make_instrument(lead=0.6, pad=0.6))

    report = setup.analyze()

    assert [node.defects for node in report.nodes] == [(), ()]
    assert report.mix is not None
    assert report.mix.defects == (Defect.CLIPPING, Defect.HEADROOM)
    assert report.mix.clipped_samples == 2 * FRAMES
    assert report_failed(report)


def test_stem_over_full_scale_is_not_a_defect(tmp_path: Path) -> None:
    setup = Setup(tmp_path, instrument=make_instrument(lead=1.2, pad=-0.6))

    report = setup.analyze()

    lead, bass = report.nodes
    assert lead.clipped_samples == 2 * FRAMES
    assert lead.defects == bass.defects == ()
    assert report.mix is not None
    assert report.mix.clipped_samples == 0
    assert report.mix.defects == ()
    assert not report_failed(report)


def test_gain_turns_a_clipping_mix_clean(tmp_path: Path) -> None:
    setup = Setup(tmp_path, instrument=make_instrument(lead=0.6, pad=0.6))
    song = make_song()
    quiet = dataclasses.replace(
        song, tracks=tuple(dataclasses.replace(track, gain=-6.0) for track in song.tracks)
    )

    report = setup.analyze(quiet)

    assert report.mix is not None
    assert report.mix.defects == ()
    mix = setup.audio_files.read(setup.store.mix_path(), FRAMES)
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


def test_visualizer_error_propagates_and_leaves_the_record_unfinished(tmp_path: Path) -> None:
    setup = Setup(tmp_path, visualizer=FakeVisualizer(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        setup.analyze()

    with setup.store.lock():
        pass
    assert tree(setup.record(1)) == ["song.json"]
    setup.visualizer.raises = None
    assert setup.analyze().run == 2


# Graph songs (C17): the record holds the wiring and mode, and digests it as the song text.


def graph_song(*, rewired: bool = False) -> Graph:
    """A lead through one drive, wired by hand, and an untouched bass."""
    lead = TrackNode("lead", sound="fake", notes=BAR)
    drive = EffectNode("lead.drive", effect="drive", gain_db=-6.0)
    bass = TrackNode("bass", sound="fake:pad", notes=BAR)
    send = Wire("lead", "lead.drive", "audio", gain_db=-12.0 if rewired else 0.0)
    return Graph(120, (lead, drive, bass), (send,))


def graph_setup(tmp_path: Path) -> Setup:
    drive = FakeEffect("drive", identity="drive", amount=1.0)
    return Setup(tmp_path, song=graph_song(), effects=(drive,))


def test_a_graph_song_is_analyzed_and_its_record_holds_the_wiring(tmp_path: Path) -> None:
    setup = graph_setup(tmp_path)

    report = setup.analyze(graph_song())

    assert report.mix is not None
    assert [node.name for node in report.nodes] == ["lead", "lead.drive", "bass"]
    driven = report.nodes[1]
    assert driven.kind == "effect"
    assert driven.upstream == ("lead",)
    mix = setup.audio_files.read(setup.store.mix_path(), FRAMES)
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


# The feedback regime: cycle members' keys come from the group digest, so analysis reads the same
# stamps rendering wrote.


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
    stamped, so a rendered feedback graph reads as current and the report stays in song order."""
    graph = loop_graph()
    setup = Setup(
        tmp_path,
        song=graph,
        effects=[
            FakeEffect("sum", identity="sum", amount=1.0),
            FakeEffect("drive", identity="drive", amount=1.0),
        ],
    )

    report = setup.analyze(graph)

    nodes = {node.name: node for node in report.nodes}
    assert [node.name for node in report.nodes] == [node.name for node in graph.nodes]
    assert all(node.status is AnalysisStatus.ANALYZED for node in report.nodes)
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
    again = setup.analyze(graph)

    assert all(node.status is AnalysisStatus.ANALYZED for node in again.nodes)
