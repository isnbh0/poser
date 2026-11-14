"""Renders and analyzes the demo song with the plugins on this machine. Deselected by default."""

import math
from pathlib import Path

import pytest
from helpers import SURGE_DIR, real_analyze, real_render
from pedalboard.io import ReadableAudioFile

from poser.adapters.song_formats import SONG_FORMATS
from poser.core.audio import stem_samples
from poser.core.loading import load_song
from poser.core.report import CompleteReport, Defect


@pytest.mark.real_plugins
def test_demo_song_mixes_and_analyzes(tmp_path: Path) -> None:
    song_path = SURGE_DIR / "demo.yaml"
    song = load_song(song_path, SONG_FORMATS)
    real_render(tmp_path, song, "songs/demo.yaml")

    report = real_analyze(tmp_path, song, "songs/demo.yaml")

    assert isinstance(report, CompleteReport), report
    for node in report.nodes:
        assert Defect.SILENT not in node.defects
    assert report.mix.defects == ()
    assert all(node.sound_key is not None for node in report.nodes)
    lufs = report.mix.measurements.integrated_lufs
    assert lufs is not None
    assert math.isfinite(lufs)
    record = tmp_path / "renders" / "songs" / "demo.yaml" / "history" / str(report.run)
    assert (tmp_path / "renders" / "songs" / "demo.yaml" / "latest").resolve() == record.resolve()
    with ReadableAudioFile(str(record / report.mix.path)) as mix:  # ty: ignore[no-matching-overload]
        assert mix.frames == stem_samples(song.graph)
