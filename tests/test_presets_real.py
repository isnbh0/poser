"""Proves an installed preset's chunk reaches a real render, by pitch. Deselected by default."""

import base64
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import CAPTURE_STATE, fxp_bytes, real_specs, strongest_hz

from poser import render
from poser.plugin_check import load_config
from poser.presets import component_state
from poser.song import Note, Song, Track


@pytest.mark.real_plugins
def test_preset_chunk_reaches_the_render(tmp_path: Path) -> None:
    serum = next(spec for spec in real_specs() if spec.name == "Serum")
    state_file = tmp_path / "state.b64"
    subprocess.run(
        [sys.executable, "-c", CAPTURE_STATE, serum.path, str(state_file)], check=True, timeout=120
    )
    chunk = component_state(base64.b64decode(state_file.read_text()))
    presets_dir = tmp_path / "presets"
    (presets_dir / "Test").mkdir(parents=True)
    (presets_dir / "Test" / "low.fxp").write_bytes(fxp_bytes(chunk, name="low"))
    config = tmp_path / "plugins.toml"
    config.write_text(
        f'[[plugin]]\nname = "Serum"\npath = "{serum.path}"\npresets = "{presets_dir}"\n'
    )
    song = Song(
        bpm=60, tracks=(Track("lead", "pedalboard", "Serum:Test/low", (Note(0, 60, 1.5, 100),)),)
    )

    [result] = render.render_song(
        song, tmp_path / "renders" / "preset", load_config(config), sounds_dir=tmp_path / "sounds"
    )

    assert result.status is render.TrackStatus.RENDERED, result.detail
    assert result.stem is not None
    assert strongest_hz(result.stem) == pytest.approx(33, abs=3)
