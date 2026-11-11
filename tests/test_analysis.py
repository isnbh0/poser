"""Analyzing a song's stems, on fakes."""

import dataclasses
import hashlib
import json
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast, override

import numpy as np
import pytest
from fakes import (
    FakeInstrument,
    FakeMeasurer,
    FakeMixer,
    FakeRecordWriter,
    FakeRenderStore,
    FakeSection,
    FakeSongStore,
    FakeSound,
    FakeVisualizer,
    NpyAudioFiles,
)

from poser.core.analysis import AnalysisTools, analyze_song, measure
from poser.core.audio import Audio, measure_peak_dbfs, stem_samples, write_raw_audio
from poser.core.cache import Stamp, node_key
from poser.core.jsonvalue import Json
from poser.core.names import Identifier, SongId, TrackName
from poser.core.ports import Instrument, Mixer, MixInput, ReportSection, SectionInput, SectionResult
from poser.core.rendering import ResolvedNode, render_song, resolve_node
from poser.core.report import (
    BANDS,
    AnalysisStatus,
    Defect,
    Levels,
    Report,
    SectionData,
    SectionError,
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
        sections: Sequence[ReportSection] = (),
        mixer: Mixer | None = None,
        visualizer: FakeVisualizer | None = None,
        song: Song | None = None,
        instrument: FakeInstrument | None = None,
    ) -> None:
        self.instrument = instrument or make_instrument()
        self.renders = FakeRenderStore(tmp_path / "renders")
        self.store: FakeSongStore = self.renders.song(SONG_ID)
        self.audio_files = NpyAudioFiles()
        self.visualizer = visualizer or FakeVisualizer()
        self.tools = AnalysisTools(
            mixer or FakeMixer(), FakeMeasurer(), self.visualizer, tuple(sections)
        )
        results = render_song(
            song or make_song(),
            self.renders,
            SONG_ID,
            self.instruments,
            self.audio_files,
            timeout_s=10.0,
            force=False,
        )
        assert all(result.status in (StemStatus.RENDERED, StemStatus.SILENT) for result in results)

    @property
    def instruments(self) -> dict[Identifier, Instrument]:
        return {self.instrument.name: self.instrument}

    def analyze(self, song: Song | None = None) -> Report:
        return analyze_song(
            song or make_song(),
            "demo",
            self.renders,
            SONG_ID,
            self.instruments,
            self.audio_files,
            FakeRecordWriter(),
            self.tools,
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


def node_key_of(song: Song, index: int, setup: Setup, *, resolve: bool) -> str:
    """The key the report records for the song's index'th node.

    Analysis resolves the sound only when a stem exists to read, so without one it digests None.
    """
    graph = song.graph()
    node = graph.nodes[index]
    identity = None
    if resolve:
        resolved = resolve_node(node, setup.instruments)
        if isinstance(resolved, ResolvedNode):
            identity = resolved.sound_key
    return node_key(graph, node, identity, ())


# Measuring


def test_measure_takes_peak_clipping_and_bars_from_the_audio_and_levels_from_the_measurer() -> None:
    song = Song(bpm=120, tracks=(Track("lead", "fake", (Note(0, 60, 8, 100),)),))
    samples = np.zeros((2, stem_samples(song)), dtype=np.float32)
    samples[:, :96000] = 0.5
    samples[:, 96000:192000] = 0.25
    samples[0, 10] = 1.5
    samples[1, 20] = -1.25
    levels = Levels(-9.0, dict.fromkeys(BANDS, -3.0))

    measurements = measure(Audio(samples), song, FakeMeasurer(levels))

    assert measurements.peak_dbfs == measure_peak_dbfs(samples)
    assert measurements.clipped_samples == 2
    assert measurements.bar_dbfs == tuple(bar_dbfs(samples, 2.0, 2))
    assert measurements.integrated_lufs == -9.0
    assert measurements.bands_db == levels.bands_db


def test_analysis_tools_refuse_two_sections_with_one_name() -> None:
    with pytest.raises(ValueError, match="two report sections are named 'tempo'"):
        AnalysisTools(
            FakeMixer(),
            FakeMeasurer(),
            FakeVisualizer(),
            (FakeSection("tempo"), FakeSection("tempo")),
        )


# Full and failed runs


def test_analysis_refuses_a_store_whose_stems_the_audio_files_cannot_read(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path)
    wav = FakeRenderStore(tmp_path / "renders", ".wav")

    with setup.store.lock(), pytest.raises(ValueError, match=r"stem suffix \.wav differs"):
        analyze_song(
            make_song(),
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

    mix = setup.audio_files.read(setup.store.mix_path(), stem_samples(make_song()))
    assert np.allclose(mix.samples, 0.5 * 10 ** (-6 / 20) + 0.2)
    lead, bass = report.nodes
    assert lead.gain_db == -6.0
    assert lead.peak_dbfs == pytest.approx(20 * np.log10(0.5))
    assert bass.gain_db == 0.0


def test_stray_stem_does_not_enter_the_mix(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    stray = setup.store.stem_path(TrackName("removed"))
    write_raw_audio(stray, np.full((2, stem_samples(make_song())), 0.25, dtype=np.float32))

    setup.analyze()

    mix = setup.audio_files.read(setup.store.mix_path(), stem_samples(make_song()))
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
    frames = stem_samples(make_song())
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


FRAMES = stem_samples(make_song())


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
    assert bass.node_key == node_key_of(
        song, 1, setup, resolve=case not in {"missing", "missing_and_unresolved"}
    )
    if case in {"missing", "missing_and_unresolved", "unresolved", "unknown_instrument"}:
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


# Sections


class ActingSection(FakeSection):
    """A section that does something to its input, then returns its data."""

    def __init__(self, name: str, act: Callable[[SectionInput], object]) -> None:
        super().__init__(name, {"acted": True})
        self.act = act

    @override
    def track(self, request: SectionInput) -> SectionResult:
        self.act(request)
        return super().track(request)

    @override
    def mix(self, request: SectionInput) -> SectionResult:
        self.act(request)
        return super().mix(request)


class DictSection(FakeSection):
    @override
    def track(self, request: SectionInput) -> SectionResult:
        return cast("SectionResult", {"value": 1})

    @override
    def mix(self, request: SectionInput) -> SectionResult:
        return cast("SectionResult", {"value": 1})


def test_section_data_reaches_every_node_and_the_mix(tmp_path: Path) -> None:
    section = FakeSection("tempo", {"steady": True})
    setup = Setup(tmp_path, [section])

    report = setup.analyze()

    assert report.mix is not None
    expected = {"tempo": SectionData(1, Json.of({"steady": True}), ())}
    assert [dict(node.sections) for node in report.nodes] == [expected, expected]
    assert dict(report.mix.sections) == expected
    assert [request.track for request in section.seen] == [*make_song().tracks, None]
    assert section.parts == ["track", "track", "mix"]
    assert not report_failed(report)


def test_section_defect_fails_the_report(tmp_path: Path) -> None:
    setup = Setup(tmp_path, [FakeSection("tempo", None, ["drift"])])

    report = setup.analyze()

    assert report.mix is not None
    assert report.mix.sections["tempo"] == SectionData(1, Json.of(None), (Identifier("drift"),))
    assert report_failed(report)


def test_failing_sections_are_recorded_and_the_others_still_run(tmp_path: Path) -> None:
    after = FakeSection("after", {"ran": True})
    sections = [
        FakeSection("raising", raises=RuntimeError("boom"), version=2),
        DictSection("dict"),
        after,
    ]
    setup = Setup(tmp_path, sections)

    report = setup.analyze()

    assert report.mix is not None
    for sections_seen in [*(node.sections for node in report.nodes), report.mix.sections]:
        assert sections_seen["raising"] == SectionError(2, "RuntimeError: boom")
        assert sections_seen["dict"] == SectionError(
            1, "TypeError: returned dict, not a SectionResult"
        )
        assert sections_seen["after"] == SectionData(1, Json.of({"ran": True}), ())
    assert len(after.seen) == 3
    assert report_failed(report)


def write_samples(request: SectionInput) -> None:
    request.audio.samples[0, 0] = 1.0


def assign_band(request: SectionInput) -> None:
    request.measurements.bands_db["bass"] = 0.0  # ty: ignore[invalid-assignment]


def clear_bars(request: SectionInput) -> None:
    request.measurements.bar_dbfs.clear()  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize(
    ("act", "error"),
    [
        (write_samples, "ValueError: assignment destination is read-only"),
        (assign_band, "TypeError: 'mappingproxy' object does not support item assignment"),
        (clear_bars, "AttributeError: 'tuple' object has no attribute 'clear'"),
    ],
)
def test_section_cannot_change_what_later_sections_and_the_report_see(
    tmp_path: Path, act: Callable[[SectionInput], None], error: str
) -> None:
    later = FakeSection("later")
    setup = Setup(tmp_path, [ActingSection("acting", act), later])
    alone = FakeSection("later")
    plain = Setup(tmp_path / "plain", [alone]).analyze()

    report = setup.analyze()

    assert report.mix is not None
    for sections in [*(node.sections for node in report.nodes), report.mix.sections]:
        assert sections["acting"] == SectionError(1, error)
    lead_stem = setup.audio_files.read(setup.store.stem_path(LEAD), FRAMES)
    assert np.array_equal(later.seen[0].audio.samples, lead_stem.samples)
    assert [request.measurements for request in later.seen] == [
        request.measurements for request in alone.seen
    ]
    for kept, fresh in zip([*report.nodes, report.mix], [*plain.nodes, plain.mix], strict=True):
        assert fresh is not None
        assert (kept.peak_dbfs, kept.bands_db, kept.bar_dbfs) == (
            fresh.peak_dbfs,
            fresh.bands_db,
            fresh.bar_dbfs,
        )


def test_kept_section_json_cannot_change_the_report(tmp_path: Path) -> None:
    section = FakeSection("tempo", {"beats": [1, 2]})
    setup = Setup(tmp_path, [section])
    report = setup.analyze()

    data = section.track(section.seen[0]).data.value()
    assert isinstance(data, dict)
    data["beats"] = []

    assert report.nodes[0].sections["tempo"] == SectionData(1, Json.of({"beats": [1, 2]}), ())


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
