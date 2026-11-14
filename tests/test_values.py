"""Core value types: each constructor accepts valid values and raises for every broken rule."""

import math
from collections.abc import Callable
from types import MappingProxyType

import numpy as np
import pytest

from poser.core.audio import NUM_CHANNELS, Audio, AudioFault, AudioProblem, make_audio
from poser.core.cache import Stamp, sound_key
from poser.core.checks import CheckStatus, check_status
from poser.core.jsonvalue import Json
from poser.core.names import Identifier, NodeName, SongId, Suffix, TrackName
from poser.core.performance import Control, NoteOff, NoteOn, Performance
from poser.core.report import (
    BANDS,
    AnalysisStatus,
    Defect,
    Levels,
    Measurements,
    MixReport,
    NodeReport,
    Report,
)
from poser.core.song import Note, Song, SongError, Track
from poser.core.sounds import SoundEntry, SoundRef, parse_sound_ref
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus, StemStatus

KEY = "0123456789abcdef" * 4
BANDS_DB = dict.fromkeys(BANDS, -20.0)


def _make[T](factory: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Call a constructor with values its annotations do not promise, as untyped data would."""
    return factory(*args, **kwargs)


def _bad(factory: Callable[..., object], *args: object, **kwargs: object) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 -- each rule has its own message.
        factory(*args, **kwargs)


def _measurements(**changes: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "peak_dbfs": -6.0,
        "integrated_lufs": -14.0,
        "clipped_samples": 0,
        "bands_db": dict(BANDS_DB),
        "bar_dbfs": [-10.0, -12.0],
    }
    return {**fields, **changes}


def _mix(**changes: object) -> dict[str, object]:
    fields: dict[str, object] = {
        **_measurements(),
        "path": "mix/mix.wav",
        "spectrogram": "mix/mix.png",
        "defects": (),
    }
    return {**fields, **changes}


def _node_report(**changes: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "name": "lead",
        "status": AnalysisStatus.ANALYZED,
        "node_key": KEY,
        "sound_key": KEY,
        "kind": "track",
        "upstream": (),
    }
    return {**fields, **changes}


def _stereo(frames: int = 4) -> np.ndarray:
    return np.zeros((NUM_CHANNELS, frames), dtype=np.float32)


# Names


def test_track_name() -> None:
    assert TrackName("lead_1-a") == "lead_1-a"
    for value in ("", "a b", "../x", "a/b", "a.b", True, 1):
        _bad(TrackName, value)


def test_node_name() -> None:
    """A node's name may carry dots, as a compiled chain instance's does; a track's may not."""
    for value in ("lead", "lead.drive", "lead.drive.2"):
        assert NodeName(value) == value
    assert isinstance(NodeName("lead.drive"), NodeName)
    assert isinstance(TrackName("lead"), NodeName)
    for value in ("", "a b", "../x", "a/b", "a[b]", ".lead", "lead.", "lead..drive", True, 1):
        _bad(NodeName, value)


def test_identifier() -> None:
    for value in ("Serum", "Serum2", "serum-2", "a_b", "0x"):
        assert Identifier(value) == value
    for value in ("", "-a", "_a", "Serum Presets", "a:b", True):
        _bad(Identifier, value)


def test_suffix() -> None:
    assert Suffix(".yaml") == ".yaml"
    assert Suffix(".mp3") == ".mp3"
    for value in (".WAV", "wav", ".", "", ".ya ml", ".tar.gz", True):
        _bad(Suffix, value)


def test_song_id() -> None:
    assert SongId("songs/demo.yaml") == "songs/demo.yaml"
    assert SongId("demo.py") == "demo.py"
    for value in ("", "/a", "a/../b", "a//b", "a\\b", "./a", "a/", "..", True):
        _bad(SongId, value)


# JSON


def test_json() -> None:
    assert Json('{"a":1}').value() == {"a": 1}
    assert Json("null").value() is None
    for text in ('{"b":1, "a":2}', '{"b":1,"a":2}', "NaN", "Infinity", "not json", "", 1, None):
        _bad(Json, text)


def test_json_of() -> None:
    assert Json.of({"b": [1.0], "a": None}).text == '{"a":null,"b":[1.0]}'
    assert Json.of(True).value() is True
    for value in ((1, 2), {1, 2}, {1: "a"}, math.nan, math.inf, [math.nan], object()):
        _bad(Json.of, value)


def test_json_value_is_a_new_copy() -> None:
    held = Json.of({"a": [1, 2]})
    first = held.value()
    assert isinstance(first, dict)
    first["a"] = []
    first["b"] = 3
    assert held.value() == {"a": [1, 2]}


# Sounds


def test_sound_ref() -> None:
    assert SoundRef(Identifier("serum")).name is None
    assert SoundRef(Identifier("serum"), "presets/Bass/X").name == "presets/Bass/X"
    _bad(SoundRef, "serum")  # A plain str is not an Identifier.
    _bad(SoundRef, Identifier("serum"), "")
    _bad(SoundRef, Identifier("serum"), 1)


def test_sound_entry() -> None:
    ref = SoundRef(Identifier("serum"))
    assert SoundEntry(ref, "Serum's default patch").ref == ref
    _bad(SoundEntry, ref, "")
    _bad(SoundEntry, ref, None)
    _bad(SoundEntry, "serum", "a description")


@pytest.mark.parametrize(
    ("text", "instrument", "name"),
    [
        ("serum", "serum", None),
        ("serum:presets/Bass/X [Y]", "serum", "presets/Bass/X [Y]"),
        ("serum:presets/LD: Two Parts", "serum", "presets/LD: Two Parts"),
        ("serum::x", "serum", ":x"),
    ],
)
def test_parse_sound_ref(text: str, instrument: str, name: str | None) -> None:
    ref = parse_sound_ref(text)
    assert ref == SoundRef(Identifier(instrument), name)
    assert isinstance(ref.instrument, Identifier)
    assert str(ref) == text


@pytest.mark.parametrize("text", ["", "serum:", ":x", "bad name", "-a:x", "a b:x"])
def test_parse_sound_ref_failures(text: str) -> None:
    _bad(parse_sound_ref, text)


def test_parse_sound_ref_refuses_non_text() -> None:
    _bad(parse_sound_ref, 1)


# Songs


def test_track_and_song() -> None:
    note = Note(0, 60, 1, 100)
    track = Track("lead", "serum:presets/Bass/X", (note,))
    assert Song(120, (track,)).tracks == (track,)
    for args in (
        ("bad name", "serum", (note,)),
        ("lead", "bad name", (note,)),
        ("lead", "serum:", (note,)),
        ("lead", "serum", (note,), True),
        ("lead", "serum", (note,), 13),
    ):
        with pytest.raises(SongError):
            _make(Track, *args)
    for bpm in (0, -1, True, math.nan):
        with pytest.raises(SongError):
            _make(Song, bpm, (track,))
    with pytest.raises(SongError):
        Song(120, (track, track))  # Duplicate names.
    with pytest.raises(TypeError):
        _make(Track, "lead", "serum", (note,), engine="pedalboard")


def _song(bpm: float, beat: float, length: float = 1) -> Song:
    return Song(bpm, (Track("lead", "serum", (Note(beat, 60, length, 100),)),))


@pytest.mark.parametrize(("bpm", "beat"), [(1e-300, 1e300), (60, 3600)])
def test_song_past_the_limit_is_refused(bpm: float, beat: float) -> None:
    with pytest.raises(SongError, match="must end within 3600 seconds"):
        _song(bpm, beat)


def test_song_ending_at_the_limit_builds() -> None:
    assert _song(60, 3599).bpm == 60


# Audio


def test_audio() -> None:
    assert Audio(_stereo()).frames == 4
    data = _stereo()
    data[0, 0] = math.nan
    for value in (
        [[0.0], [0.0]],
        np.zeros(4, dtype=np.float32),
        np.zeros((1, 4), dtype=np.float32),
        np.zeros((NUM_CHANNELS, 0), dtype=np.float32),
        np.zeros((NUM_CHANNELS, 4), dtype=np.float64),
        data,
    ):
        _bad(Audio, value)


def test_audio_owns_a_read_only_copy() -> None:
    data = _stereo()
    audio = Audio(data)
    data[0, 0] = 1.0
    assert audio.samples[0, 0] == 0.0
    samples = audio.samples
    with pytest.raises(ValueError):  # noqa: PT011 -- numpy's read-only error.
        samples[0, 0] = 1.0
    with pytest.raises(ValueError):  # noqa: PT011 -- numpy's read-only error.
        samples.setflags(write=True)
    x = _stereo()
    assert not np.shares_memory(Audio(x).samples, Audio(x).samples)


def test_make_audio_faults() -> None:
    nan = _stereo()
    nan[1, 2] = math.nan
    inf = _stereo()
    inf[0, 1] = math.inf
    cases = [
        ([[0.0] * 4, [0.0] * 4], AudioFault.SHAPE),
        (np.zeros((1, 4), dtype=np.float32), AudioFault.SHAPE),
        (np.zeros((NUM_CHANNELS, 4), dtype=np.float64), AudioFault.DTYPE),
        (_stereo(5), AudioFault.LENGTH),
        (nan, AudioFault.NON_FINITE),
        (inf, AudioFault.NON_FINITE),
    ]
    for data, fault in cases:
        problem = make_audio(data, 4)
        assert isinstance(problem, AudioProblem)
        assert problem.fault is fault
    assert isinstance(make_audio(_stereo(), 4), Audio)


# Performances


def test_note_off() -> None:
    assert NoteOff(0, 127).pitch == 127
    for args in ((-1, 60), (0, -1), (0, 128), (True, 60), (0, True), (0.0, 60)):
        _bad(NoteOff, *args)


def test_control() -> None:
    assert Control(5, 119, 127).value == 127
    for args in ((-1, 1, 0), (0, 120, 0), (0, -1, 0), (0, 1, 128), (0, 1, -1), (0, True, 0)):
        _bad(Control, *args)
    _bad(Control, 0, 1, True)


def test_note_on() -> None:
    assert NoteOn(0, 60, 1).velocity == 1
    for args in ((-1, 60, 100), (0, 128, 100), (0, 60, 0), (0, 60, 128), (0, 60, True)):
        _bad(NoteOn, *args)


def test_performance() -> None:
    events = (NoteOff(0, 60), Control(0, 1, 5), NoteOn(0, 60, 100), NoteOff(9, 60))
    assert Performance(10, events).events == events
    assert Performance(1, ()).samples == 1
    for samples in (0, True, -1):
        _bad(Performance, samples, ())
    _bad(Performance, 10, list(events))
    _bad(Performance, 10, ("event",))
    _bad(Performance, 9, events)  # An event at sample 9 is past 9 samples.
    _bad(Performance, 10, (NoteOn(1, 60, 100), NoteOff(0, 60)))
    _bad(Performance, 10, (NoteOn(0, 60, 100), NoteOff(0, 60)))
    _bad(Performance, 10, (Control(0, 1, 5), NoteOff(0, 60)))
    _bad(Performance, 10, (NoteOn(0, 60, 100), Control(0, 1, 5)))


# Cache


def test_stamp() -> None:
    assert Stamp(KEY, KEY, StemStatus.RENDERED, -3.0).peak_dbfs == -3.0
    assert Stamp(KEY, KEY, StemStatus.SILENT, -200).status is StemStatus.SILENT
    for key in (KEY.upper(), KEY[:-1], KEY + "0", "", 1):
        _bad(Stamp, key, KEY, StemStatus.RENDERED, 0.0)
        _bad(Stamp, KEY, key, StemStatus.RENDERED, 0.0)
    _bad(Stamp, KEY, KEY, "rendered", 0.0)
    _bad(Stamp, KEY, KEY, RenderFailureStatus.CRASHED, 0.0)
    for peak in (math.nan, math.inf, -math.inf, True, "0"):
        _bad(Stamp, KEY, KEY, StemStatus.RENDERED, peak)


def test_sound_key_needs_a_version() -> None:
    assert len(sound_key("pedalboard", "1", "state")) == 64
    _bad(sound_key, "pedalboard", "", "state")


# Report


def test_levels() -> None:
    levels = Levels(None, BANDS_DB)
    assert dict(levels.bands_db) == BANDS_DB
    _bad(Levels, math.nan, BANDS_DB)
    _bad(Levels, True, BANDS_DB)
    _bad(Levels, -14.0, {**BANDS_DB, "air": 0.0})
    _bad(Levels, -14.0, dict.fromkeys(list(BANDS)[1:], 0.0))
    _bad(Levels, -14.0, {**BANDS_DB, "sub": math.inf})
    _bad(Levels, -14.0, [0.0] * len(BANDS))


def test_measurements() -> None:
    measurements = _make(Measurements, **_measurements())
    assert measurements.bar_dbfs == (-10.0, -12.0)
    assert _make(Measurements, **_measurements(integrated_lufs=None)).integrated_lufs is None
    for changes in (
        {"peak_dbfs": math.inf},
        {"peak_dbfs": True},
        {"integrated_lufs": math.nan},
        {"clipped_samples": -1},
        {"clipped_samples": True},
        {"clipped_samples": 1.0},
        {"bands_db": {**BANDS_DB, "air": 0.0}},
        {"bands_db": {**BANDS_DB, "sub": math.nan}},
        {"bar_dbfs": [math.nan]},
        {"bar_dbfs": "loud"},
    ):
        _bad(Measurements, **_measurements(**changes))


def test_node_report() -> None:
    report = _make(NodeReport, **_node_report(defects=[Defect.SILENT]))
    assert report.defects == (Defect.SILENT,)
    for changes in (
        {"status": "analyzed"},
        {"kind": "magnet"},
        {"upstream": ("lead", 1)},
        {"upstream": "lead"},
        {"defects": (AnalysisStatus.ANALYZED,)},
        {"defects": ("silent",)},
        {"gain_db": math.nan},
        {"peak_dbfs": math.inf},
        {"clipped_samples": -1},
        {"bands_db": {"sub": 0.0}},
        {"bar_dbfs": [math.nan]},
    ):
        _bad(NodeReport, **_node_report(**changes))


def test_mix_report() -> None:
    mix = _make(MixReport, **_mix(defects=[Defect.CLIPPING]))
    assert mix.defects == (Defect.CLIPPING,)
    for changes in (
        {"defects": (AnalysisStatus.ANALYZED,)},
        {"peak_dbfs": math.nan},
        {"clipped_samples": -1},
        {"bands_db": {}},
        {"bar_dbfs": [math.inf]},
    ):
        _bad(MixReport, **_mix(**changes))


def test_report() -> None:
    node = _make(NodeReport, **_node_report())
    report = _make(Report, "demo", 1, KEY, [node], None)
    assert report.nodes == (node,)
    _bad(Report, "demo", 1, KEY, [node, "lead"], None)
    _bad(Report, "demo", 1, KEY, "lead", None)
    _bad(Report, "demo", 1, KEY, (node,), {"path": "mix.wav"})


def test_collections_are_immutable_copies() -> None:
    bands = dict(BANDS_DB)
    bars = [-10.0, -12.0]
    measurements = _make(Measurements, -6.0, -14.0, 0, bands, bars)
    bands["sub"] = 0.0
    bars.append(0.0)
    assert measurements.bands_db["sub"] == -20.0
    assert measurements.bar_dbfs == (-10.0, -12.0)
    assert isinstance(measurements.bar_dbfs, tuple)
    with pytest.raises(TypeError):
        measurements.bands_db["sub"] = 0.0  # ty: ignore[invalid-assignment]

    defects = [Defect.SILENT]
    node = _make(
        NodeReport,
        **_node_report(bands_db=bands, bar_dbfs=bars, defects=defects),
    )
    mix = _make(MixReport, **_mix(defects=defects))
    nodes = [node]
    report = _make(Report, "demo", 1, KEY, nodes, mix)
    snapshot = (dict(node.bands_db or {}), node.bar_dbfs)
    defects.append(Defect.CLIPPING)
    bands["bass"] = 0.0
    nodes.append(node)
    assert (dict(node.bands_db or {}), node.bar_dbfs) == snapshot
    assert node.defects == (Defect.SILENT,)
    assert mix.defects == (Defect.SILENT,)
    assert report.nodes == (node,)
    for value in (node, mix):
        assert isinstance(value.defects, tuple)
    assert isinstance(node.bands_db, MappingProxyType)
    assert isinstance(mix.bands_db, MappingProxyType)
    with pytest.raises(TypeError):
        mix.bands_db["sub"] = 0.0  # ty: ignore[invalid-assignment]


# Checks


CHECK_STATUS_TABLE = [
    (SoundFailureStatus.UNKNOWN_SOUND, CheckStatus.LOAD_FAILED),
    (SoundFailureStatus.LOAD_FAILED, CheckStatus.LOAD_FAILED),
    (RenderFailureStatus.LOAD_FAILED, CheckStatus.LOAD_FAILED),
    (RenderFailureStatus.CRASHED, CheckStatus.CRASHED),
    (RenderFailureStatus.TIMED_OUT, CheckStatus.TIMED_OUT),
    # A check has no upstream and no retry, so these cannot occur there; they map to a crash.
    (RenderFailureStatus.LATE_START, CheckStatus.CRASHED),
    (RenderFailureStatus.UNSUPPORTED_PITCH, CheckStatus.CRASHED),
    (RenderFailureStatus.UPSTREAM_FAILED, CheckStatus.CRASHED),
]


def test_check_status_maps_every_failure() -> None:
    members = [*SoundFailureStatus, *RenderFailureStatus]
    assert [(type(status), status) for status, _ in CHECK_STATUS_TABLE] == [
        (type(status), status) for status in members
    ]
    for status, expected in CHECK_STATUS_TABLE:
        assert check_status(status) is expected
