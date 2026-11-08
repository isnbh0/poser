"""Every one of the scaffold's songs renders and analyzes with no defects on this machine's
plugins."""

from pathlib import Path

import pytest
from helpers import SCAFFOLD_DIR, real_specs

from poser import analyze, render
from poser.song import SCRIPT_SUFFIX, YAML_SUFFIXES, load_song

SONGS = sorted(
    path for path in SCAFFOLD_DIR.iterdir() if path.suffix in (*YAML_SUFFIXES, SCRIPT_SUFFIX)
)


@pytest.mark.real_plugins
@pytest.mark.parametrize("path", SONGS, ids=[path.name for path in SONGS])
def test_song_renders_without_defects(path: Path, tmp_path: Path) -> None:
    specs = real_specs()
    song = load_song(path)
    song_dir = tmp_path / "renders" / path.name
    render.render_song(song, song_dir, specs, sounds_dir=tmp_path / "sounds")

    report = analyze.analyze_song(song, path, specs, song_dir, tmp_path / "sounds")

    for track in report.tracks:
        assert track.status is analyze.TrackStatus.ANALYZED, f"{track.name}: {track.detail}"
        assert track.defects == [], track.name
    assert report.mix is not None
    assert report.mix.defects == [], report.mix.peak_dbfs
