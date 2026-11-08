"""Renders the demo song through the plugins installed on this machine. Deselected by default."""

from pathlib import Path

import numpy as np
import pytest
from helpers import REPO_DIR, band_spectrum_db, change_time_s, read_stem, real_specs
from pedalboard.io import ReadableAudioFile

from poser import render
from poser.plugin_check import measure_peak_dbfs
from poser.render import TrackStatus
from poser.song import ControlChange, Note, Song, Track, load_song


@pytest.mark.real_plugins
def test_demo_song_renders_aligned_audible_stems(tmp_path: Path) -> None:
    song = load_song(REPO_DIR / "songs" / "demo.yaml")
    expected_samples = render.expected_samples(render.song_duration_s(song))

    results = render.render_song(
        song, tmp_path / "demo", real_specs(), sounds_dir=tmp_path / "sounds"
    )

    assert [result.track for result in results] == [track.name for track in song.tracks]
    for result in results:
        assert result.status is TrackStatus.RENDERED, result.detail
        assert result.stem is not None
        with ReadableAudioFile(result.stem) as stem:  # ty: ignore[no-matching-overload]
            assert stem.samplerate == render.SAMPLE_RATE
            audio = stem.read(stem.frames)
        assert audio.shape == (render.NUM_CHANNELS, expected_samples)
        assert measure_peak_dbfs(audio) > render.SILENCE_DBFS


SOUND = "Serum:Leads/LD A Bit of Luck [SD]"


def render_cc_song(tmp_path: Path, name: str, beats: float, *changes: ControlChange) -> np.ndarray:
    track = Track("lead", "pedalboard", SOUND, (Note(0, 60, beats, 100),), cc=changes)
    (result,) = render.render_song(
        Song(120, (track,)), tmp_path / name, real_specs(), sounds_dir=tmp_path / "sounds"
    )
    assert result.status is TrackStatus.RENDERED, result.detail
    assert result.stem is not None
    return read_stem(str(result.stem))


@pytest.mark.real_plugins
def test_mod_wheel_changes_a_preset_that_maps_it(tmp_path: Path) -> None:
    low = render_cc_song(tmp_path, "low", 3, ControlChange(0, 1, 0))
    high = render_cc_song(tmp_path, "high", 3, ControlChange(0, 1, 127))

    difference = np.abs(band_spectrum_db(low, 0.3, 1.5) - band_spectrum_db(high, 0.3, 1.5))

    assert difference.mean() > 6


@pytest.mark.real_plugins
def test_controller_change_lands_within_a_block_of_its_time(tmp_path: Path) -> None:
    held = render_cc_song(tmp_path, "held", 5, ControlChange(0, 1, 0))
    stepped = render_cc_song(
        tmp_path, "stepped", 5, ControlChange(0, 1, 0), ControlChange(2.6, 1, 127)
    )

    # The step is at 1.30 seconds. In 8192-sample blocks it took effect at about 1.21 seconds.
    assert 1.27 <= change_time_s(held, stepped) <= 1.35
