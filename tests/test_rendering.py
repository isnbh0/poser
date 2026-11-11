"""Rendering a song's tracks into stems, on fakes."""

import dataclasses
import os
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pytest
from fakes import (
    BadOutput,
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

from poser.core import cache, rendering
from poser.core.analysis import AnalysisTools, analyze_song
from poser.core.cache import Stamp, StampProblem, node_key, sound_key
from poser.core.graph import Node
from poser.core.names import Identifier, SongId, TrackName
from poser.core.performance import perform
from poser.core.ports import Instrument, RenderFailure, SongBusyError, SoundFailure
from poser.core.rendering import ResolvedNode, TrackResult, render_song, resolve_node
from poser.core.song import Note, Song, Track
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus, StemStatus

SONG_ID = SongId("songs/demo.yaml")
LEAD = TrackName("lead")
BASS = TrackName("bass")
NOTE = (Note(beat=0, pitch=60, length=1, velocity=100),)


def make_song(lead: str = "fake", bass: str = "fake:pad", *, gain: float = 0.0) -> Song:
    return Song(bpm=120, tracks=(Track("lead", lead, NOTE, gain=gain), Track("bass", bass, NOTE)))


def make_instrument(**sounds: FakeSound) -> FakeInstrument:
    return FakeInstrument(
        "fake", {None: FakeSound(), "pad": FakeSound("a pad", "pad", 0.2), **sounds}
    )


class Setup:
    """A fake instrument, store, and audio files, and a way to render a song on them."""

    def __init__(self, tmp_path: Path, instrument: FakeInstrument | None = None) -> None:
        self.instrument = instrument or make_instrument()
        self.renders = FakeRenderStore(tmp_path / "renders")
        self.store: FakeSongStore = self.renders.song(SONG_ID)
        self.audio_files = NpyAudioFiles()

    @property
    def instruments(self) -> Mapping[Identifier, Instrument]:
        return {self.instrument.name: self.instrument}

    def render(self, song: Song, *, force: bool = False) -> list[TrackResult]:
        return render_song(
            song,
            self.renders,
            SONG_ID,
            self.instruments,
            self.audio_files,
            timeout_s=10.0,
            force=force,
        )


def statuses(results: list[TrackResult]) -> list[object]:
    """The results' statuses, which compare by identity, so a status of another family with the
    same value does not pass."""
    return [result.status for result in results]


def same(seen: list[object], expected: list[object]) -> bool:
    return len(seen) == len(expected) and all(a is b for a, b in zip(seen, expected, strict=True))


def by_name(results: list[TrackResult]) -> dict[TrackName, TrackResult]:
    """One render's results keyed by the node's name, so tests need no render order."""
    return {result.track: result for result in results}


def test_node_of_an_unconfigured_instrument_is_unknown_sound() -> None:
    node = Node("lead", "track", sound="other", notes=NOTE)
    result = resolve_node(node, {Identifier("fake"): make_instrument()})
    assert result == SoundFailure(
        SoundFailureStatus.UNKNOWN_SOUND, "no instrument 'other' in poser.toml"
    )


def test_sound_failure_passes_through_resolve_node() -> None:
    failure = SoundFailure(SoundFailureStatus.LOAD_FAILED, "the preset is corrupt")
    instrument = make_instrument(broken=FakeSound(sound_failure=failure))
    node = Node("lead", "track", sound="fake:broken", notes=NOTE)
    result = resolve_node(node, {instrument.name: instrument})
    assert result is failure


def test_sound_key_changes_with_adapter_id_version_and_identity() -> None:
    node = Node("lead", "track", sound="fake", notes=NOTE)

    def key(adapter_id: str = "fake", version: str = "1", identity: str = "one") -> str:
        instrument = FakeInstrument(
            "fake", {None: FakeSound(identity=identity)}, version, adapter_id
        )
        resolved = resolve_node(node, {instrument.name: instrument})
        assert isinstance(resolved, ResolvedNode)
        assert resolved.sound_key == sound_key(adapter_id, version, identity)
        return resolved.sound_key

    keys = [key(), key(adapter_id="other"), key(version="2"), key(identity="two")]
    assert len(set(keys)) == len(keys)
    assert key() == keys[0]


def test_sound_identity_is_in_the_node_key() -> None:
    """The audit's counterexample: a changed preset changes the key with no song input touched."""
    graph = make_song().graph()
    node = graph.nodes[0]

    def key(identity: str) -> str:
        instrument = FakeInstrument("fake", {None: FakeSound(identity=identity)})
        resolved = resolve_node(node, {instrument.name: instrument})
        assert isinstance(resolved, ResolvedNode)
        return node_key(graph, node, resolved.sound_key, ())

    assert key("one") != key("two")


def test_upstream_keys_are_in_the_node_key() -> None:
    """Forward contract for P3+: an upstream entry changes the key of every node it feeds."""
    graph = make_song().graph()
    node = graph.nodes[0]
    base = node_key(graph, node, None, ())

    fed = node_key(graph, node, None, ((base, "audio", 0.0),))

    assert fed != base
    assert fed != node_key(graph, node, None, ((base, "control", 0.0),))
    assert fed != node_key(graph, node, None, ((base, "audio", -6.0),))


def test_a_gain_only_edit_changes_no_node_key() -> None:
    quiet = make_song().graph()
    loud = make_song(gain=-6.0).graph()

    assert [node_key(quiet, node, None, ()) for node in quiet.nodes] == [
        node_key(loud, node, None, ()) for node in loud.nodes
    ]


def test_render_writes_stamped_stems_and_reports_their_peaks(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    song = make_song()

    results = by_name(setup.render(song))
    lead, bass = results[LEAD], results[BASS]

    assert same(statuses([lead, bass]), [StemStatus.RENDERED] * 2)
    assert lead.stem == setup.store.stem_path(LEAD)
    assert lead.peak_dbfs == pytest.approx(20 * np.log10(0.5))
    assert not lead.cached
    stamp = setup.store.read_stamp(LEAD)
    assert isinstance(stamp, Stamp)
    graph = song.graph()
    resolved = resolve_node(graph.nodes[0], setup.instruments)
    assert isinstance(resolved, ResolvedNode)
    assert stamp.track_key == node_key(graph, graph.nodes[0], resolved.sound_key, ())
    assert stamp.status is StemStatus.RENDERED
    audio = setup.audio_files.read(
        setup.store.stem_path(BASS), perform(song, song.tracks[1]).samples
    )
    assert np.allclose(audio.samples, 0.2)


def test_second_render_of_an_unchanged_song_renders_nothing(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    first = setup.render(make_song())

    second = setup.render(make_song())

    assert len(setup.instrument.renders) == 2
    assert [result.cached for result in second] == [True, True]
    assert [(r.status, r.peak_dbfs, r.stem) for r in second] == [
        (r.status, r.peak_dbfs, r.stem) for r in first
    ]
    assert same(statuses(second), statuses(first))


def test_cached_silent_track_stays_silent(tmp_path: Path) -> None:
    setup = Setup(tmp_path, make_instrument(quiet=FakeSound(identity="quiet", amplitude=0.0)))
    song = make_song(bass="fake:quiet")

    first = by_name(setup.render(song))
    second = by_name(setup.render(song))

    for results in (first, second):
        assert [results[LEAD].status, results[BASS].status] == [
            StemStatus.RENDERED,
            StemStatus.SILENT,
        ]
    assert second[BASS].cached


def test_force_renders_every_track(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.render(make_song())

    results = setup.render(make_song(), force=True)

    assert len(setup.instrument.renders) == 4
    assert [result.cached for result in results] == [False, False]


def test_only_changed_tracks_render_again(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.render(make_song())
    changed = make_song(bass="fake")

    results = by_name(setup.render(changed))

    assert [results[LEAD].cached, results[BASS].cached] == [True, False]
    # Nodes render in topological order, so the pad plays before the default sound each pass.
    assert [request.sound.ref.name for request in setup.instrument.renders] == [
        "pad",
        None,
        None,
    ]


def test_gain_edit_reuses_every_stem(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.render(make_song())

    results = setup.render(make_song(gain=-6.0))

    assert [result.cached for result in results] == [True, True]
    assert len(setup.instrument.renders) == 2


def test_changed_sound_identity_renders_again(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.render(make_song())
    setup.instrument = make_instrument(pad=FakeSound("a pad", "pad, edited", 0.2))

    results = by_name(setup.render(make_song()))

    assert [results[LEAD].cached, results[BASS].cached] == [True, False]


def test_changed_stem_or_spoiled_stamp_renders_again(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    song = make_song()
    setup.render(song)
    stem = setup.store.stem_path(LEAD)
    info = stem.stat()
    setup.audio_files.write(
        stem, setup.audio_files.read(stem, perform(song, song.tracks[0]).samples)
    )
    os.utime(stem, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))
    setup.store.spoil_stamp(BASS)
    assert setup.store.read_stamp(LEAD) is StampProblem.CHANGED

    results = setup.render(song)

    assert [result.cached for result in results] == [False, False]
    assert isinstance(setup.store.read_stamp(LEAD), Stamp)
    assert isinstance(setup.store.read_stamp(BASS), Stamp)


@pytest.mark.parametrize("status", list(RenderFailureStatus))
def test_render_failure_passes_through_and_leaves_no_stem(
    tmp_path: Path, status: RenderFailureStatus
) -> None:
    failure = RenderFailure(status, "the worker failed")
    setup = Setup(tmp_path)
    setup.render(make_song())
    setup.instrument = make_instrument(
        pad=FakeSound("a pad", "pad, now failing", render_failure=failure)
    )

    results = by_name(setup.render(make_song()))
    lead, bass = results[LEAD], results[BASS]

    assert lead.status is StemStatus.RENDERED
    assert bass == TrackResult(BASS, status, detail="the worker failed")
    assert bass.status is status
    assert not setup.store.stem_path(BASS).exists()
    assert setup.store.read_stamp(BASS) is StampProblem.MISSING


def test_unresolved_sound_removes_the_earlier_stem_and_stamp(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.render(make_song())

    results = by_name(setup.render(make_song(bass="fake:gone")))
    lead, bass = results[LEAD], results[BASS]

    assert lead.cached
    assert bass.status is SoundFailureStatus.UNKNOWN_SOUND
    assert bass.detail == "no sound named 'gone'"
    assert not setup.store.stem_path(BASS).exists()
    assert setup.store.read_stamp(BASS) is StampProblem.MISSING


@pytest.mark.parametrize("bad", ["wrong_length", "nan", "no_file"])
def test_unusable_output_is_crashed_and_leaves_no_stem(tmp_path: Path, bad: BadOutput) -> None:
    setup = Setup(tmp_path, make_instrument(pad=FakeSound("a pad", "pad", 0.2, bad_output=bad)))

    results = by_name(setup.render(make_song()))
    lead, bass = results[LEAD], results[BASS]

    assert lead.status is StemStatus.RENDERED
    assert bass.status is RenderFailureStatus.CRASHED
    assert bass.detail is not None
    assert bass.detail.startswith("instrument wrote unusable audio: ")
    assert not setup.store.stem_path(BASS).exists()
    assert setup.store.read_stamp(BASS) is StampProblem.MISSING


def test_silent_render_writes_the_stem_and_reports_silent(tmp_path: Path) -> None:
    setup = Setup(tmp_path, make_instrument(quiet=FakeSound(identity="quiet", amplitude=0.0)))

    (lead,) = setup.render(Song(bpm=120, tracks=(Track("lead", "fake:quiet", NOTE),)))

    assert lead.status is StemStatus.SILENT
    assert lead.stem is not None
    assert lead.stem.is_file()
    stamp = setup.store.read_stamp(LEAD)
    assert isinstance(stamp, Stamp)
    assert stamp.status is StemStatus.SILENT


def test_requests_carry_the_performance_to_a_path_that_is_gone_afterwards(tmp_path: Path) -> None:
    failure = RenderFailure(RenderFailureStatus.CRASHED, "the worker died")
    setup = Setup(
        tmp_path,
        make_instrument(
            pad=FakeSound("a pad", "pad", 0.2, bad_output="wrong_length"),
            dead=FakeSound(identity="dead", render_failure=failure),
        ),
    )
    song = Song(
        bpm=120,
        tracks=(
            Track("lead", "fake", NOTE),
            Track("bass", "fake:pad", (*NOTE, Note(1, 62, 1, 90))),
            Track("drums", "fake:dead", NOTE),
        ),
    )

    statuses_seen = statuses(setup.render(song))

    crashed = RenderFailureStatus.CRASHED
    # Nodes render in the graph's topological order: bass, drums, lead.
    assert same(statuses_seen, [crashed, crashed, StemStatus.RENDERED])
    requests = setup.instrument.renders
    assert [request.sound.ref.name for request in requests] == ["pad", "dead", None]
    assert [request.performance for request in requests] == [
        perform(song, song.tracks[1]),
        perform(song, song.tracks[2]),
        perform(song, song.tracks[0]),
    ]
    assert len({request.audio_path for request in requests}) == 3
    assert not [request.audio_path for request in requests if request.audio_path.exists()]
    assert not [
        request.audio_path.parent for request in requests if request.audio_path.parent.exists()
    ]


def test_perform_runs_before_the_old_stem_is_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = Setup(tmp_path)
    setup.render(make_song())
    stamp = setup.store.read_stamp(LEAD)

    def refuse(song: Song, track: Track) -> None:
        raise RuntimeError("perform failed")

    monkeypatch.setattr(rendering, "perform", refuse)
    with pytest.raises(RuntimeError, match="perform failed"):
        setup.render(make_song(), force=True)

    assert setup.store.stem_path(LEAD).is_file()
    assert setup.store.read_stamp(LEAD) == stamp


def test_render_clears_the_latest_results(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    latest = [setup.store.mix_path(), setup.store.report_path()]
    latest.append(setup.store.image_path(LEAD, setup.renders.stem_suffix))
    for path in latest:
        path.write_text("old")
    record = setup.store.start_record("song", setup.renders.stem_suffix)

    setup.render(make_song())

    assert not [path for path in latest if path.exists()]
    assert record.path.is_dir()


def test_render_refuses_to_start_while_the_song_is_locked(tmp_path: Path) -> None:
    setup = Setup(tmp_path)

    with setup.store.lock(), pytest.raises(SongBusyError):
        setup.render(make_song())

    assert setup.instrument.renders == []
    assert same(statuses(setup.render(make_song())), [StemStatus.RENDERED] * 2)


def test_mismatched_stem_suffix_raises_before_locking(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.renders = FakeRenderStore(tmp_path / "renders", ".wav")

    with setup.store.lock(), pytest.raises(ValueError, match="stem suffix"):
        setup.render(make_song())

    assert setup.instrument.renders == []


def test_track_result_dataclass_replace_keeps_its_rules() -> None:
    result = TrackResult(LEAD, StemStatus.RENDERED, -6.0, Path("lead.npy"))
    with pytest.raises(ValueError, match="cannot be cached"):
        TrackResult(LEAD, RenderFailureStatus.CRASHED, detail="no", cached=True)
    with pytest.raises(ValueError, match="has no detail"):
        dataclasses.replace(result, detail="no")


def test_stems_are_byte_identical_across_the_render_format_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A format bump changes only cache keys: one re-render, then stems the machine cannot tell
    apart, and after that every stamp reports cached again."""
    setup = Setup(tmp_path)
    song = make_song()
    monkeypatch.setattr(cache, "RENDER_FORMAT", 2)
    setup.render(song)
    before = {name: setup.store.stem_path(name).read_bytes() for name in (LEAD, BASS)}

    monkeypatch.setattr(cache, "RENDER_FORMAT", 3)
    bumped = by_name(setup.render(song))

    assert [result.cached for result in bumped.values()] == [False, False]
    assert {name: setup.store.stem_path(name).read_bytes() for name in (LEAD, BASS)} == before

    again = by_name(setup.render(song))

    assert [result.cached for result in again.values()] == [True, True]


def test_topological_order_is_deterministic_and_ties_break_by_name() -> None:
    first = make_song().graph().topological()
    second = make_song().graph().topological()

    assert [node.name for node in first] == ["bass", "lead"]
    assert [node.name for node in first] == [node.name for node in second]


def test_render_order_is_topological_and_report_order_is_song_order(tmp_path: Path) -> None:
    """C4: the two orders are separate contracts, so they are asserted separately."""
    song = make_song()
    instrument = make_instrument()
    instruments = {instrument.name: instrument}
    renders = FakeRenderStore(tmp_path / "renders")
    store = renders.song(SONG_ID)
    audio_files = NpyAudioFiles()
    tools = AnalysisTools(FakeMixer(), FakeMeasurer(), FakeVisualizer())

    rendered = render_song(
        song, renders, SONG_ID, instruments, audio_files, timeout_s=10.0, force=False
    )
    report = analyze_song(
        song, "demo", renders, SONG_ID, instruments, audio_files, FakeRecordWriter(), tools
    )

    graph = song.graph()
    assert [result.track for result in rendered] == [node.name for node in graph.topological()]
    assert [result.track for result in rendered] == ["bass", "lead"]
    assert [node.name for node in report.nodes] == [node.name for node in graph.nodes]
    assert [node.name for node in report.nodes] == ["lead", "bass"]
    assert store.stem_path(BASS).is_file()
