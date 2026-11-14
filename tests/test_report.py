"""Tests of the report schema, its JSON form, and the built-in analysis policy."""

import json
from dataclasses import replace

import numpy as np
import pytest

from poser.core.audio import SAMPLE_RATE
from poser.core.meter import Meter, MeterSpan
from poser.core.report import (
    ANALYSIS_STATUSES,
    BANDS,
    LEVEL_FLOOR_DBFS,
    AnalyzedNode,
    CompleteReport,
    Defect,
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
    report_failed,
    report_to_data,
    silent_defects,
    span_dbfs,
)
from poser.core.song import Note, Song, Track
from poser.core.statuses import SkipStatus, SoundFailureStatus, UnrenderedStatus

KEY = "0" * 64
SOUND = "1" * 64
SPANS = (LevelSpan(0, 4), LevelSpan(4, 8))
BANDS_DB = dict.fromkeys(BANDS, -30.0)


def measurements(peak_dbfs: float = -6.0, clipped_samples: int = 0) -> Measurements:
    return Measurements(
        peak_dbfs=peak_dbfs,
        integrated_lufs=-14.0,
        clipped_samples=clipped_samples,
        bands_db=BANDS_DB,
        span_dbfs=(-12.0, -13.0),
    )


MEASUREMENTS_DATA = {
    "peak_dbfs": -6.0,
    "integrated_lufs": -14.0,
    "clipped_samples": 0,
    "bands_db": BANDS_DB,
    "span_dbfs": [-12.0, -13.0],
}


def analyzed(**changes: object) -> AnalyzedNode:
    clean = AnalyzedNode(
        "lead", "track", (), -3.0, KEY, SOUND, measurements(), (), "images/node-0.png"
    )
    return replace(clean, **changes)


def measured(**changes: object) -> MeasuredNode:
    return replace(
        MeasuredNode("lead", "track", (), -3.0, KEY, SOUND, measurements(), ()), **changes
    )


def unrendered(**changes: object) -> UnrenderedNode:
    clean = UnrenderedNode(
        "pad", "track", (), 0.0, KEY, SOUND, UnrenderedStatus.NOT_RENDERED, "not rendered"
    )
    return replace(clean, **changes)


def unplanned(**changes: object) -> UnplannedNode:
    clean = UnplannedNode(
        "echo", "effect", ("lead",), 0.0, None, SoundFailureStatus.UNKNOWN_SOUND, "no recipe"
    )
    return replace(clean, **changes)


def mix(**changes: object) -> MixReport:
    clean = MixReport("audio/mix.wav", measurements(), "images/mix.png", ())
    return replace(clean, **changes)


def complete(
    nodes: tuple[AnalyzedNode, ...] = (), mix_report: MixReport | None = None
) -> CompleteReport:
    return CompleteReport("song", 1, KEY, SPANS, nodes or (analyzed(),), mix_report or mix())


def incomplete(
    nodes: tuple[MeasuredNode | UnrenderedNode | UnplannedNode, ...] = (),
) -> IncompleteReport:
    return IncompleteReport("song", 1, KEY, SPANS, nodes or (measured(), unrendered()))


@pytest.mark.parametrize(
    ("value", "failed"),
    [
        (complete(), False),
        (incomplete(), True),
        (incomplete((unplanned(),)), True),
        (complete((analyzed(defects=(Defect.SILENT,)),)), True),
        (complete(mix_report=mix(defects=(Defect.HEADROOM,))), True),
    ],
    ids=["clean", "incomplete", "unplanned", "node_defect", "mix_defect"],
)
def test_report_failed(value: Report, failed: bool) -> None:
    assert report_failed(value) is failed


def test_a_complete_report_is_json_in_the_documented_shape() -> None:
    value = complete(
        (analyzed(defects=(Defect.SILENT,)),),
        mix(defects=(Defect.CLIPPING,)),
    )

    data = report_to_data(value)

    assert data == {
        "status": "complete",
        "song": "song",
        "run": 1,
        "song_sha256": KEY,
        "spans": [{"beat": 0, "end": 4}, {"beat": 4, "end": 8}],
        "nodes": [
            {
                "name": "lead",
                "kind": "track",
                "upstream": [],
                "gain_db": -3.0,
                "status": "analyzed",
                "node_key": KEY,
                "sound_key": SOUND,
                "measurements": MEASUREMENTS_DATA,
                "defects": ["silent"],
                "spectrogram": "images/node-0.png",
            }
        ],
        "mix": {
            "path": "audio/mix.wav",
            "measurements": MEASUREMENTS_DATA,
            "spectrogram": "images/mix.png",
            "defects": ["clipping"],
        },
    }
    assert json.loads(json.dumps(data, allow_nan=False)) == data


def test_an_incomplete_report_is_json_with_each_node_type_and_no_mix() -> None:
    value = incomplete(
        (
            measured(),
            unrendered(status=UnrenderedStatus.UNREADABLE_STEM, detail="cannot read"),
            unplanned(),
            unplanned(
                name="tail",
                upstream=("echo",),
                sound_key=SOUND,
                status=SkipStatus.UPSTREAM_FAILED,
                detail="upstream 'echo' did not render",
            ),
        )
    )

    data = report_to_data(value)

    assert data == {
        "status": "incomplete",
        "song": "song",
        "run": 1,
        "song_sha256": KEY,
        "spans": [{"beat": 0, "end": 4}, {"beat": 4, "end": 8}],
        "nodes": [
            {
                "name": "lead",
                "kind": "track",
                "upstream": [],
                "gain_db": -3.0,
                "status": "measured",
                "node_key": KEY,
                "sound_key": SOUND,
                "measurements": MEASUREMENTS_DATA,
                "defects": [],
            },
            {
                "name": "pad",
                "kind": "track",
                "upstream": [],
                "gain_db": 0.0,
                "status": "unreadable_stem",
                "node_key": KEY,
                "sound_key": SOUND,
                "detail": "cannot read",
            },
            {
                "name": "echo",
                "kind": "effect",
                "upstream": ["lead"],
                "gain_db": 0.0,
                "status": "unknown_sound",
                "sound_key": None,
                "detail": "no recipe",
            },
            {
                "name": "tail",
                "kind": "effect",
                "upstream": ["echo"],
                "gain_db": 0.0,
                "status": "upstream_failed",
                "sound_key": SOUND,
                "detail": "upstream 'echo' did not render",
            },
        ],
    }


def test_an_incomplete_report_needs_a_node_that_is_not_measured() -> None:
    with pytest.raises(ValueError, match="not measured"):
        IncompleteReport("song", 1, KEY, SPANS, (measured(), measured(name="pad")))


def test_an_incomplete_report_refuses_analyzed_nodes() -> None:
    with pytest.raises(ValueError, match="IncompleteReport"):
        IncompleteReport("song", 1, KEY, SPANS, (analyzed(), unrendered()))  # ty: ignore[invalid-argument-type]


def test_a_complete_report_refuses_other_nodes() -> None:
    with pytest.raises(ValueError, match=r"CompleteReport\.nodes"):
        CompleteReport("song", 1, KEY, SPANS, (measured(),), mix())  # ty: ignore[invalid-argument-type]


def test_every_node_status_is_listed() -> None:
    assert ANALYSIS_STATUSES == (
        "analyzed",
        "measured",
        "not_rendered",
        "unreadable_stem",
        "unknown_sound",
        "load_failed",
        "unsupported_pitch",
        "upstream_failed",
    )


def test_changing_report_data_leaves_the_report_unchanged() -> None:
    value = complete()
    expected = report_to_data(value)
    data = report_to_data(value)
    assert isinstance(data, dict)
    nodes = data["nodes"]
    assert isinstance(nodes, list)
    node_data = nodes[0]
    assert isinstance(node_data, dict)
    values = node_data["measurements"]
    assert isinstance(values, dict)

    part = values["bands_db"]
    assert isinstance(part, dict)
    part.clear()
    part = values["span_dbfs"]
    assert isinstance(part, list)
    part.append(0.0)
    part = node_data["defects"]
    assert isinstance(part, list)
    part.append("silent")
    nodes.clear()

    assert report_to_data(value) == expected
    assert value.nodes[0].measurements.bands_db == BANDS_DB
    assert value.nodes[0].measurements.span_dbfs == (-12.0, -13.0)


def sine(frequency: float, seconds: float, amplitude: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    tone = (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)
    return np.stack([tone, tone])


def test_span_levels_are_the_rms_of_each_span() -> None:
    audio = np.zeros((2, 6 * SAMPLE_RATE), dtype=np.float32)
    audio[:, : 2 * SAMPLE_RATE] = sine(1000.0, 2.0)

    levels = span_dbfs(audio, [(0.0, 2.0), (2.0, 4.0)])

    assert len(levels) == 2
    assert levels[0] == pytest.approx(-3.01, abs=0.02)
    assert levels[1] == LEVEL_FLOOR_DBFS
    assert all(round(level, 2) == level for level in levels)


def test_span_levels_past_the_audio_are_at_the_floor() -> None:
    audio = np.full((2, SAMPLE_RATE), 0.5, dtype=np.float32)

    levels = span_dbfs(audio, [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)])

    assert levels == [pytest.approx(-6.02, abs=0.01), LEVEL_FLOOR_DBFS, LEVEL_FLOOR_DBFS]


@pytest.mark.parametrize(
    ("peak_dbfs", "defects"), [(-90.0, [Defect.SILENT]), (-60.0, []), (0.5, [])]
)
def test_track_is_silent_below_the_silence_threshold(
    peak_dbfs: float, defects: list[Defect]
) -> None:
    assert silent_defects(measurements(peak_dbfs)) == defects


@pytest.mark.parametrize(
    ("peak_dbfs", "clipped_samples", "defects"),
    [
        (-1.5, 0, []),
        (-0.5, 0, [Defect.HEADROOM]),
        (1.0, 96000, [Defect.CLIPPING, Defect.HEADROOM]),
        (-90.0, 0, [Defect.SILENT]),
    ],
)
def test_mix_defects_follow_the_ceiling_and_full_scale(
    peak_dbfs: float, clipped_samples: int, defects: list[Defect]
) -> None:
    assert mix_defects(measurements(peak_dbfs, clipped_samples)) == defects


@pytest.mark.parametrize(
    "changes",
    [{"song": 1}, {"run": True}, {"run": 0}, {"song_sha256": None}],
)
def test_report_refuses_bad_scalars(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="CompleteReport"):
        replace(complete(), **changes)
    with pytest.raises(ValueError, match="IncompleteReport"):
        replace(incomplete(), **changes)


def ending_at(last_beat: float, *spans: MeterSpan) -> Song:
    """Return a song whose one note ends at `last_beat`, with the given meter."""
    track = Track("lead", "fake", (Note(0, 60, last_beat, 100),))
    return Song(bpm=120, tracks=(track,), meter=Meter(spans))


def bounds(spans: tuple[LevelSpan, ...]) -> list[tuple[float, float, int | None]]:
    return [(span.beat, span.end, span.number) for span in spans]


def test_a_song_without_a_meter_has_unnumbered_spans_of_four_beats() -> None:
    assert bounds(level_spans(ending_at(10).graph())) == [
        (0, 4, None),
        (4, 8, None),
        (8, 10, None),
    ]


def test_a_metered_song_has_one_numbered_span_per_bar() -> None:
    assert bounds(level_spans(ending_at(21, MeterSpan(0, 7)).graph())) == [
        (0, 7, 1),
        (7, 14, 2),
        (14, 21, 3),
    ]


def test_an_unmetered_intro_is_cut_where_the_meter_starts() -> None:
    assert bounds(level_spans(ending_at(15, MeterSpan(6, 3)).graph())) == [
        (0, 4, None),
        (4, 6, None),
        (6, 9, 1),
        (9, 12, 2),
        (12, 15, 3),
    ]


def test_bar_numbers_continue_across_a_meter_change() -> None:
    song = ending_at(14, MeterSpan(0, 4, 8), MeterSpan(8, 3))
    assert bounds(level_spans(song.graph())) == [(0, 4, 1), (4, 8, 2), (8, 11, 3), (11, 14, 4)]


def test_unmetered_spans_count_from_their_own_stretch() -> None:
    song = ending_at(17, MeterSpan(0, 3, 6))
    assert bounds(level_spans(song.graph())) == [
        (0, 3, 1),
        (3, 6, 2),
        (6, 10, None),
        (10, 14, None),
        (14, 17, None),
    ]


@pytest.mark.parametrize("meter", [(), (MeterSpan(0, 4),)])
def test_a_last_beat_on_a_boundary_adds_no_empty_span(meter: tuple[MeterSpan, ...]) -> None:
    spans = level_spans(ending_at(8, *meter).graph())
    assert [(span.beat, span.end) for span in spans] == [(0, 4), (4, 8)]


def test_a_sliver_past_the_last_span_joins_it() -> None:
    assert bounds(level_spans(ending_at(16.0036).graph())) == [
        (0, 4, None),
        (4, 8, None),
        (8, 12, None),
        (12, 16.0036, None),
    ]
    metered = level_spans(ending_at(176.0036, MeterSpan(0, 4)).graph())
    assert len(metered) == 44
    assert bounds(metered)[-1] == (172, 176.0036, 44)


@pytest.mark.parametrize(
    ("beat", "end", "number"),
    [(4, 4, None), (-1, 4, None), (0, float("inf"), None), (0, 4, 0), (0, 4, 1.0), (0, 4, True)],
)
def test_a_level_span_refuses_bad_values(beat: float, end: float, number: object) -> None:
    with pytest.raises(ValueError, match="LevelSpan"):
        LevelSpan(beat, end, number)  # ty: ignore[invalid-argument-type]


def test_a_report_refuses_levels_that_do_not_match_its_spans() -> None:
    short = replace(measurements(), span_dbfs=(-12.0,))
    with pytest.raises(ValueError, match="span_dbfs"):
        complete(nodes=(analyzed(measurements=short),))
    with pytest.raises(ValueError, match="span_dbfs"):
        complete(mix_report=mix(measurements=short))
    with pytest.raises(ValueError, match="span_dbfs"):
        incomplete(nodes=(measured(measurements=short), unrendered()))
    with pytest.raises(ValueError, match="span_dbfs"):
        replace(complete(), spans=(*SPANS, LevelSpan(8, 12)))
    with pytest.raises(ValueError, match=r"CompleteReport\.spans"):
        replace(complete(), spans=((0, 4), (4, 8)))


def test_report_data_gives_spans_with_numbers_only_on_bars() -> None:
    spans = (LevelSpan(0, 4), LevelSpan(4, 7.5, 1))
    data = report_to_data(replace(complete(), spans=spans))
    assert isinstance(data, dict)
    assert list(data)[:5] == ["status", "song", "run", "song_sha256", "spans"]
    assert data["spans"] == [{"beat": 0, "end": 4}, {"beat": 4, "end": 7.5, "number": 1}]
    text = json.dumps(data)
    assert '"span_dbfs": [-12.0, -13.0]' in text
    assert "bar_dbfs" not in text
    assert "bar_dbfs" not in json.dumps(report_to_data(incomplete()))
