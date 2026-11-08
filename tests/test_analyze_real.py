"""Renders and analyzes the demo song with the plugins on this machine. Deselected by default."""

import math
from pathlib import Path

import pytest
from helpers import REPO_DIR, real_specs
from pedalboard.io import ReadableAudioFile

from poser import render
from poser.analyze import Defect, TrackStatus, analyze_song
from poser.song import load_song


@pytest.mark.real_plugins
def test_demo_song_mixes_and_analyzes(tmp_path: Path) -> None:
    song_path = REPO_DIR / "songs" / "demo.yaml"
    song = load_song(song_path)
    specs = real_specs()
    render.render_song(song, tmp_path / "demo", specs, sounds_dir=tmp_path / "sounds")

    report = analyze_song(song, song_path, specs, tmp_path / "demo", tmp_path / "sounds")

    for track in report.tracks:
        assert track.status is TrackStatus.ANALYZED, track.detail
        assert Defect.SILENT not in track.defects
    assert report.mix is not None
    assert report.mix.defects == []
    assert all(track.sound_key is not None for track in report.tracks)
    assert report.mix.integrated_lufs is not None
    assert math.isfinite(report.mix.integrated_lufs)
    with ReadableAudioFile(report.mix.path) as mix:  # ty: ignore[no-matching-overload]
        assert mix.frames == render.expected_samples(render.song_duration_s(song))
