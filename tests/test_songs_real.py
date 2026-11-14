"""Every Surge reference song renders and analyzes without defects on this machine's plugins."""

from pathlib import Path

import pytest
from helpers import SURGE_DIR, real_analyze, real_render

from poser.adapters.song_formats import SONG_FORMATS
from poser.core.loading import load_song
from poser.core.report import CompleteReport

SUFFIXES = {suffix for fmt in SONG_FORMATS for suffix in fmt.suffixes}
SONGS = sorted(path for path in SURGE_DIR.glob("demo.*") if path.suffix in SUFFIXES)


def test_reference_setup_has_songs() -> None:
    assert SONGS


@pytest.mark.real_plugins
@pytest.mark.parametrize("path", SONGS, ids=[path.name for path in SONGS])
def test_song_renders_without_defects(path: Path, tmp_path: Path) -> None:
    song = load_song(path, SONG_FORMATS)
    real_render(tmp_path, song, f"songs/{path.name}")

    report = real_analyze(tmp_path, song, f"songs/{path.name}")

    assert isinstance(report, CompleteReport), report
    for node in report.nodes:
        assert node.defects == (), node.name
    assert report.mix.defects == (), report.mix.measurements.peak_dbfs
