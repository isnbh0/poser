"""Planning a graph's render units, and the law that each unit's key digests its render input."""

import dataclasses
from collections.abc import Mapping

import pytest
from fakes import FakeEffect, FakeInstrument, FakeSound

from poser.core import cache
from poser.core.cache import (
    DelayInput,
    DelayMemberInput,
    EffectInput,
    EffectMemberInput,
    GroupFeed,
    GroupInput,
    RenderInput,
    StemRef,
    TrackInput,
    unit_key,
)
from poser.core.graph import BUFFER_SIZE, DelayNode, EffectNode, Graph, GraphNode, TrackNode, Wire
from poser.core.jsonvalue import JsonValue, digest
from poser.core.names import Identifier, NodeName
from poser.core.performance import NoteOff, NoteOn, Performance, PitchSupport
from poser.core.planning import GroupUnit, Plan, TrackUnit, Unit, Unplanned, plan, resolve_all
from poser.core.ports import Effect, Instrument, SoundFailure
from poser.core.song import Note
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus

KEY_A = "a" * 64
KEY_B = "b" * 64
PERFORMANCE = Performance(48000, (NoteOn(0, 60, 100), NoteOff(24000, 60)))
REF = StemRef(KEY_A, 0, -3.0)


# The key law, field by field. Each input maps every one of its fields to a changed value; a
# field added later without a variant fails `test_every_field_has_a_variant`.


def group_input() -> GroupInput:
    return GroupInput(
        frames=48000,
        members=(EffectMemberInput(NodeName("a"), KEY_A), DelayMemberInput(NodeName("b"), 64)),
        wires=(Wire("a", "b", "audio"), Wire("b", "a", "audio", gain_db=-6)),
        inputs=(GroupFeed(NodeName("a"), REF, "audio"),),
    )


BASES: tuple[RenderInput, ...] = (
    TrackInput(KEY_A, PERFORMANCE),
    EffectInput(KEY_A, 48000, (REF,), None),
    DelayInput(64, 48000, (REF,)),
    group_input(),
)

VARIANTS: Mapping[type, Mapping[str, object]] = {
    TrackInput: {
        "sound": KEY_B,
        "performance": Performance(48000, (NoteOn(0, 60, 100), NoteOff(24001, 60))),
    },
    EffectInput: {
        "recipe": KEY_B,
        "frames": 48001,
        "inputs": (REF, REF),
        "control": StemRef(KEY_B, 0),
    },
    DelayInput: {"delay_samples": 65, "frames": 48001, "inputs": ()},
    GroupInput: {
        "frames": 48001,
        "members": (
            EffectMemberInput(NodeName("a"), KEY_B),
            DelayMemberInput(NodeName("b"), 64),
        ),
        "wires": (Wire("a", "b", "audio"), Wire("b", "a", "audio", gain_db=-7)),
        "inputs": (GroupFeed(NodeName("a"), REF, "control"),),
    },
}

# The parts inputs hold, varied the same way through their `to_data`.
type Part = StemRef | EffectMemberInput | DelayMemberInput | GroupFeed

PARTS: tuple[tuple[Part, Mapping[str, object]], ...] = (
    (REF, {"unit_key": KEY_B, "index": 1, "gain_db": -4.0}),
    (EffectMemberInput(NodeName("a"), KEY_A), {"name": NodeName("c"), "recipe": KEY_B}),
    (DelayMemberInput(NodeName("b"), 64), {"name": NodeName("c"), "delay_samples": 65}),
    (
        GroupFeed(NodeName("a"), REF, "audio"),
        {"member": NodeName("b"), "ref": StemRef(KEY_B, 0), "kind": "control"},
    ),
)


def names(value: object) -> set[str]:
    assert dataclasses.is_dataclass(value)
    return {field.name for field in dataclasses.fields(value)}


@pytest.mark.parametrize("base", BASES, ids=lambda base: type(base).__name__)
def test_every_field_has_a_variant(base: RenderInput) -> None:
    assert set(VARIANTS[type(base)]) == names(base)


@pytest.mark.parametrize("base", BASES, ids=lambda base: type(base).__name__)
def test_a_key_is_the_digest_of_its_input_and_every_field_changes_it(base: RenderInput) -> None:
    assert unit_key(base) == digest(base.to_data())
    for field in dataclasses.fields(base):
        changed = dataclasses.replace(base, **{field.name: VARIANTS[type(base)][field.name]})
        assert unit_key(changed) != unit_key(base), field.name


@pytest.mark.parametrize(("part", "variants"), PARTS, ids=lambda part: type(part).__name__)
def test_every_field_of_an_input_part_changes_its_data(
    part: Part, variants: Mapping[str, object]
) -> None:
    assert set(variants) == names(part)
    for name, value in variants.items():
        changed = dataclasses.replace(part, **{name: value})
        assert changed.to_data() != part.to_data(), name


@pytest.mark.parametrize("base", BASES, ids=lambda base: type(base).__name__)
def test_a_render_format_bump_changes_every_key(
    base: RenderInput, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = unit_key(base)
    monkeypatch.setattr(cache, "RENDER_FORMAT", cache.RENDER_FORMAT + 1)
    assert unit_key(base) != before


def test_whole_and_float_gains_give_one_key() -> None:
    assert unit_key(DelayInput(64, 48000, (StemRef(KEY_A, 0, -3),))) == unit_key(
        DelayInput(64, 48000, (StemRef(KEY_A, 0, -3.0),))
    )


def test_group_members_must_be_distinct_and_in_name_order() -> None:
    with pytest.raises(ValueError, match="name order"):
        dataclasses.replace(group_input(), members=tuple(reversed(group_input().members)))


def test_a_track_input_is_tagged_with_its_kind_and_format() -> None:
    data = TrackInput(KEY_A, PERFORMANCE).to_data()
    assert isinstance(data, dict)
    assert data["kind"] == "track"
    fmt: JsonValue = data["format"]
    assert isinstance(fmt, dict)
    assert set(fmt) == {"render_format", "sample_rate", "channels", "bit_depth"}


# Planning graphs on fakes.


def instruments(pitches: PitchSupport = PitchSupport.SEMITONES) -> Mapping[Identifier, Instrument]:
    sounds = {None: FakeSound(), "pad": FakeSound("a pad", "pad", 0.2)}
    return {Identifier("fake"): FakeInstrument("fake", sounds, pitches=pitches)}


def effects() -> Mapping[Identifier, Effect]:
    made = (FakeEffect("drive"), FakeEffect("sum", identity="sum", amount=1.0))
    return {each.name: each for each in made}


def plan_of(graph: Graph, pitches: PitchSupport = PitchSupport.SEMITONES) -> Plan:
    found = instruments(pitches)
    return plan(graph, resolve_all(graph, found, effects()))


def unit_of(graph: Graph, name: str, pitches: PitchSupport = PitchSupport.SEMITONES) -> Unit:
    return plan_of(graph, pitches).place(name)[0]


def key(graph: Graph, name: str) -> str:
    unit = unit_of(graph, name)
    assert not isinstance(unit, Unplanned)
    return unit.key


def chain(
    *,
    bpm: float = 120,
    track: str = "lead",
    sound: str = "fake",
    notes: tuple[Note, ...] = (Note(0, 60, 1, 100),),
    gain_db: float = 0.0,
    pad_beats: float = 1,
) -> Graph:
    """A track through a drive, and a pad on its own."""
    nodes: tuple[GraphNode, ...] = (
        TrackNode(track, sound=sound, notes=notes, gain_db=gain_db),
        EffectNode("drive", effect="drive"),
        TrackNode("pad", sound="fake:pad", notes=(Note(0, 48, pad_beats, 100),)),
    )
    return Graph(bpm, nodes, (Wire(track, "drive", "audio"),))


def test_a_track_name_or_gain_leaves_its_key_and_its_downstream_key() -> None:
    base = chain()
    for other in (chain(track="melody"), chain(gain_db=-6.0)):
        renamed = next(node.name for node in other.nodes if isinstance(node, TrackNode))
        assert key(other, renamed) == key(base, "lead")
        assert key(other, "drive") == key(base, "drive")


def test_a_wire_gain_into_an_effect_changes_the_effect_key() -> None:
    base = chain()
    louder = dataclasses.replace(base, wires=(Wire("lead", "drive", "audio", gain_db=-6),))
    assert key(louder, "lead") == key(base, "lead")
    assert key(louder, "drive") != key(base, "drive")


def test_tempo_reaches_a_key_only_through_the_samples_a_track_plays() -> None:
    base = chain(bpm=120)
    # Half the tempo and half the beats: every event lands on the same sample.
    same_samples = chain(bpm=60, notes=(Note(0, 60, 0.5, 100),), pad_beats=0.5)
    assert key(same_samples, "lead") == key(base, "lead")
    assert key(same_samples, "drive") == key(base, "drive")
    # Half the tempo alone moves the note-off.
    slower = chain(bpm=60)
    assert key(slower, "lead") != key(base, "lead")


def test_a_note_moved_by_less_than_a_sample_keeps_its_key() -> None:
    moved = chain(notes=(Note(0.00001, 60, 1, 100),))
    assert key(moved, "lead") == key(chain(), "lead")


def test_an_unresolvable_sound_fails_its_unit_and_everything_downstream_names_it() -> None:
    graph = chain(sound="fake:gone")

    lead = unit_of(graph, "lead")
    drive = unit_of(graph, "drive")
    assert isinstance(lead, Unplanned)
    assert (lead.status, lead.detail) == (SoundFailureStatus.UNKNOWN_SOUND, "no sound named 'gone'")
    assert isinstance(drive, Unplanned)
    assert (drive.status, drive.detail) == (
        RenderFailureStatus.UPSTREAM_FAILED,
        "upstream 'lead' did not render",
    )
    assert not isinstance(unit_of(graph, "pad"), Unplanned)


def test_resolution_failures_from_resolve_all_are_kept() -> None:
    graph = chain(sound="fake:gone")
    resolved = resolve_all(graph, instruments(), effects())
    assert list(resolved) == [node.name for node in graph.nodes]
    assert isinstance(resolved["lead"], SoundFailure)


def test_a_fractional_pitch_on_a_whole_pitch_instrument_is_unsupported_pitch() -> None:
    graph = chain(notes=(Note(0, 60.5, 1, 100),))

    lead = unit_of(graph, "lead")
    assert isinstance(lead, Unplanned)
    assert lead.status is RenderFailureStatus.UNSUPPORTED_PITCH
    assert isinstance(unit_of(graph, "lead", PitchSupport.CONTINUOUS), TrackUnit)


def test_a_track_unit_holds_the_performance_its_instrument_plays() -> None:
    lead = unit_of(chain(), "lead")
    assert isinstance(lead, TrackUnit)
    assert lead.input.performance == Performance(
        lead.input.performance.samples, (NoteOn(0, 60, 100), NoteOff(24000, 60))
    )
    assert lead.input.sound == lead.resolved.sound_key


def loop_graph() -> Graph:
    """A track feeds a sum that loops through a delay and a drive back into itself."""
    nodes: tuple[GraphNode, ...] = (
        TrackNode("track", sound="fake", notes=(Note(0, 60, 1, 100),)),
        EffectNode("loop", effect="sum"),
        DelayNode("late", delay_samples=BUFFER_SIZE),
        EffectNode("loop.drive", effect="drive"),
    )
    wires = (
        Wire("track", "loop", "audio"),
        Wire("loop", "late", "audio"),
        Wire("late", "loop.drive", "audio"),
        Wire("loop.drive", "loop", "audio", gain_db=-12),
    )
    return Graph(120, nodes, wires, "feedback")


def test_the_members_of_a_cycle_group_share_one_unit_and_key() -> None:
    planned = plan_of(loop_graph())
    places = [planned.place(name) for name in ("late", "loop", "loop.drive")]

    unit = places[0][0]
    assert isinstance(unit, GroupUnit)
    assert all(found is unit for found, _ in places)
    assert [index for _, index in places] == [0, 1, 2]
    assert unit.nodes == ("late", "loop", "loop.drive")
    feed = unit.input.inputs[0]
    assert feed.member == "loop"
    assert feed.ref.unit_key == key(loop_graph(), "track")


def test_a_cycle_group_whose_feed_fails_fails_whole_naming_the_feed() -> None:
    graph = loop_graph()
    nodes = (TrackNode("track", sound="fake:gone", notes=(Note(0, 60, 1, 100),)), *graph.nodes[1:])
    unit = unit_of(dataclasses.replace(graph, nodes=nodes), "loop")

    assert isinstance(unit, Unplanned)
    assert unit.nodes == ("late", "loop", "loop.drive")
    assert unit.failure_of(NodeName("loop")) == (
        RenderFailureStatus.UPSTREAM_FAILED,
        "upstream 'track' did not render",
    )
