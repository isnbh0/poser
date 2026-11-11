"""Tests that each song file renders into its own directory, named by its path in the workspace."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from workspaces import CommandWorkspace, make_workspace, write_script_song, write_song

from poser.adapters.song_formats import SONG_FORMATS
from poser.cli import analyze, render
from poser.cli.common import EXIT_ALL_PASS, EXIT_BAD_CONFIG
from poser.core.loading import load_song
from poser.core.song import Song


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CommandWorkspace:
    return make_workspace(tmp_path, monkeypatch)


def test_songs_with_one_name_in_different_directories_keep_apart(
    workspace: CommandWorkspace,
) -> None:
    first = write_song(workspace.root / "songs" / "x.yaml", ("lead", "synth"))
    second = write_song(workspace.root / "drafts" / "x.yaml", ("lead", "synth"))
    render.main([str(first)])
    stem = workspace.renders / "songs" / "x.yaml" / "stems" / "lead.wav"
    before = stem.read_bytes()

    render.main([str(second)])

    assert (workspace.renders / "drafts" / "x.yaml" / "stems" / "lead.wav").is_file()
    assert stem.read_bytes() == before


def test_script_and_yaml_with_one_stem_render_apart(workspace: CommandWorkspace) -> None:
    script = write_script_song(workspace.root / "songs" / "x.py", "synth")
    data = write_song(workspace.root / "songs" / "x.yaml", ("lead", "synth"))

    script_song = load_song(script, SONG_FORMATS)
    data_song = load_song(data, SONG_FORMATS)
    assert isinstance(script_song, Song)
    assert isinstance(data_song, Song)
    assert script_song.tracks == data_song.tracks
    for song in (script, data):
        assert render.main([str(song)]) == EXIT_ALL_PASS

    assert (workspace.renders / "songs" / "x.py" / "stems" / "lead.wav").is_file()
    assert (workspace.renders / "songs" / "x.yaml" / "stems" / "lead.wav").is_file()


def test_song_beside_a_directory_of_its_name_keeps_its_mix(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    outer = write_script_song(workspace.root / "songs" / "x.py", "synth")
    inner = write_script_song(workspace.root / "songs" / "x" / "mix.py", "synth")
    render.main([str(outer)])
    analyze.main([str(outer)])
    mix = workspace.renders / "songs" / "x.py" / "latest" / "mix.wav"
    before = mix.read_bytes()

    assert render.main([str(inner)]) == EXIT_ALL_PASS
    assert analyze.main([str(inner)]) == EXIT_ALL_PASS

    assert (workspace.renders / "songs" / "x" / "mix.py" / "latest" / "mix.wav").is_file()
    assert mix.read_bytes() == before


@pytest.mark.parametrize(
    ("prog", "main"), [("render-song", render.main), ("analyze-song", analyze.main)]
)
def test_song_outside_the_workspace_exits_2(
    workspace: CommandWorkspace,
    tmp_path_factory: pytest.TempPathFactory,
    capsys: pytest.CaptureFixture[str],
    prog: str,
    main: Callable[[list[str]], int],
) -> None:
    song = write_song(tmp_path_factory.mktemp("elsewhere") / "x.yaml", ("lead", "synth"))

    assert main([str(song)]) == EXIT_BAD_CONFIG

    assert capsys.readouterr().err == (
        f"{prog}: {song.resolve()} is outside the workspace at {workspace.root}\n"
    )
    assert not workspace.renders.exists()


def test_every_path_option_given_makes_the_working_directory_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = make_workspace(tmp_path / "cfg", monkeypatch)
    song = write_song(tmp_path / "songs" / "x.yaml", ("lead", "synth"))
    out = tmp_path / "out"
    argv = [
        str(song),
        "--config",
        str(workspace.config),
        "--output-dir",
        str(out),
        "--sounds-dir",
        str(tmp_path / "sounds"),
    ]

    assert render.main(argv) == EXIT_ALL_PASS
    assert (out / "songs" / "x.yaml" / "stems" / "lead.wav").is_file()
    assert not workspace.renders.exists()

    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    capsys.readouterr()
    assert render.main(argv) == EXIT_BAD_CONFIG
    assert f"outside the workspace at {(tmp_path / 'sub').resolve()}\n" in capsys.readouterr().err


@pytest.mark.parametrize("cwd", ["another-workspace", "no-workspace"])
def test_config_alone_makes_its_directory_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cwd: str
) -> None:
    workspace = make_workspace(tmp_path / "cfg", monkeypatch)
    song = write_song(workspace.root / "songs" / "x.yaml", ("lead", "synth"))
    here = tmp_path / cwd
    here.mkdir()
    if cwd == "another-workspace":
        make_workspace(here, monkeypatch)
    before = sorted(path.name for path in here.iterdir())
    monkeypatch.chdir(here)
    config = ["--config", str(workspace.config)]

    assert render.main([str(song), *config]) == EXIT_ALL_PASS
    assert analyze.main([str(song), *config]) == EXIT_ALL_PASS

    assert (workspace.renders / "songs" / "x.yaml" / "stems" / "lead.wav").is_file()
    assert (workspace.renders / "songs" / "x.yaml" / "latest" / "mix.wav").is_file()
    assert sorted(path.name for path in here.iterdir()) == before


def test_analysis_reads_the_stems_render_wrote(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_song(workspace.root / "songs" / "x.yaml", ("lead", "synth"))
    render.main(["songs/x.yaml"])
    capsys.readouterr()

    assert analyze.main(["songs/x.yaml", "--json"]) == EXIT_ALL_PASS

    report = json.loads(capsys.readouterr().out)
    assert [node["status"] for node in report["nodes"]] == ["analyzed"]
    song_dir = workspace.renders / "songs" / "x.yaml"
    assert report["mix"]["path"] == str(song_dir / "latest" / "mix.wav")
