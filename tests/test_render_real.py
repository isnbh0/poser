"""Renders the demo song through the plugins installed on this machine. Deselected by default."""

from pathlib import Path

import pytest
from pedalboard.io import ReadableAudioFile

from vsti_composer import render
from vsti_composer.plugin_check import DEFAULT_CONFIG, PROJECT_DIR, load_config, measure_peak_dbfs
from vsti_composer.render import TrackStatus
from vsti_composer.song import load_song


@pytest.mark.real_plugins
def test_demo_song_renders_aligned_audible_stems(tmp_path: Path) -> None:
    song = load_song(PROJECT_DIR / "songs" / "demo.yaml")
    expected_samples = render.expected_samples(render.song_duration_s(song))

    results = render.render_song(song, "demo", load_config(DEFAULT_CONFIG), tmp_path)

    assert [result.track for result in results] == [track.name for track in song.tracks]
    for result in results:
        assert result.status is TrackStatus.RENDERED, result.detail
        assert result.stem is not None
        with ReadableAudioFile(result.stem) as stem:  # ty: ignore[no-matching-overload]
            assert stem.samplerate == render.SAMPLE_RATE
            audio = stem.read(stem.frames)
        assert audio.shape == (render.NUM_CHANNELS, expected_samples)
        assert measure_peak_dbfs(audio) > render.SILENCE_DBFS
