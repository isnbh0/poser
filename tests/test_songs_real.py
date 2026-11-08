"""Every song in `songs/` renders and analyzes on this machine's plugins with no defects."""

from pathlib import Path

import pytest

from poser import analyze, render
from poser.plugin_check import DEFAULT_CONFIG, PROJECT_DIR, load_config
from poser.song import SCRIPT_SUFFIX, YAML_SUFFIXES, load_song

SONGS = sorted(
    path
    for path in (PROJECT_DIR / "songs").iterdir()
    if path.suffix in (*YAML_SUFFIXES, SCRIPT_SUFFIX)
)


@pytest.mark.real_plugins
@pytest.mark.parametrize("path", SONGS, ids=[path.name for path in SONGS])
def test_song_renders_without_defects(path: Path, tmp_path: Path) -> None:
    specs = load_config(DEFAULT_CONFIG)
    song = load_song(path)
    output_dir = tmp_path / "renders"
    render.render_song(song, path.stem, specs, output_dir)

    report = analyze.analyze_song(song, path, specs, output_dir)

    for track in report.tracks:
        assert track.status is analyze.TrackStatus.ANALYZED, f"{track.name}: {track.detail}"
        assert track.defects == [], track.name
    assert report.mix is not None
    assert report.mix.defects == [], report.mix.peak_dbfs
