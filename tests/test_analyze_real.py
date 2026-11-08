"""Renders and analyzes the demo song with the plugins on this machine. Deselected by default."""

import math
from pathlib import Path

import pytest
from helpers import SCAFFOLD_DIR, real_specs
from pedalboard.io import ReadableAudioFile

from poser import render
from poser.adapters.song_formats import SONG_FORMATS
from poser.analyze import analyze_song
from poser.core.audio import stem_samples
from poser.core.loading import load_song
from poser.core.report import AnalysisStatus, Defect


@pytest.mark.real_plugins
def test_demo_song_mixes_and_analyzes(tmp_path: Path) -> None:
    song_path = SCAFFOLD_DIR / "demo.yaml"
    song = load_song(song_path, SONG_FORMATS)
    specs = real_specs()
    render.render_song(song, tmp_path / "demo", specs, sounds_dir=tmp_path / "sounds")

    report = analyze_song(song, song_path, specs, tmp_path / "demo", tmp_path / "sounds")

    for track in report.tracks:
        assert track.status is AnalysisStatus.ANALYZED, track.detail
        assert Defect.SILENT not in track.defects
    assert report.mix is not None
    assert report.mix.defects == ()
    assert all(track.sound_key is not None for track in report.tracks)
    assert report.mix.integrated_lufs is not None
    assert math.isfinite(report.mix.integrated_lufs)
    with ReadableAudioFile(report.mix.path) as mix:  # ty: ignore[no-matching-overload]
        assert mix.frames == stem_samples(song)
