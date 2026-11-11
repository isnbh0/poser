"""Tests of the report schema, its JSON form, and the built-in analysis policy."""

import json
from dataclasses import replace

import numpy as np
import pytest

from poser.core.audio import SAMPLE_RATE
from poser.core.jsonvalue import Json
from poser.core.names import Identifier
from poser.core.report import (
    BANDS,
    LEVEL_FLOOR_DBFS,
    AnalysisStatus,
    Defect,
    Measurements,
    MixReport,
    NodeReport,
    Report,
    SectionData,
    SectionError,
    bar_dbfs,
    mix_defects,
    report_failed,
    report_to_data,
    silent_defects,
)

KEY = "0" * 64
BANDS_DB = dict.fromkeys(BANDS, -30.0)


def measurements(peak_dbfs: float = -6.0, clipped_samples: int = 0) -> Measurements:
    return Measurements(
        peak_dbfs=peak_dbfs,
        integrated_lufs=-14.0,
        clipped_samples=clipped_samples,
        bands_db=BANDS_DB,
        bar_dbfs=(-12.0, -13.0),
    )


def node(**changes: object) -> NodeReport:
    clean = NodeReport(
        name="lead",
        status=AnalysisStatus.ANALYZED,
        node_key=KEY,
        sound_key=KEY,
        kind="track",
        upstream=(),
        gain_db=-3.0,
        peak_dbfs=-6.0,
        integrated_lufs=-14.0,
        clipped_samples=0,
        bands_db=BANDS_DB,
        bar_dbfs=(-12.0, -13.0),
        spectrogram="lead.png",
    )
    return replace(clean, **changes)


def mix(**changes: object) -> MixReport:
    clean = MixReport(
        path="mix.wav",
        peak_dbfs=-6.0,
        integrated_lufs=-14.0,
        clipped_samples=0,
        bands_db=BANDS_DB,
        bar_dbfs=(-12.0, -13.0),
        spectrogram="mix.png",
        defects=(),
    )
    return replace(clean, **changes)


def report(nodes: tuple[NodeReport, ...] = (), mix_report: MixReport | None = None) -> Report:
    return Report(song="song", run=1, song_sha256=KEY, nodes=nodes or (node(),), mix=mix_report)


SECTION_DEFECT = {Identifier("tempo"): SectionData(1, Json.of({}), (Identifier("drift"),))}
SECTION_ERROR = {Identifier("tempo"): SectionError(1, "no beat found")}
SECTION_CLEAN = {Identifier("tempo"): SectionData(1, Json.of({"bpm": 120}))}


@pytest.mark.parametrize(
    ("value", "failed"),
    [
        (report(mix_report=mix()), False),
        (report((node(sections=SECTION_CLEAN),), mix()), False),
        (report(), True),
        (
            report(
                (node(status=AnalysisStatus.STALE_STEM, detail="song changed"),),
                mix(),
            ),
            True,
        ),
        (report((node(defects=(Defect.SILENT,)),), mix()), True),
        (report(mix_report=mix(defects=(Defect.HEADROOM,))), True),
        (report((node(sections=SECTION_DEFECT),), mix()), True),
        (report(mix_report=mix(sections=SECTION_DEFECT)), True),
        (report((node(sections=SECTION_ERROR),), mix()), True),
        (report(mix_report=mix(sections=SECTION_ERROR)), True),
    ],
    ids=[
        "clean",
        "clean_section",
        "missing_mix",
        "stale_track",
        "track_defect",
        "mix_defect",
        "track_section_defect",
        "mix_section_defect",
        "track_section_error",
        "mix_section_error",
    ],
)
def test_report_failed(value: Report, failed: bool) -> None:
    assert report_failed(value) is failed


NODE_KEYS = {
    "name",
    "status",
    "kind",
    "upstream",
    "node_key",
    "sound_key",
    "gain_db",
    "peak_dbfs",
    "integrated_lufs",
    "clipped_samples",
    "bands_db",
    "bar_dbfs",
    "spectrogram",
    "defects",
    "detail",
    "sections",
}
MIX_KEYS = {
    "path",
    "peak_dbfs",
    "integrated_lufs",
    "clipped_samples",
    "bands_db",
    "bar_dbfs",
    "spectrogram",
    "defects",
    "sections",
}


def test_report_to_data_has_the_documented_keys_and_shapes() -> None:
    value = report(
        (node(sections=SECTION_DEFECT, defects=(Defect.SILENT,)),),
        mix(sections=SECTION_ERROR, defects=(Defect.CLIPPING,)),
    )

    data = report_to_data(value)

    assert isinstance(data, dict)
    assert set(data) == {"song", "run", "song_sha256", "nodes", "mix"}
    nodes = data["nodes"]
    assert isinstance(nodes, list)
    (node_data,) = nodes
    mix_data = data["mix"]
    assert isinstance(node_data, dict)
    assert isinstance(mix_data, dict)
    assert set(node_data) == NODE_KEYS
    assert set(mix_data) == MIX_KEYS
    assert "tracks" not in data
    assert node_data["kind"] == "track"
    assert node_data["upstream"] == []
    for part in (node_data, mix_data):
        assert isinstance(part["defects"], list)
        assert isinstance(part["bar_dbfs"], list)
        assert isinstance(part["bands_db"], dict)
        assert isinstance(part["sections"], dict)
    assert node_data["status"] == "analyzed"
    assert node_data["defects"] == ["silent"]
    assert node_data["sections"] == {"tempo": {"version": 1, "data": {}, "defects": ["drift"]}}
    assert mix_data["defects"] == ["clipping"]
    assert mix_data["sections"] == {"tempo": {"version": 1, "error": "no beat found"}}
    assert json.loads(json.dumps(data, allow_nan=False)) == data


def test_a_report_with_upstream_nodes_names_them_in_json() -> None:
    value = report((node(upstream=("bass", "drums")),))

    data = report_to_data(value)

    assert isinstance(data, dict)
    nodes = data["nodes"]
    assert isinstance(nodes, list)
    (node_data,) = nodes
    assert isinstance(node_data, dict)
    assert node_data["upstream"] == ["bass", "drums"]


def test_changing_report_data_leaves_the_report_unchanged() -> None:
    value = report((node(sections=SECTION_CLEAN),), mix())
    expected = report_to_data(value)
    data = report_to_data(value)
    assert isinstance(data, dict)
    nodes = data["nodes"]
    assert isinstance(nodes, list)
    node_data = nodes[0]
    assert isinstance(node_data, dict)

    for key in ("bands_db", "sections"):
        part = node_data[key]
        assert isinstance(part, dict)
        part.clear()
    for key in ("bar_dbfs", "defects"):
        part = node_data[key]
        assert isinstance(part, list)
        part.append(0.0)
    nodes.clear()

    assert report_to_data(value) == expected
    assert value.nodes[0].bands_db == BANDS_DB
    assert value.nodes[0].bar_dbfs == (-12.0, -13.0)


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
    with pytest.raises(ValueError, match="Report"):
        replace(report(), **changes)
