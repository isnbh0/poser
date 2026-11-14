"""Tests of the report schema, its JSON form, and the built-in analysis policy."""

import json
from dataclasses import replace

import numpy as np
import pytest

from poser.core.audio import SAMPLE_RATE
from poser.core.report import (
    ANALYSIS_STATUSES,
    BANDS,
    LEVEL_FLOOR_DBFS,
    AnalyzedNode,
    CompleteReport,
    Defect,
    IncompleteReport,
    MeasuredNode,
    Measurements,
    MixReport,
    Report,
    UnplannedNode,
    UnrenderedNode,
    bar_dbfs,
    mix_defects,
    report_failed,
    report_to_data,
    silent_defects,
)
from poser.core.statuses import SkipStatus, SoundFailureStatus, UnrenderedStatus

KEY = "0" * 64
SOUND = "1" * 64
BANDS_DB = dict.fromkeys(BANDS, -30.0)


def measurements(peak_dbfs: float = -6.0, clipped_samples: int = 0) -> Measurements:
    return Measurements(
        peak_dbfs=peak_dbfs,
        integrated_lufs=-14.0,
        clipped_samples=clipped_samples,
        bands_db=BANDS_DB,
        bar_dbfs=(-12.0, -13.0),
    )


MEASUREMENTS_DATA = {
    "peak_dbfs": -6.0,
    "integrated_lufs": -14.0,
    "clipped_samples": 0,
    "bands_db": BANDS_DB,
    "bar_dbfs": [-12.0, -13.0],
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
    return CompleteReport("song", 1, KEY, nodes or (analyzed(),), mix_report or mix())


def incomplete(
    nodes: tuple[MeasuredNode | UnrenderedNode | UnplannedNode, ...] = (),
) -> IncompleteReport:
    return IncompleteReport("song", 1, KEY, nodes or (measured(), unrendered()))


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
        IncompleteReport("song", 1, KEY, (measured(), measured(name="pad")))


def test_an_incomplete_report_refuses_analyzed_nodes() -> None:
    with pytest.raises(ValueError, match="IncompleteReport"):
        IncompleteReport("song", 1, KEY, (analyzed(), unrendered()))  # ty: ignore[invalid-argument-type]


def test_a_complete_report_refuses_other_nodes() -> None:
    with pytest.raises(ValueError, match=r"CompleteReport\.nodes"):
        CompleteReport("song", 1, KEY, (measured(),), mix())  # ty: ignore[invalid-argument-type]


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
    part = values["bar_dbfs"]
    assert isinstance(part, list)
    part.append(0.0)
    part = node_data["defects"]
    assert isinstance(part, list)
    part.append("silent")
    nodes.clear()

    assert report_to_data(value) == expected
    assert value.nodes[0].measurements.bands_db == BANDS_DB
    assert value.nodes[0].measurements.bar_dbfs == (-12.0, -13.0)


def sine(frequency: float, seconds: float, amplitude: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    tone = (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)
    return np.stack([tone, tone])


def test_bar_levels_are_the_rms_of_each_bar() -> None:
    audio = np.zeros((2, 6 * SAMPLE_RATE), dtype=np.float32)
    audio[:, : 2 * SAMPLE_RATE] = sine(1000.0, 2.0)

    levels = bar_dbfs(audio, 2.0, 2)

    assert len(levels) == 2
    assert levels[0] == pytest.approx(-3.01, abs=0.02)
    assert levels[1] == LEVEL_FLOOR_DBFS
    assert all(round(level, 2) == level for level in levels)


def test_bar_levels_past_the_audio_are_at_the_floor() -> None:
    levels = bar_dbfs(np.full((2, SAMPLE_RATE), 0.5, dtype=np.float32), 1.0, 3)

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
