"""Renders the demo song through the plugins installed on this machine. Deselected by default."""

from pathlib import Path

import numpy as np
import pytest
from helpers import SCAFFOLD_DIR, band_spectrum_db, change_time_s, read_stem, real_render
from pedalboard.io import ReadableAudioFile

from poser.adapters.song_formats import SONG_FORMATS
from poser.core.audio import (
    NUM_CHANNELS,
    SAMPLE_RATE,
    SILENCE_DBFS,
    measure_peak_dbfs,
    stem_samples,
)
from poser.core.loading import load_song
from poser.core.song import ControlChange, Note, Song, Track
from poser.core.statuses import StemStatus


@pytest.mark.real_plugins
def test_demo_song_renders_aligned_audible_stems(tmp_path: Path) -> None:
    song = load_song(SCAFFOLD_DIR / "demo.yaml", SONG_FORMATS)
    expected_samples = stem_samples(song)

    results = real_render(tmp_path, song, "songs/demo.yaml")

    assert [result.track for result in results] == [track.name for track in song.tracks]
    for result in results:
        assert result.status is StemStatus.RENDERED, result.detail
        assert result.stem is not None
        with ReadableAudioFile(str(result.stem)) as stem:  # ty: ignore[no-matching-overload]
            assert stem.samplerate == SAMPLE_RATE
            audio = stem.read(stem.frames)
        assert audio.shape == (NUM_CHANNELS, expected_samples)
        assert measure_peak_dbfs(audio) > SILENCE_DBFS


SOUND = "serum:presets/Leads/LD A Bit of Luck [SD]"


def render_cc_song(tmp_path: Path, name: str, beats: float, *changes: ControlChange) -> np.ndarray:
    track = Track("lead", SOUND, (Note(0, 60, beats, 100),), cc=changes)
    (result,) = real_render(tmp_path, Song(120, (track,)), f"songs/{name}.yaml")
    assert result.status is StemStatus.RENDERED, result.detail
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
