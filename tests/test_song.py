"""Tests for the song model. No plugins, files, or rendering."""

from collections.abc import Callable
from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from poser.core.graph import Graph, GraphError, Node, Wire
from poser.core.music import chord, notes, pitch, scale, swing
from poser.core.names import Identifier, TrackName
from poser.core.song import (
    ControlChange,
    Note,
    NoteData,
    Song,
    SongData,
    SongError,
    Track,
    parse_song,
    song_graph,
    song_to_data,
)
from poser.core.sounds import SoundRef


def valid_note() -> dict[str, object]:
    return {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}


def valid_track(name: str = "lead") -> dict[str, object]:
    return {"name": name, "sound": "serum", "notes": [valid_note()]}


def valid_song() -> dict[str, object]:
    return {"bpm": 120, "tracks": [valid_track()]}


def test_seconds_uses_bpm() -> None:
    assert parse_song({**valid_song(), "bpm": 90}).seconds(3) == pytest.approx(2.0)


def first_item(container: dict[str, object], key: str) -> dict[str, object]:
    """Return the first mapping in a list held by song data under construction."""
    items = container[key]
    assert isinstance(items, list)
    first = items[0]
    assert isinstance(first, dict)
    return first


def first_track(song: dict[str, object]) -> dict[str, object]:
    return first_item(song, "tracks")


def first_note(song: dict[str, object]) -> dict[str, object]:
    return first_item(first_track(song), "notes")


def set_bpm(song: dict[str, object], value: object) -> None:
    song["bpm"] = value


def set_track(song: dict[str, object], key: str, value: object) -> None:
    first_track(song)[key] = value


def set_note(song: dict[str, object], key: str, value: object) -> None:
    first_note(song)[key] = value


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda s: set_bpm(s, 0), "bpm"),
        (lambda s: set_bpm(s, -120), "bpm"),
        (lambda s: set_bpm(s, "fast"), "bpm"),
        (lambda s: set_bpm(s, True), "bpm"),
        (lambda s: set_bpm(s, float("inf")), "bpm"),
        (lambda s: s.update(tracks=[]), "at least one track"),
        (lambda s: s.update(tracks="lead"), "at least one track"),
        (lambda s: set_track(s, "notes", []), "at least one note"),
        (lambda s: s.update(tracks=[valid_track(), valid_track()]), "duplicate track name 'lead'"),
        (lambda s: s.update(tracks=[valid_track(), valid_track("LEAD")]), "duplicate track name"),
        (lambda s: set_track(s, "name", "has space"), "tracks[0].name"),
        (lambda s: set_track(s, "name", "../escape"), "tracks[0].name"),
        (lambda s: set_track(s, "name", "line\n"), "tracks[0].name"),
        (lambda s: set_track(s, "name", 7), "tracks[0].name"),
        (lambda s: set_track(s, "engine", "pedalboard"), "tracks[0]: unknown key 'engine'"),
        (lambda s: set_track(s, "sound", "bad name"), "tracks[0].sound: must be <instrument>"),
        (lambda s: set_track(s, "sound", "serum:"), "tracks[0].sound"),
        (lambda s: set_track(s, "sound", ":preset"), "tracks[0].sound"),
        (lambda s: set_track(s, "sound", ""), "tracks[0].sound"),
        (lambda s: set_note(s, "pitch", -1), "notes[0].pitch"),
        (lambda s: set_note(s, "pitch", 128), "notes[0].pitch"),
        (lambda s: set_note(s, "pitch", 127.5), "notes[0].pitch"),
        (lambda s: set_note(s, "velocity", 0), "notes[0].velocity"),
        (lambda s: set_note(s, "velocity", 128), "notes[0].velocity"),
        (lambda s: set_note(s, "beat", -1), "notes[0].beat"),
        (lambda s: set_note(s, "beat", float("nan")), "notes[0].beat"),
        (lambda s: set_note(s, "length", 0), "notes[0].length"),
        (lambda s: set_note(s, "length", -0.5), "notes[0].length"),
        (lambda s: s.update(tempo=120), "unknown key 'tempo'"),
        (lambda s: set_track(s, "pan", 1), "tracks[0]: unknown key 'pan'"),
        (lambda s: set_note(s, "vel", 1), "notes[0]: unknown key 'vel'"),
        (lambda s: s.pop("bpm"), "missing key 'bpm'"),
        (lambda s: first_track(s).pop("sound"), "tracks[0]: missing key 'sound'"),
        (lambda s: first_note(s).pop("pitch"), "notes[0]: missing key 'pitch'"),
    ],
)
def test_invalid_song_names_the_problem(
    change: Callable[[dict[str, object]], object], expected: str
) -> None:
    song = valid_song()
    change(song)

    with pytest.raises(SongError) as raised:
        parse_song(song)

    assert expected in str(raised.value)


@pytest.mark.parametrize("data", [None, [], "song", 3])
def test_song_that_is_not_a_mapping_is_rejected(data: object) -> None:
    with pytest.raises(SongError, match="must be a mapping"):
        parse_song(data)


def test_each_problem_gets_its_own_message() -> None:
    song = valid_song()
    set_bpm(song, 0)
    set_note(song, "pitch", 200)
    set_note(song, "velocity", 0)

    with pytest.raises(SongError) as raised:
        parse_song(song)

    assert len(raised.value.problems) == 3


def helper_song() -> Song:
    progression = chord(pitch("A3"), "min") + chord(pitch("F3"), "maj", 1)
    lead = swing(notes(scale(pitch("A4"), "minor"), 0, 0.5, 90, step=0.5), 2 / 3)
    return Song(
        bpm=96.5,
        tracks=(
            Track("lead", "serum:presets/Leads/LD A Bit of Luck [SD]", lead),
            Track("pad", "serum", notes(progression, 0, 4, 70)),
        ),
    )


def test_song_to_data_round_trips_through_parse_song() -> None:
    song = helper_song()
    assert parse_song(song_to_data(song)) == song


def test_track_without_gain_has_gain_zero() -> None:
    assert parse_song(valid_song()).tracks[0].gain == 0.0


@pytest.mark.parametrize("gain", [-60, 12, -6.5, 3])
def test_gain_in_range_parses(gain: float) -> None:
    song = valid_song()
    set_track(song, "gain", gain)
    assert parse_song(song).tracks[0].gain == gain


@pytest.mark.parametrize("gain", [-60.5, 12.5, True, "loud", float("nan"), float("inf")])
def test_bad_gain_is_reported_with_other_problems(gain: object) -> None:
    song = valid_song()
    set_track(song, "gain", gain)
    set_bpm(song, 0)

    with pytest.raises(SongError) as caught:
        parse_song(song)

    assert any(problem.startswith("tracks[0].gain: ") for problem in caught.value.problems)
    assert any(problem.startswith("bpm: ") for problem in caught.value.problems)


def test_bad_gain_message_names_the_range_and_value() -> None:
    song = valid_song()
    set_track(song, "gain", "loud")
    with pytest.raises(SongError) as caught:
        parse_song(song)
    assert caught.value.problems == [
        "tracks[0].gain: must be a number of decibels from -60 to 12, got 'loud'"
    ]


def test_integer_too_large_for_a_float_is_a_song_error() -> None:
    song = valid_song()
    set_track(song, "gain", 10**400)
    set_bpm(song, 10**400)
    with pytest.raises(SongError) as caught:
        parse_song(song)
    assert [problem.split(":")[0] for problem in caught.value.problems] == ["bpm", "tracks[0].gain"]


def test_integer_gain_is_stored_as_a_float() -> None:
    song = valid_song()
    set_track(song, "gain", -6)
    gain = parse_song(song).tracks[0].gain
    assert gain == -6.0
    assert type(gain) is float


def gain_song() -> Song:
    note = (Note(beat=0, pitch=60, length=1, velocity=100),)
    return Song(
        bpm=120,
        tracks=(
            Track("lead", "serum", note, gain=-6),
            Track("bass", "serum", note),
        ),
    )


def test_song_to_data_writes_gain_between_sound_and_notes_only_when_set() -> None:
    lead, bass = song_to_data(gain_song())["tracks"]
    assert list(lead) == ["name", "sound", "gain", "notes"]
    assert lead["gain"] == -6
    assert list(bass) == ["name", "sound", "notes"]


def test_song_to_data_is_the_song_format() -> None:
    note: NoteData = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}
    expected: SongData = {
        "bpm": 120,
        "tracks": [
            {
                "name": "lead",
                "sound": "serum",
                "gain": -6.0,
                "notes": [note],
            },
            {"name": "bass", "sound": "serum", "notes": [note]},
        ],
    }

    data = song_to_data(gain_song())

    assert data == expected
    assert list(data) == ["bpm", "tracks"]
    assert [list(track) for track in data["tracks"]] == [
        ["name", "sound", "gain", "notes"],
        ["name", "sound", "notes"],
    ]
    assert [list(note) for track in data["tracks"] for note in track["notes"]] == [
        ["beat", "pitch", "length", "velocity"]
    ] * 2


# Controller changes


def cc_song(*changes: object) -> dict[str, object]:
    song = valid_song()
    first_track(song)["cc"] = list(changes)
    return song


def cc_problems(*changes: object) -> list[str]:
    song = cc_song(*changes)
    set_bpm(song, -1)  # Another problem, so controller problems are reported alongside it.
    with pytest.raises(SongError) as error:
        parse_song(song)
    assert "bpm: must be a positive number, got -1" in error.value.problems
    return [p for p in error.value.problems if not p.startswith("bpm")]


def test_controller_changes_parse_in_listed_order_with_step_by_default() -> None:
    song = parse_song(
        cc_song(
            {"beat": 0, "control": 1, "value": 0, "shape": "linear"},
            {"beat": 2, "control": 74, "value": 90},
            {"beat": 2, "control": 1, "value": 127},
        )
    )

    assert song.tracks[0].cc == (
        ControlChange(0, 1, 0, "linear"),
        ControlChange(2, 74, 90, "step"),
        ControlChange(2, 1, 127, "step"),
    )


def test_empty_cc_equals_no_cc() -> None:
    assert parse_song(cc_song()).tracks[0].cc == ()
    assert parse_song(cc_song()) == parse_song(valid_song())


def good_cc(**changes: object) -> dict[str, object]:
    return {"beat": 0, "control": 1, "value": 0, **changes}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (["x"], "tracks[0].cc[0]: must be a mapping, got 'x'"),
        ([good_cc(depth=1)], "tracks[0].cc[0]: unknown key 'depth'"),
        ([{"beat": 0, "control": 1}], "tracks[0].cc[0]: missing key 'value'"),
        ([good_cc(beat=-1)], "tracks[0].cc[0].beat: must be a number that is not negative, got -1"),
        (
            [good_cc(beat="1")],
            "tracks[0].cc[0].beat: must be a number that is not negative, got '1'",
        ),
        (
            [good_cc(beat=4), good_cc(beat=2)],
            "tracks[0].cc[1].beat: must not be earlier than the previous change at beat 4, got 2",
        ),
        (
            [good_cc(control=-1)],
            "tracks[0].cc[0].control: must be an integer from 0 to 119, got -1",
        ),
        (
            [good_cc(control=120)],
            "tracks[0].cc[0].control: must be an integer from 0 to 119, got 120",
        ),
        (
            [good_cc(control=1.0)],
            "tracks[0].cc[0].control: must be an integer from 0 to 119, got 1.0",
        ),
        (
            [good_cc(control=True)],
            "tracks[0].cc[0].control: must be an integer from 0 to 119, got True",
        ),
        ([good_cc(value=-1)], "tracks[0].cc[0].value: must be an integer from 0 to 127, got -1"),
        ([good_cc(value=128)], "tracks[0].cc[0].value: must be an integer from 0 to 127, got 128"),
        ([good_cc(value=1.0)], "tracks[0].cc[0].value: must be an integer from 0 to 127, got 1.0"),
        ([good_cc(shape="curve")], "tracks[0].cc[0].shape: must be step or linear, got 'curve'"),
        (
            [good_cc(shape="linear"), good_cc(beat=1, control=74)],
            "tracks[0].cc[0].shape: linear needs a later change to control 1",
        ),
        (
            [good_cc(shape="linear"), good_cc(value=9)],
            "tracks[0].cc[0].shape: linear needs a later change to control 1",
        ),
    ],
)
def test_bad_controller_change_is_reported_with_other_problems(
    changes: list[object], message: str
) -> None:
    assert cc_problems(*changes) == [message]


def test_cc_that_is_not_a_list_is_reported_with_other_problems() -> None:
    song = valid_song()
    first_track(song)["cc"] = "x"
    set_bpm(song, -1)
    with pytest.raises(SongError) as error:
        parse_song(song)
    assert error.value.problems == [
        "bpm: must be a positive number, got -1",
        "tracks[0].cc: must be a list of controller changes, got 'x'",
    ]


def test_bad_ramp_target_reports_only_its_own_problem() -> None:
    problems = cc_problems(good_cc(shape="linear"), good_cc(beat=1, value=200))

    assert problems == ["tracks[0].cc[1].value: must be an integer from 0 to 127, got 200"]


def cc_track_song() -> Song:
    note = Note(0, 60, 1, 100)
    changes = (ControlChange(0, 1, 0, "linear"), ControlChange(2, 1, 127))
    return Song(
        120,
        (
            Track("lead", "serum", (note,), gain=-6.0, cc=changes),
            Track("bass", "serum", (note,)),
        ),
    )


def test_song_to_data_writes_cc_after_notes_and_shape_only_for_linear() -> None:
    data = song_to_data(cc_track_song())

    lead, bass = data["tracks"]
    assert list(lead) == ["name", "sound", "gain", "notes", "cc"]
    assert list(bass) == ["name", "sound", "notes"]
    assert lead.get("cc") == [
        {"beat": 0, "control": 1, "value": 0, "shape": "linear"},
        {"beat": 2, "control": 1, "value": 127},
    ]


def construct[T](cls: type[T], **values: object) -> T:
    """Call a song class with values of any type, as a script without a type checker may."""
    return cast("Callable[..., T]", cls)(**values)


def make_track(**changes: object) -> Track:
    values: dict[str, object] = {
        "name": "lead",
        "sound": "serum",
        "notes": (Note(0, 60, 1, 100),),
    }
    return construct(Track, **(values | changes))


NOTE: dict[str, object] = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}
CHANGE: dict[str, object] = {"beat": 0, "control": 1, "value": 0}


def make_note(**changes: object) -> Note:
    return construct(Note, **(NOTE | changes))


def make_change(**changes: object) -> ControlChange:
    return construct(ControlChange, **(CHANGE | changes))


def make_song(bpm: object, tracks: object) -> Song:
    return construct(Song, bpm=bpm, tracks=tracks)


@pytest.mark.parametrize(
    ("build", "location"),
    [
        (lambda: make_note(beat=-1), "Note.beat:"),
        (lambda: make_note(beat="0"), "Note.beat:"),
        (lambda: make_note(pitch=128), "Note.pitch:"),
        (lambda: make_note(pitch=-0.5), "Note.pitch:"),
        (lambda: make_note(length=0), "Note.length:"),
        (lambda: make_note(velocity=0), "Note.velocity:"),
        (lambda: make_note(velocity=True), "Note.velocity:"),
        (lambda: make_change(beat=-0.5), "ControlChange.beat:"),
        (lambda: make_change(control=120), "ControlChange.control:"),
        (lambda: make_change(value=128), "ControlChange.value:"),
        (lambda: make_change(shape="curve"), "ControlChange.shape:"),
        (lambda: make_track(name="../victim"), "Track.name:"),
        (lambda: make_track(name=""), "Track.name:"),
        (lambda: make_track(sound="bad name"), "Track.sound:"),
        (lambda: make_track(sound=""), "Track.sound:"),
        (lambda: make_track(gain=13), "Track.gain:"),
        (lambda: make_track(gain="0"), "Track.gain:"),
        (lambda: make_track(notes=()), "Track.notes:"),
        (lambda: make_track(notes=None), "Track.notes:"),
        (lambda: make_track(notes=((0, 60, 1, 100),)), "Track.notes[0]:"),
        (lambda: make_track(cc=None), "Track.cc:"),
        (lambda: make_track(cc=({"beat": 0},)), "Track.cc[0]:"),
        (
            lambda: make_track(cc=(ControlChange(2, 1, 0), ControlChange(1, 1, 0))),
            "Track.cc[1].beat:",
        ),
        (lambda: make_track(cc=(ControlChange(0, 1, 0, "linear"),)), "Track.cc[0].shape:"),
        (lambda: make_song(0, (make_track(),)), "Song.bpm:"),
        (lambda: make_song(120, ()), "Song.tracks:"),
        (lambda: make_song(120, None), "Song.tracks:"),
        (lambda: make_song(120, ("lead",)), "Song.tracks[0]:"),
        (lambda: make_song(120, (make_track(), make_track())), "Song.tracks:"),
    ],
)
def test_invalid_object_raises_with_its_class_location(
    build: Callable[[], object], location: str
) -> None:
    with pytest.raises(SongError) as error:
        build()

    assert len(error.value.problems) == 1, error.value.problems
    assert error.value.problems[0].startswith(location), error.value.problems


def test_note_reports_every_problem_at_once() -> None:
    with pytest.raises(SongError) as error:
        Note(beat=-1, pitch=200, length=0, velocity=0)

    assert [p.split(":")[0] for p in error.value.problems] == [
        "Note.beat",
        "Note.pitch",
        "Note.length",
        "Note.velocity",
    ]


def test_track_name_cannot_leave_the_render_directory() -> None:
    with pytest.raises(SongError) as error:
        Track(name="../victim", sound="x", notes=(Note(0, 60, 1, 100),))

    assert error.value.problems == [
        "Track.name: must contain only letters, digits, '_', and '-', got '../victim'"
    ]


def test_sound_that_is_not_a_reference_is_a_song_error() -> None:
    expected = (
        "must be <instrument> or <instrument>:<name>, with an instrument name matching "
        "[A-Za-z0-9][A-Za-z0-9_-]*, got 'bad name'"
    )
    data = valid_song()
    set_track(data, "sound", "bad name")
    with pytest.raises(SongError) as parsed:
        parse_song(data)
    with pytest.raises(SongError) as built:
        make_track(sound="bad name")

    assert parsed.value.problems == [f"tracks[0].sound: {expected}"]
    assert built.value.problems == [f"Track.sound: {expected}"]


@pytest.mark.parametrize(
    ("sound", "ref"),
    [
        ("serum", SoundRef(Identifier("serum"))),
        (
            "serum:presets/Leads/LD A Bit of Luck [SD]",
            SoundRef(Identifier("serum"), "presets/Leads/LD A Bit of Luck [SD]"),
        ),
        ("serum-2:a:b", SoundRef(Identifier("serum-2"), "a:b")),
    ],
)
def test_track_sound_ref_and_track_name_are_the_parsed_values(sound: str, ref: SoundRef) -> None:
    track = make_track(name="lead_1", sound=sound)

    assert track.sound_ref == ref
    assert track.track_name == TrackName("lead_1")
    assert isinstance(track.track_name, TrackName)


def test_track_has_no_engine() -> None:
    with pytest.raises(TypeError):
        make_track(engine="pedalboard")


def test_song_without_tracks_is_a_song_error() -> None:
    with pytest.raises(SongError) as error:
        Song(bpm=120, tracks=())

    assert error.value.problems == ["Song.tracks: must list at least one track"]


def test_track_names_differing_in_case_are_duplicates() -> None:
    with pytest.raises(SongError) as error:
        Song(120, (make_track(name="lead"), make_track(name="Lead")))

    assert error.value.problems == ["Song.tracks: duplicate track name 'Lead'"]


def test_linear_change_without_a_later_target_is_a_song_error() -> None:
    with pytest.raises(SongError) as error:
        make_track(cc=(ControlChange(0, 1, 0, "linear"),))

    assert error.value.problems == ["Track.cc[0].shape: linear needs a later change to control 1"]


def test_lists_are_stored_as_tuples_and_gain_as_a_float() -> None:
    note = Note(0, 60, 1, 100)
    from_lists = make_track(notes=[note], cc=[])
    from_tuples = make_track(notes=(note,), cc=())

    assert from_lists == from_tuples
    assert isinstance(from_lists.notes, tuple)
    assert isinstance(from_lists.cc, tuple)
    gain = make_track(gain=-3).gain
    assert gain == -3.0
    assert isinstance(gain, float)
    assert isinstance(make_song(120, [from_lists]).tracks, tuple)


def test_generator_of_notes_is_a_song_error() -> None:
    with pytest.raises(SongError) as error:
        make_track(notes=(n for n in (Note(0, 60, 1, 100),)))

    assert [p.split(":")[0] for p in error.value.problems] == ["Track.notes"]


def test_song_graph_compiles_and_is_frozen() -> None:
    song = helper_song()
    graph = song.graph()

    assert [node.name for node in graph.nodes] == ["lead", "pad"]
    assert graph.bpm == song.bpm
    assert graph.wires == ()
    node = graph.nodes[0]
    assert node.gain_db == song.tracks[0].gain
    assert node.notes == song.tracks[0].notes
    # No mutable state is aliased: the graph holds tuples, and the node refuses assignment.
    assert isinstance(graph.nodes, tuple)
    assert isinstance(node.notes, tuple)
    with pytest.raises(FrozenInstanceError):
        node.gain_db = 3.0  # ty: ignore[invalid-assignment]
    with pytest.raises(FrozenInstanceError):
        graph.nodes = ()  # ty: ignore[invalid-assignment]


# Effect chains (C6, C7, C10): a track's `effects` compiles to named chain nodes.


def chain_song(*effects: str, name: str = "bass", gain: float = -6.0) -> Song:
    return Song(120, (make_track(name=name, gain=gain, effects=effects),))


def test_effects_round_trip_through_song_to_data_and_parse() -> None:
    song = chain_song("drive", "crush")

    data = song_to_data(song)
    parsed = parse_song(data)

    assert parsed == song
    assert data["tracks"][0]["effects"] == ["drive", "crush"]
    assert list(data["tracks"][0]) == ["name", "sound", "effects", "gain", "notes"]


def test_empty_or_absent_effects_mean_the_same_song() -> None:
    assert make_track().effects == ()
    data = valid_song()
    first_track(data)["effects"] = []
    parsed = parse_song(data)
    assert parsed.tracks[0].effects == ()
    assert parsed == parse_song(valid_song())
    assert "effects" not in song_to_data(parsed)["tracks"][0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.update(effects="drive"), "effects: must be a list of effect recipe names"),
        (lambda t: t.update(effects=[1]), "effects[0]: must be non-empty text, got 1"),
        (lambda t: t.update(effects=[""]), "effects[0]: must be non-empty text, got ''"),
    ],
    ids=["not-a-list", "number", "blank"],
)
def test_bad_effects_are_reported_with_other_problems(
    change: Callable[[dict[str, object]], object], message: str
) -> None:
    song = valid_song()
    change(first_track(song))
    set_bpm(song, -1)

    with pytest.raises(SongError) as raised:
        parse_song(song)

    assert any(message in p for p in raised.value.problems)
    assert any(p.startswith("bpm") for p in raised.value.problems)


def test_an_unknown_recipe_name_is_a_compile_problem_naming_it() -> None:
    with pytest.raises(SongError) as raised:
        chain_song("drive").graph(("crush",))

    assert raised.value.problems == ["tracks[0]: no effect recipe 'drive' in poser.toml"]
    # Without the configured names, every recipe is unknown, as a caller without the mapping.
    with pytest.raises(SongError):
        chain_song("drive").graph()


def test_a_chain_compiles_in_series_with_the_fader_on_its_leaf() -> None:
    graph = chain_song("drive", "crush").graph(("drive", "crush"))

    assert [node.name for node in graph.nodes] == ["bass", "bass.drive", "bass.crush"]
    assert [node.kind for node in graph.nodes] == ["track", "effect", "effect"]
    assert [node.gain_db for node in graph.nodes] == [0.0, 0.0, -6.0]
    assert [(w.from_name, w.to_name, w.kind, w.gain_db) for w in graph.wires] == [
        ("bass", "bass.drive", "audio", 0.0),
        ("bass.drive", "bass.crush", "audio", 0.0),
    ]
    (track_node, drive, crush) = graph.nodes
    assert track_node.sound == "serum"
    assert (drive.effect, crush.effect) == ("drive", "crush")


def test_a_track_without_effects_compiles_as_before() -> None:
    song = chain_song()
    graph = song.graph(("drive",))

    assert [node.name for node in graph.nodes] == ["bass"]
    assert graph.wires == ()
    assert graph.nodes[0].gain_db == -6.0


def test_repeated_recipe_instances_take_ordinals() -> None:
    graph = chain_song("drive", "crush", "drive").graph(("drive", "crush"))

    assert [node.name for node in graph.nodes] == [
        "bass",
        "bass.drive",
        "bass.crush",
        "bass.drive.2",
    ]
    assert [(w.from_name, w.to_name) for w in graph.wires] == [
        ("bass", "bass.drive"),
        ("bass.drive", "bass.crush"),
        ("bass.crush", "bass.drive.2"),
    ]


def test_two_tracks_chains_stay_distinct_and_the_compile_is_deterministic() -> None:
    song = Song(
        120,
        (make_track(name="a", effects=("drive",)), make_track(name="b", effects=("drive",))),
    )

    graph = song.graph(("drive",))
    again = song.graph(("drive",))

    assert [node.name for node in graph.nodes] == ["a", "a.drive", "b", "b.drive"]
    assert graph == again


def graph_song() -> Graph:
    """A direct-frontend graph: a lead into one drive, as its author named every node."""
    lead = Node("lead", "track", sound="serum", notes=(Note(0, 60, 1, 100),))
    drive = Node("lead.drive", "effect", effect="drive")
    return Graph(120, (lead, drive), (Wire("lead", "lead.drive", "audio"),))


def test_song_graph_hands_a_graph_back_and_resolves_its_effect_references() -> None:
    """C14: a Graph's recipes resolve against `effects` exactly as a compiled song's do."""
    graph = graph_song()
    assert song_graph(graph, ("drive",)) is graph

    with pytest.raises(SongError) as raised:
        song_graph(graph, ())

    assert raised.value.problems == ["nodes[1]: no effect recipe 'drive' in poser.toml"]


def test_song_graph_compiles_a_song_as_its_method_does() -> None:
    song = chain_song("drive")

    assert song_graph(song, ("drive",)) == song.graph(("drive",))


# Pitch is a number on the MIDI scale, and a track can be unpitched.


@pytest.mark.parametrize("value", [0, 127, 60.5, 60.25])
def test_a_pitch_is_any_number_from_0_to_127(value: float) -> None:
    song = valid_song()
    set_note(song, "pitch", value)

    assert parse_song(song).tracks[0].notes[0].pitch == value


@pytest.mark.parametrize("value", [-0.5, 127.5, float("nan"), float("inf"), True, "60"])
def test_a_pitch_outside_the_numbers_from_0_to_127_is_rejected(value: object) -> None:
    song = valid_song()
    set_note(song, "pitch", value)

    with pytest.raises(SongError) as error:
        parse_song(song)

    assert any(
        ".pitch" in problem and "a number from 0 to 127" in problem
        for problem in error.value.problems
    )


def test_a_yaml_shaped_fractional_note_reaches_the_song() -> None:
    data = {
        "bpm": 120,
        "tracks": [
            {
                "name": "lead",
                "sound": "serum",
                "notes": [{"beat": 0, "pitch": 60.5, "length": 1, "velocity": 100}],
            }
        ],
    }

    assert parse_song(data).tracks[0].notes == (Note(beat=0, pitch=60.5, length=1, velocity=100),)


def test_an_integral_pitch_is_stored_as_an_int() -> None:
    note = Note(beat=0, pitch=60.0, length=1, velocity=100)

    assert type(note.pitch) is int
    assert note.pitch == 60
    assert note == Note(beat=0, pitch=60, length=1, velocity=100)
    assert type(Note(beat=0, pitch=60.5, length=1, velocity=100).pitch) is float


def test_the_record_keeps_a_fractional_pitch_and_writes_an_integral_one_as_an_int() -> None:
    notes = (
        Note(beat=0, pitch=60.5, length=1, velocity=100),
        Note(beat=1, pitch=60.0, length=1, velocity=100),
    )
    song = Song(bpm=120, tracks=(Track("lead", "serum", notes),))

    data = song_to_data(song)
    pitches = [note["pitch"] for note in data["tracks"][0]["notes"]]

    assert pitches == [60.5, 60]
    assert [type(value) for value in pitches] == [float, int]
    assert parse_song(data) == song


@pytest.mark.parametrize("value", [True, False])
def test_pitched_parses_as_a_bool(value: bool) -> None:
    song = valid_song()
    set_track(song, "pitched", value)

    assert parse_song(song).tracks[0].pitched is value


@pytest.mark.parametrize("value", ["false", 0, 1, None])
def test_pitched_rejects_a_non_bool(value: object) -> None:
    song = valid_song()
    set_track(song, "pitched", value)

    with pytest.raises(SongError, match=r"tracks\[0\]\.pitched: must be true or false"):
        parse_song(song)


def test_an_unpitched_track_rejects_a_fractional_pitch_naming_the_note() -> None:
    song = valid_song()
    set_track(song, "pitched", False)
    track = first_track(song)
    track["notes"] = [valid_note(), {**valid_note(), "pitch": 60.5}]

    with pytest.raises(SongError) as error:
        parse_song(song)

    assert error.value.problems == [
        (
            "tracks[0].notes[1].pitch: an unpitched track's pitches are key numbers and must be "
            "whole, got 60.5"
        )
    ]
    with pytest.raises(SongError, match=r"Track\.notes\[0\]\.pitch: an unpitched track"):
        Track("kit", "drums", (Note(0, 60.5, 1, 100),), pitched=False)


def test_the_record_writes_pitched_only_when_false() -> None:
    note = (Note(0, 36, 1, 100),)
    pitched = Song(bpm=120, tracks=(Track("lead", "serum", note),))
    unpitched = Song(bpm=120, tracks=(Track("kit", "drums", note, pitched=False),))

    assert "pitched" not in song_to_data(pitched)["tracks"][0]
    assert song_to_data(unpitched)["tracks"][0].get("pitched") is False
    assert parse_song(song_to_data(unpitched)) == unpitched


def test_a_compiled_graph_carries_pitched_on_the_track_node() -> None:
    note = (Note(0, 36, 1, 100),)
    song = Song(
        bpm=120,
        tracks=(Track("lead", "serum", note), Track("kit", "drums", note, pitched=False)),
    )

    graph = song.graph()

    assert [(node.name, node.pitched) for node in graph.nodes] == [("lead", True), ("kit", False)]
    assert graph.to_data()["nodes"][1].get("pitched") is False
    assert "pitched" not in graph.to_data()["nodes"][0]


def test_only_a_track_node_can_be_unpitched() -> None:
    with pytest.raises(GraphError, match=r"Node\.pitched: only a track node can be unpitched"):
        Node("wet", "effect", effect="reverb", pitched=False)
