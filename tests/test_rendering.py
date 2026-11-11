"""Rendering a song's tracks into stems, on fakes."""

import dataclasses
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, override

import numpy as np
import pytest
from fakes import (
    BadOutput,
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

from poser.core import cache, rendering
from poser.core.analysis import AnalysisTools, analyze_song
from poser.core.audio import Audio
from poser.core.cache import Stamp, StampProblem, node_key, sound_key
from poser.core.graph import BUFFER_SIZE, Graph, Node, Wire
from poser.core.names import Identifier, NodeName, SongId, TrackName
from poser.core.performance import perform
from poser.core.ports import (
    Effect,
    EffectRenderRequest,
    Instrument,
    RenderFailure,
    ResolvedEffect,
    SongBusyError,
    SoundFailure,
)
from poser.core.rendering import ResolvedNode, TrackResult, render_song, resolve_node
from poser.core.report import AnalysisStatus, NodeReport, Report
from poser.core.song import Note, Song, Track
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus, StemStatus

SONG_ID = SongId("songs/demo.yaml")


class NamedEffect(Effect, Protocol):
    """What every configured effect the tests pass Setup offers: its configured name."""

    @property
    def name(self) -> Identifier: ...


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

    def __init__(
        self,
        tmp_path: Path,
        instrument: FakeInstrument | None = None,
        effect: NamedEffect | None = None,
        effects: NamedEffect | Sequence[NamedEffect] | None = None,
    ) -> None:
        self.instrument = instrument or make_instrument()
        self.effect = effect
        if effects is None:
            self.extra: tuple[NamedEffect, ...] = ()
        elif isinstance(effects, Sequence):
            self.extra = tuple(effects)
        else:
            self.extra = (effects,)
        self.renders = FakeRenderStore(tmp_path / "renders")
        self.store: FakeSongStore = self.renders.song(SONG_ID)
        self.audio_files = NpyAudioFiles()

    @property
    def instruments(self) -> Mapping[Identifier, Instrument]:
        return {self.instrument.name: self.instrument}

    @property
    def effects(self) -> Mapping[Identifier, Effect]:
        """The configured effects, with `effect` replacing its own entry, so a test can swap it."""
        configured = {each.name: each for each in self.extra}
        if self.effect is not None:
            configured[self.effect.name] = self.effect
        return configured

    def render(self, song: Song | Graph, *, force: bool = False) -> list[TrackResult]:
        return render_song(
            song,
            self.renders,
            SONG_ID,
            self.instruments,
            self.audio_files,
            effects=self.effects,
            timeout_s=10.0,
            force=force,
        )

    def analyze(self, song: Song | Graph) -> Report:
        tools = AnalysisTools(FakeMixer(), FakeMeasurer(), FakeVisualizer())
        return analyze_song(
            song,
            "demo",
            self.renders,
            SONG_ID,
            self.instruments,
            self.audio_files,
            FakeRecordWriter(),
            tools,
            effects=self.effects,
        )


def statuses(results: list[TrackResult]) -> list[object]:
    """The results' statuses, which compare by identity, so a status of another family with the
    same value does not pass."""
    return [result.status for result in results]


def same(seen: list[object], expected: list[object]) -> bool:
    return len(seen) == len(expected) and all(a is b for a, b in zip(seen, expected, strict=True))


def by_name(results: list[TrackResult]) -> dict[NodeName, TrackResult]:
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


# Effect chains (RC1, C6, C8, C9, C10, C18): wired rendering, exercised headlessly.


DRIVE = NodeName("lead.drive")


def chain_song(*, gain: float = 0.0, notes: tuple[Note, ...] = NOTE) -> Song:
    """A lead through one drive, and an untouched bass."""
    return Song(
        120,
        (
            Track("lead", "fake", notes, gain=gain, effects=("drive",)),
            Track("bass", "fake:pad", NOTE),
        ),
    )


def chain_setup(tmp_path: Path, **effect: object) -> Setup:
    return Setup(tmp_path, effect=FakeEffect("drive", **effect))  # ty: ignore[invalid-argument-type]


def lead_frames(song: Song) -> int:
    return perform(song, song.tracks[0]).samples


def by_node(report: Report) -> dict[NodeName, NodeReport]:
    return {NodeName(node.name): node for node in report.nodes}


def test_a_chain_renders_track_then_effect_and_the_mix_sums_the_leaf(tmp_path: Path) -> None:
    drive_effect = FakeEffect("drive", amount=2.0)
    setup = Setup(tmp_path, effect=drive_effect)
    song = chain_song(gain=-6.0)

    first = by_name(setup.render(song))
    lead, drive = first[LEAD], first[DRIVE]

    assert same(statuses([lead, drive]), [StemStatus.RENDERED] * 2)
    assert not lead.cached
    assert not drive.cached
    stem = setup.audio_files.read(setup.store.stem_path(DRIVE), lead_frames(song))
    # The effect's output is the track's stem, scaled by the recipe's amount.
    assert np.allclose(stem.samples, 1.0)
    again = by_name(setup.render(song))
    assert [result.cached for result in again.values()] == [True, True, True]
    assert len(setup.instrument.renders) == 2  # The bass, and the lead once.
    assert len(drive_effect.renders) == 1

    report = setup.analyze(song)
    nodes = by_node(report)
    assert nodes[DRIVE].upstream == ("lead",)
    assert nodes[DRIVE].kind == "effect"
    assert nodes[DRIVE].gain_db == -6.0  # The fader compiled onto the chain's leaf.
    mix = setup.audio_files.read(setup.store.mix_path(), lead_frames(song))
    # The mix sums the leaf, at the leaf's gain_db, and never the track's dry stem.
    assert np.allclose(mix.samples, 0.5 * 2.0 * 10 ** (-6 / 20) + 0.2)


def test_a_gain_edit_renders_nothing_and_reaches_only_the_mix(tmp_path: Path) -> None:
    drive_effect = FakeEffect("drive")
    setup = Setup(tmp_path, effect=drive_effect)
    setup.render(chain_song())

    results = by_name(setup.render(chain_song(gain=-6.0)))

    assert [result.cached for result in results.values()] == [True, True, True]
    assert len(drive_effect.renders) == 1
    report = setup.analyze(chain_song(gain=-6.0))
    assert report.mix is not None
    mix = setup.audio_files.read(setup.store.mix_path(), lead_frames(chain_song()))
    assert np.allclose(mix.samples, 0.5 * 2.0 * 10 ** (-6 / 20) + 0.2)


def test_changing_the_effect_re_renders_exactly_the_effect_node(tmp_path: Path) -> None:
    setup = chain_setup(tmp_path)
    setup.render(chain_song())
    setup.effect = FakeEffect("drive", identity="drive, edited", amount=3.0)

    results = by_name(setup.render(chain_song()))

    assert [results[LEAD].cached, results[DRIVE].cached] == [True, False]
    assert len(setup.instrument.renders) == 2  # No new instrument render at all.
    stem = setup.audio_files.read(setup.store.stem_path(DRIVE), lead_frames(chain_song()))
    assert np.allclose(stem.samples, 1.5)


def test_changing_the_track_re_renders_its_chain(tmp_path: Path) -> None:
    """C9's closure: an edit upstream moves every node downstream of it."""
    setup = chain_setup(tmp_path)
    setup.render(chain_song())

    # A note inside the old horizon, so the song's length does not change every key.
    results = by_name(setup.render(chain_song(notes=(*NOTE, Note(0.5, 64, 0.5, 90)))))

    assert [results[LEAD].cached, results[DRIVE].cached, results[BASS].cached] == [
        False,
        False,
        True,
    ]


def test_a_changed_sound_identity_re_renders_the_chain(tmp_path: Path) -> None:
    """C2's forward contract on a chain: the recipe closure sees the sound change."""
    setup = chain_setup(tmp_path)
    setup.render(chain_song())
    setup.instrument = FakeInstrument(
        "fake",
        {None: FakeSound("the default, edited", "edited"), "pad": FakeSound("a pad", "pad", 0.2)},
    )

    results = by_name(setup.render(chain_song()))

    assert [results[LEAD].cached, results[DRIVE].cached, results[BASS].cached] == [
        False,
        False,
        True,
    ]


def test_an_effect_that_fails_leaves_its_downstream_upstream_failed(tmp_path: Path) -> None:
    crashed = RenderFailure(RenderFailureStatus.CRASHED, "the effect died")
    setup = Setup(
        tmp_path,
        effects=(
            FakeEffect("drive", render_failure=crashed),
            FakeEffect("crush", identity="crush"),
        ),
    )
    song = Song(
        120,
        (Track("lead", "fake", NOTE, effects=("drive", "crush")), Track("bass", "fake:pad", NOTE)),
    )
    crush = NodeName("lead.crush")

    results = by_name(setup.render(song))

    assert results[LEAD].status is StemStatus.RENDERED
    assert results[DRIVE].status is RenderFailureStatus.CRASHED
    assert results[crush].status is RenderFailureStatus.UPSTREAM_FAILED
    detail = results[crush].detail
    assert detail is not None
    assert "lead.drive" in detail
    assert not setup.store.stem_path(crush).exists()
    assert setup.store.read_stamp(crush) is StampProblem.MISSING

    report = setup.analyze(song)
    nodes = by_node(report)
    # The failed node and the node it fed left no stems; the track itself is analyzed.
    assert nodes[LEAD].status is AnalysisStatus.ANALYZED
    assert nodes[DRIVE].status is AnalysisStatus.MISSING_STEM
    assert nodes[crush].status is AnalysisStatus.MISSING_STEM


def test_a_failed_track_leaves_its_chain_upstream_failed(tmp_path: Path) -> None:
    failure = RenderFailure(RenderFailureStatus.CRASHED, "the worker failed")
    setup = Setup(
        tmp_path,
        make_instrument(pad=FakeSound("a pad", "pad", 0.2, render_failure=failure)),
        FakeEffect("drive"),
    )
    song = Song(120, (Track("lead", "fake:pad", NOTE, effects=("drive",)),))

    results = by_name(setup.render(song))

    assert results[LEAD].status is RenderFailureStatus.CRASHED
    assert results[DRIVE].status is RenderFailureStatus.UPSTREAM_FAILED
    detail = results[DRIVE].detail
    assert detail is not None
    assert "lead" in detail

    report = setup.analyze(song)
    nodes = by_node(report)
    assert nodes[LEAD].status is AnalysisStatus.MISSING_STEM
    assert nodes[DRIVE].status is AnalysisStatus.MISSING_STEM
    assert report.mix is None


def test_an_effect_refusing_an_instrument_reports_load_failed(tmp_path: Path) -> None:
    """C8's role-aware refusal, at the fake level; the real one lives in the worker."""
    setup = chain_setup(
        tmp_path,
        render_failure=RenderFailure(RenderFailureStatus.LOAD_FAILED, "'x.vst3' is not an effect"),
    )
    song = chain_song()

    results = by_name(setup.render(song))

    assert results[LEAD].status is StemStatus.RENDERED
    assert results[DRIVE].status is RenderFailureStatus.LOAD_FAILED
    detail = results[DRIVE].detail
    assert detail is not None
    assert "is not an effect" in detail


# Buses, sends, sidechain, and fan-out (RC2, RC3, RC4, C13): wired graphs with many inputs.

FRAMES = lead_frames(make_song())


def graph_of(nodes: tuple[Node, ...], wires: tuple[Wire, ...]) -> Graph:
    """A direct-frontend graph at one tempo, as a script's `build()` returns one."""
    return Graph(120, nodes, wires)


def key_of(setup: Setup, name: str) -> str:
    stamp = setup.store.read_stamp(NodeName(name))
    assert isinstance(stamp, Stamp)
    return stamp.track_key


def test_a_bus_is_a_leaf_whose_sends_scale_and_sum_into_the_mix(tmp_path: Path) -> None:
    """RC2: three drums into a sum bus; the bus is the only leaf, so the mix carries it alone."""
    setup = Setup(tmp_path, effects=(FakeEffect("sum", identity="sum", amount=1.0),))
    dry = 0.2  # The pad sound's amplitude.

    def bus_graph(hat_send_db: float, hat_gain_db: float = 0.0) -> Graph:
        nodes = (
            Node("kick", "track", sound="fake:pad", notes=NOTE),
            Node("snare", "track", sound="fake:pad", notes=NOTE),
            Node("hat", "track", sound="fake:pad", notes=NOTE, gain_db=hat_gain_db),
            Node("drums", "effect", effect="sum", gain_db=-6.0),
        )
        wires = (
            Wire("kick", "drums", "audio"),
            Wire("snare", "drums", "audio"),
            Wire("hat", "drums", "audio", gain_db=hat_send_db),
        )
        return graph_of(nodes, wires)

    loud = by_name(setup.render(bus_graph(0.0)))
    report = setup.analyze(bus_graph(0.0))

    assert same(statuses(list(loud.values())), [StemStatus.RENDERED] * 4)
    bus_stem = setup.audio_files.read(setup.store.stem_path(NodeName("drums")), FRAMES)
    # The bus stem is the three sends at their wire gains; no bus node renders its own sound.
    assert np.allclose(bus_stem.samples, dry * (2 + 10 ** (0 / 20)))
    assert by_node(report)[NodeName("drums")].upstream == ("kick", "snare", "hat")
    assert report.mix is not None
    mix = setup.audio_files.read(setup.store.mix_path(), FRAMES)
    # The mix sums the bus, the only leaf, at the bus's gain_db, and nothing dry.
    assert np.allclose(mix.samples, dry * 3 * 10 ** (-6 / 20))

    # A non-leaf node's mix fader neither scales its send nor invalidates downstream stems.
    faded = bus_graph(0.0, hat_gain_db=-12.0)
    assert all(result.cached for result in setup.render(faded))
    assert setup.analyze(faded).mix is not None
    faded_mix = setup.audio_files.read(setup.store.mix_path(), FRAMES)
    assert np.allclose(faded_mix.samples, mix.samples)

    # A send at -12 dB instead of 0 dB is in the downstream key: the bus re-renders, the rest not.
    quiet = by_name(setup.render(bus_graph(-12.0)))
    assert [quiet[NodeName(name)].cached for name in ("kick", "snare", "hat", "drums")] == [
        True,
        True,
        True,
        False,
    ]
    quiet_report = setup.analyze(bus_graph(-12.0))
    assert quiet_report.mix is not None
    quiet_mix = setup.audio_files.read(setup.store.mix_path(), FRAMES)
    assert not np.allclose(quiet_mix.samples, mix.samples)
    assert np.allclose(quiet_mix.samples, dry * (2 + 10 ** (-12 / 20)) * 10 ** (-6 / 20))


def test_a_control_edge_keys_the_duck_on_both_sides(tmp_path: Path) -> None:
    """RC3, corrected: the duck's key digests the kick's key through the control wire and the
    pad's key through its audio wire; the kick's key digests only the kick."""
    duck_effect = FakeEffect("duck", identity="duck", amount=1.0, sidechain=True)
    setup = Setup(tmp_path, effect=duck_effect)

    def sidechain_graph(kick_notes: tuple[Note, ...], pad_notes: tuple[Note, ...]) -> Graph:
        nodes = (
            Node("pad", "track", sound="fake", notes=pad_notes),
            Node("kick", "track", sound="fake:pad", notes=kick_notes),
            Node("duck", "effect", effect="duck"),
        )
        wires = (Wire("pad", "duck", "audio"), Wire("kick", "duck", "control"))
        return graph_of(nodes, wires)

    first = sidechain_graph(NOTE, NOTE)
    results = by_name(setup.render(first))

    assert same(statuses(list(results.values())), [StemStatus.RENDERED] * 3)
    # The control edge crossed to the effect, and the ducked output is quieter than the pad's dry
    # sound: the kick's 0.2 peak pulls the fake's output down to four fifths of it.
    (request,) = duck_effect.renders
    assert request.control is not None
    assert float(np.max(np.abs(request.control.samples))) == pytest.approx(0.2)
    stem = setup.audio_files.read(setup.store.stem_path(NodeName("duck")), FRAMES)
    assert np.allclose(stem.samples, 0.5 * (1 - 0.2))
    duck_key, pad_key, kick_key = (key_of(setup, name) for name in ("duck", "pad", "kick"))

    # A changed kick note is inside the old horizon, so the song's length stays put.
    setup.render(sidechain_graph((*NOTE, Note(0.5, 38, 0.5, 90)), NOTE))
    assert key_of(setup, "duck") != duck_key
    assert key_of(setup, "kick") != kick_key
    assert key_of(setup, "pad") == pad_key

    setup.render(sidechain_graph(NOTE, (*NOTE, Note(0.5, 64, 0.5, 90))))
    assert key_of(setup, "duck") != duck_key
    assert key_of(setup, "pad") != pad_key
    assert key_of(setup, "kick") == kick_key


def test_fan_out_splits_into_two_independently_cached_leaves(tmp_path: Path) -> None:
    """RC4: one node feeds two effects; each branch caches by its own closure alone (C12)."""
    left_effect = FakeEffect("left", identity="left", amount=2.0)
    right_effect = FakeEffect("right", identity="right", amount=1.0)
    setup = Setup(tmp_path, effects=(left_effect, right_effect))
    nodes = (
        Node("lead", "track", sound="fake", notes=NOTE),
        Node("lead.left", "effect", effect="left"),
        Node("lead.right", "effect", effect="right"),
    )
    wires = (Wire("lead", "lead.left", "audio"), Wire("lead", "lead.right", "audio"))

    setup.render(graph_of(nodes, wires))
    setup.analyze(graph_of(nodes, wires))
    mix = setup.audio_files.read(setup.store.mix_path(), FRAMES)
    # Both branches are leaves, so the mix sums them, each at its leaf's gain_db.
    assert np.allclose(mix.samples, 0.5 * 2.0 + 0.5)

    # Mutating one branch's parameters leaves the sibling cached.
    setup.extra = (FakeEffect("left", identity="left, edited", amount=3.0), right_effect)
    results = by_name(setup.render(graph_of(nodes, wires)))

    assert [results[NodeName("lead")].cached, results[NodeName("lead.left")].cached] == [
        True,
        False,
    ]
    assert results[NodeName("lead.right")].cached
    assert len(right_effect.renders) == 1  # The sibling never rendered again.
    stem = setup.audio_files.read(setup.store.stem_path(NodeName("lead.left")), FRAMES)
    assert np.allclose(stem.samples, 1.5)


def test_an_effect_that_takes_no_control_wire_reports_load_failed(tmp_path: Path) -> None:
    """C13's checked rule at the fake level: the refusal names the effect and the capability."""
    setup = Setup(tmp_path, effect=FakeEffect("drive"))
    nodes = (
        Node("pad", "track", sound="fake", notes=NOTE),
        Node("kick", "track", sound="fake:pad", notes=NOTE),
        Node("wettened", "effect", effect="drive"),
    )
    wires = (Wire("pad", "wettened", "audio"), Wire("kick", "wettened", "control"))

    results = by_name(setup.render(graph_of(nodes, wires)))

    assert results[NodeName("pad")].status is StemStatus.RENDERED
    assert results[NodeName("kick")].status is StemStatus.RENDERED
    assert results[NodeName("wettened")].status is RenderFailureStatus.LOAD_FAILED
    detail = results[NodeName("wettened")].detail
    assert detail is not None
    assert "'drive'" in detail
    assert "sidechain" in detail
    assert not setup.store.stem_path(NodeName("wettened")).exists()


# The feedback regime (RC5, C20, B3, C22, C-cycles): one block pass per cycle group, on fakes.


LOOP_SUM = NodeName("loop")
LOOP_DRIVE = NodeName("loop.drive")
LATE = NodeName("late")


def loop_graph(*, delay_samples: int = BUFFER_SIZE, notes: tuple[Note, ...] = NOTE) -> Graph:
    """RC5: a track feeds a sum, the sum runs through a delay and a drive back into itself, and
    the loop also feeds a leaf. A pad stands apart from the loop."""
    nodes = (
        Node("track", "track", sound="fake", notes=notes),
        Node("bass", "track", sound="fake:pad", notes=NOTE),
        Node("loop", "effect", effect="sum"),
        Node("late", "delay", delay_samples=delay_samples),
        Node("loop.drive", "effect", effect="drive"),
        Node("out", "effect", effect="sum"),
    )
    wires = (
        Wire("track", "loop", "audio"),
        Wire("loop", "late", "audio"),
        Wire("late", "loop.drive", "audio"),
        Wire("loop.drive", "loop", "audio", gain_db=-12),
        Wire("loop", "out", "audio"),
    )
    return Graph(120, nodes, wires, "feedback")


def loop_setup(tmp_path: Path) -> tuple[Setup, FakeEffect, FakeEffect]:
    """A setup with the loop's two recipes, handed back so tests can read their counters."""
    sum_effect = FakeEffect("sum", identity="sum", amount=1.0)
    drive_effect = FakeEffect("drive", identity="drive", amount=2.0)
    return Setup(tmp_path, effects=(sum_effect, drive_effect)), sum_effect, drive_effect


def stem_of(setup: Setup, name: NodeName, frames: int) -> Audio:
    return setup.audio_files.read(setup.store.stem_path(name), frames)


def reference_loop(
    frames: int, amplitude: float, amount: float, delay: int, return_gain_db: float
) -> dict[str, np.ndarray]:
    """The loop as a straightforward per-sample reference: the delay's output is its input read
    `delay` earlier, silence before the first fill, every stored signal rounded once to float32."""
    send = 10 ** (return_gain_db / 20)
    track = np.full(frames, amplitude, np.float32)
    summed = np.zeros(frames, np.float32)
    delayed = np.zeros(frames, np.float32)
    driven = np.zeros(frames, np.float32)
    for t in range(frames):
        held = summed[t - delay] if t >= delay else np.float32(0.0)
        delayed[t] = held
        driven[t] = np.float32(np.float64(held) * amount)
        summed[t] = np.float32(np.float64(track[t]) + np.float64(driven[t]) * send)
    return {"sum": summed, "delay": delayed, "drive": driven}


def test_the_loop_matches_a_per_sample_reference_and_holds_silence_before_fill(
    tmp_path: Path,
) -> None:
    """C21 and C22: the block pass is exactly the delay equation, silence before the line fills,
    and no dry term anywhere in the loop."""
    setup, _, _ = loop_setup(tmp_path)
    graph = loop_graph()

    results = by_name(setup.render(graph))

    assert all(result.status is StemStatus.RENDERED for result in results.values())
    expected = reference_loop(FRAMES, 0.5, 2.0, BUFFER_SIZE, return_gain_db=-12)
    for name, reference in (("loop", "sum"), ("late", "delay"), (LOOP_DRIVE, "drive")):
        stem = stem_of(setup, NodeName(name), FRAMES).samples[0]
        assert np.array_equal(stem, expected[reference]), name
    # Silence before first fill: the loop's sound enters half a block late.
    assert np.array_equal(
        stem_of(setup, LATE, FRAMES).samples[0][:BUFFER_SIZE], np.zeros(BUFFER_SIZE, np.float32)
    )
    assert not np.any(stem_of(setup, LATE, FRAMES).samples[0][:BUFFER_SIZE])


def test_a_feedback_render_is_deterministic(tmp_path: Path) -> None:
    setup, _, _ = loop_setup(tmp_path)
    setup.render(loop_graph())
    before = {
        node.name: setup.store.stem_path(NodeName(node.name)).read_bytes()
        for node in loop_graph().nodes
    }

    setup.render(loop_graph(), force=True)
    after = {
        node.name: setup.store.stem_path(NodeName(node.name)).read_bytes()
        for node in loop_graph().nodes
    }

    assert before == after


def test_mutating_the_track_rerenders_the_whole_cycle_group_and_downstream(tmp_path: Path) -> None:
    """C-cycles: one digest covers the group, so a change to any member's upstream re-renders
    every member and the group's downstream, and unrelated nodes stay cached."""
    setup, sum_effect, drive_effect = loop_setup(tmp_path)
    setup.render(loop_graph())
    renders_after_first = len(setup.instrument.renders)

    changed = loop_graph(notes=(*NOTE, Note(0.5, 64, 0.5, 90)))
    results = by_name(setup.render(changed))

    assert [
        results[NodeName(name)].cached
        for name in ("track", "bass", "loop", "late", LOOP_DRIVE, "out")
    ] == [
        False,
        True,
        False,
        False,
        False,
        False,
    ]
    assert len(sum_effect.group_renders) == 2  # The group rendered once more, as one job.
    # The first effect member in name order runs every group job; the drive instance never does.
    assert len(drive_effect.group_renders) == 0
    assert len(setup.instrument.renders) == renders_after_first + 1  # Only the track played again.


def test_a_gain_only_edit_renders_nothing(tmp_path: Path) -> None:
    """The fader is mixdown-only, so the group's digest never sees it."""
    setup, sum_effect, _ = loop_setup(tmp_path)
    setup.render(loop_graph())
    renders = len(setup.instrument.renders)

    results = by_name(setup.render(loop_graph()))  # Same value, so also a pure cache check.
    quiet_nodes = loop_graph().nodes
    quiet = Graph(
        120,
        tuple(dataclasses.replace(node, gain_db=-6.0) for node in quiet_nodes),
        loop_graph().wires,
        "feedback",
    )
    results = by_name(setup.render(quiet))

    assert [
        results[NodeName(name)].cached
        for name in ("track", "bass", "loop", "late", LOOP_DRIVE, "out")
    ] == [True] * 6
    assert len(sum_effect.group_renders) == 1
    assert len(setup.instrument.renders) == renders


def test_termination_is_structural_in_the_delay_size(tmp_path: Path) -> None:
    """C22: one job per group whatever the delay declares, over exactly ceil(horizon / block)
    blocks."""
    setup, sum_effect, _ = loop_setup(tmp_path)
    setup.render(loop_graph())
    long_setup, long_sum, _ = loop_setup(tmp_path)
    long_setup.render(loop_graph(delay_samples=4 * BUFFER_SIZE))

    assert len(sum_effect.group_renders) == len(long_sum.group_renders) == 1
    assert sum_effect.group_blocks == [[512] * (FRAMES // BUFFER_SIZE) + [FRAMES % BUFFER_SIZE]]
    assert long_sum.group_blocks == sum_effect.group_blocks


def test_a_standalone_delay_is_a_sample_exact_shift(tmp_path: Path) -> None:
    """A delay off any cycle is a straight-line node: out[t] = in[t - delay_samples], silence
    before the third sample, and cached like any stem."""
    setup = Setup(tmp_path)
    nodes = (
        Node("track", "track", sound="fake", notes=NOTE),
        Node("late", "delay", delay_samples=3),
    )
    graph = Graph(120, nodes, (Wire("track", "late", "audio"),))

    results = by_name(setup.render(graph))
    track = stem_of(setup, NodeName("track"), FRAMES).samples
    late = stem_of(setup, NodeName("late"), FRAMES).samples

    assert results[NodeName("late")].status is StemStatus.RENDERED
    assert np.array_equal(late[:, :3], np.zeros((2, 3), np.float32))
    assert np.array_equal(late[:, 3:], track[:, :-3])
    again = by_name(setup.render(graph))
    assert again[NodeName("late")].cached


def test_a_group_whose_external_upstream_fails_fails_whole(tmp_path: Path) -> None:
    failure = RenderFailure(RenderFailureStatus.CRASHED, "the worker failed")
    setup = Setup(
        tmp_path,
        instrument=make_instrument(dead=FakeSound(identity="dead", render_failure=failure)),
        effects=FakeEffect("sum", identity="sum", amount=1.0),
    )
    nodes = (
        Node("track", "track", sound="fake:dead", notes=NOTE),
        Node("loop", "effect", effect="sum"),
        Node("late", "delay", delay_samples=BUFFER_SIZE),
    )
    graph = Graph(
        120,
        nodes,
        (
            Wire("track", "loop", "audio"),
            Wire("loop", "late", "audio"),
            Wire("late", "loop", "audio"),
        ),
        "feedback",
    )

    results = by_name(setup.render(graph))

    assert results[NodeName("track")].status is RenderFailureStatus.CRASHED
    assert results[LOOP_SUM].status is RenderFailureStatus.UPSTREAM_FAILED
    assert results[LOOP_SUM].detail == "upstream 'track' did not render"
    assert results[LATE].detail == "upstream 'loop' did not render"
    assert not setup.store.stem_path(LATE).exists()


class RefusingEffect(FakeEffect):
    """A fake effect whose recipe never resolves, as a broken configuration would not."""

    @override
    def resolve(self, recipe: str) -> ResolvedEffect | SoundFailure:
        return SoundFailure(SoundFailureStatus.UNKNOWN_SOUND, "the recipe is broken")


def test_a_group_member_that_cannot_resolve_fails_the_group_naming_it(tmp_path: Path) -> None:
    setup = Setup(
        tmp_path, effects=(RefusingEffect("sum", identity="sum", amount=1.0), FakeEffect("drive"))
    )

    results = by_name(setup.render(loop_graph()))

    assert results[LOOP_SUM].status is SoundFailureStatus.UNKNOWN_SOUND
    assert results[LOOP_SUM].detail == "the recipe is broken"
    assert results[LATE].status is RenderFailureStatus.UPSTREAM_FAILED
    assert results[LATE].detail == "upstream 'loop' did not render"
    assert results[LOOP_DRIVE].detail == "upstream 'loop' did not render"


class OtherAdapterEffect(FakeEffect):
    """A fake effect of another adapter, so a loop spanning two adapters can be built."""

    @override
    @property
    def adapter_id(self) -> Identifier:
        return Identifier("other")


def test_a_loop_spanning_two_adapters_is_refused_naming_them(tmp_path: Path) -> None:
    setup = Setup(
        tmp_path,
        effects=(
            FakeEffect("sum", identity="sum", amount=1.0),
            OtherAdapterEffect("drive", identity="drive", amount=1.0),
        ),
    )

    results = by_name(setup.render(loop_graph()))

    failure = results[LOOP_SUM]
    assert failure.status is RenderFailureStatus.LOAD_FAILED
    assert failure.detail is not None
    assert "different adapters" in failure.detail
    assert "fake" in failure.detail
    assert "other" in failure.detail


class NoGroupPassEffect:
    """A fake effect adapter that never learned the block pass, seen only as an Effect."""

    def __init__(self, inner: FakeEffect) -> None:
        self._inner = inner
        self.name = inner.name

    @property
    def adapter_id(self) -> Identifier:
        return self._inner.adapter_id

    @property
    def adapter_version(self) -> str:
        return self._inner.adapter_version

    def resolve(self, recipe: str) -> ResolvedEffect | SoundFailure:
        return self._inner.resolve(recipe)

    def render(self, request: EffectRenderRequest, timeout_s: float) -> RenderFailure | None:
        return self._inner.render(request, timeout_s)


def test_an_adapter_without_a_block_pass_refuses_the_group(tmp_path: Path) -> None:
    setup = Setup(
        tmp_path,
        effects=(
            NoGroupPassEffect(FakeEffect("sum", identity="sum", amount=1.0)),
            FakeEffect("drive"),
        ),
    )

    results = by_name(setup.render(loop_graph()))

    failure = results[LOOP_SUM]
    assert failure.status is RenderFailureStatus.LOAD_FAILED
    assert failure.detail is not None
    assert "no block pass" in failure.detail
