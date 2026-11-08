"""Tests that each song file renders into its own directory, named by its path in the workspace."""

import sys
from pathlib import Path

import pytest
import yaml

from poser import analyze, render
from poser.analyze import analyze_song
from poser.plugin_check import EXIT_BAD_CONFIG, PluginSpec
from poser.song import load_song

NOTE = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}


def make_workspace(root: Path) -> None:
    """Make `root` a workspace whose plugin list names one plugin at a missing path."""
    (root / "plugins.toml").write_text(f'[[plugin]]\nname = "Serum"\npath = "{root / "x.vst3"}"\n')


def write_yaml_song(path: Path, sound: str = "Nope", bpm: int = 120) -> Path:
    """Write a one-track song; the default unknown sound means no worker runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    track = {"name": "lead", "engine": "pedalboard", "sound": sound, "notes": [NOTE]}
    path.write_text(yaml.safe_dump({"bpm": bpm, "tracks": [track]}))
    return path


def write_script_song(path: Path, bpm: int = 90) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from poser.song import Note, Song, Track\n\n\n"
        "def build() -> Song:\n"
        "    note = Note(0, 60, 1, 100)\n"
        f"    return Song({bpm}, (Track('lead', 'pedalboard', 'Nope', (note,)),))\n"
    )
    return path


def test_songs_with_one_name_in_different_directories_keep_apart(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    first = write_yaml_song(tmp_path / "songs" / "x.yaml")
    second = write_yaml_song(tmp_path / "drafts" / "x.yaml")
    render.main([str(first)])
    stem = tmp_path / "renders" / "songs" / "x.yaml" / "lead.wav"
    stem.write_bytes(b"first song's stem")

    render.main([str(second)])

    assert (tmp_path / "renders" / "drafts" / "x.yaml").is_dir()
    assert stem.read_bytes() == b"first song's stem"


def test_script_and_yaml_with_one_stem_render_apart(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    script = write_script_song(tmp_path / "songs" / "x.py")
    data = write_yaml_song(tmp_path / "songs" / "x.yaml")

    assert load_song(script).bpm == 90
    assert load_song(data).bpm == 120
    for song in (script, data):
        render.main([str(song)])

    assert (tmp_path / "renders" / "songs" / "x.py").is_dir()
    assert (tmp_path / "renders" / "songs" / "x.yaml").is_dir()


def test_song_beside_a_directory_of_its_name_keeps_its_mix(tmp_path: Path) -> None:
    make_workspace(tmp_path)
    outer = write_script_song(tmp_path / "songs" / "x.py")
    inner = write_script_song(tmp_path / "songs" / "x" / "mix.py")
    render.main([str(outer)])
    mix = tmp_path / "renders" / "songs" / "x.py" / "mix" / "mix.wav"
    mix.parent.mkdir()
    mix.write_bytes(b"outer song's mix")

    render.main([str(inner)])

    assert (tmp_path / "renders" / "songs" / "x" / "mix.py").is_dir()
    assert mix.read_bytes() == b"outer song's mix"


@pytest.mark.parametrize(
    ("prog", "main"), [("render-song", render.main), ("analyze-song", analyze.main)]
)
def test_song_outside_the_workspace_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], prog: str, main: object
) -> None:
    assert callable(main)
    make_workspace(tmp_path)
    song = write_yaml_song(tmp_path.parent / f"elsewhere-{tmp_path.name}" / "x.yaml")

    assert main([str(song)]) == EXIT_BAD_CONFIG

    assert capsys.readouterr().err == (
        f"{prog}: {song.resolve()} is outside the workspace at {tmp_path.resolve()}\n"
    )
    assert not (tmp_path / "renders").exists()


@pytest.mark.parametrize("main", [render.main, analyze.main], ids=["render", "analyze"])
def test_script_outside_the_workspace_never_runs(tmp_path: Path, main: object) -> None:
    assert callable(main)
    make_workspace(tmp_path)
    outside = tmp_path.parent / f"elsewhere-{tmp_path.name}"
    sentinel = outside / "ran"
    song = outside / "x.py"
    outside.mkdir()
    song.write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n")

    assert main([str(song)]) == EXIT_BAD_CONFIG

    assert not sentinel.exists()


def test_every_path_option_given_makes_the_working_directory_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "cfg" / "plugins.toml"
    config.parent.mkdir()
    config.write_text('[[plugin]]\nname = "Serum"\npath = "/missing.vst3"\n')
    song = write_yaml_song(tmp_path / "songs" / "x.yaml")
    out = tmp_path / "out"
    argv = [
        str(song),
        "--config",
        str(config),
        "--output-dir",
        str(out),
        "--sounds-dir",
        str(tmp_path / "sounds"),
    ]

    render.main(argv)
    assert (out / "songs" / "x.yaml").is_dir()

    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    capsys.readouterr()
    assert render.main(argv) == EXIT_BAD_CONFIG
    assert f"outside the workspace at {(tmp_path / 'sub').resolve()}\n" in capsys.readouterr().err


def test_analysis_reads_the_stems_render_wrote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    specs = [PluginSpec("Serum", "/serum.vst3")]
    song_path = write_yaml_song(tmp_path / "songs" / "x.yaml", sound="Serum")
    song = load_song(song_path)
    samples = render.expected_samples(render.song_duration_s(song))
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "import numpy as np\n"
        "from poser.render import write_stem\n"
        "job = json.loads(Path(sys.argv[-2]).read_text())\n"
        f"write_stem(Path(job['stem_path']), np.full((2, {samples}), 0.1, np.float32))\n"
        "Path(sys.argv[-1]).write_text(json.dumps(dict(status='rendered', peak_dbfs=-20.0)))\n"
    )
    monkeypatch.setattr(
        render, "worker_command", lambda job_path: [sys.executable, str(worker), str(job_path)]
    )
    song_dir = tmp_path / "renders" / "songs" / "x.yaml"

    render.render_song(song, song_dir, specs, sounds_dir=tmp_path / "sounds")
    report = analyze_song(song, song_path, specs, song_dir, tmp_path / "sounds")

    assert [track.status for track in report.tracks] == [analyze.TrackStatus.ANALYZED]
    assert report.mix is not None
    assert report.mix.path == str(song_dir.resolve() / "mix" / "mix.wav")
