"""Standalone drawing and checked, atomic directory-entry publication."""

import os
import tomllib
from pathlib import Path

import pytest
from workspaces import write_script_song, write_song

from poser.cli import piano_roll
from poser.compose import Composition
from poser.core.piano_roll import PianoRoll
from poser.core.ports import PianoRollDrawer

PNG = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "poser.toml").write_text(
        '[instruments.synth]\nadapter = "pedalboard"\nplugin = "/unavailable.vst3"\n'
        '[adapters]\nsong_formats = ["yaml", "script"]\n'
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("script", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_command_png(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    script: bool,
    explicit: bool,
) -> None:
    def forbidden(self: Composition) -> None:
        pytest.fail("audio adapter constructed")

    for name in ("instruments", "effects", "audio_files", "render_store", "analysis_tools"):
        monkeypatch.setattr(Composition, name, forbidden)
    song = (
        write_script_song(workspace / "song.py", "synth")
        if script
        else write_song(workspace / "song.yaml", ("lead", "synth"))
    )
    output = workspace / "new" / "roll.PNG"
    args = [str(song), "--output", str(output)]
    if explicit:
        args += ["--config", str(workspace / "poser.toml")]
    assert piano_roll.main(args) == 0
    assert output.read_bytes().startswith(PNG)
    assert capsys.readouterr().out.strip() == str(output)
    assert list(output.parent.iterdir()) == [output]
    assert not (workspace / "renders").exists()


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("kind", ["link", "dangling", "hardlink", "input", "directory"])
def test_rejected_before_execution(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    *,
    replace: bool,
) -> None:
    def forbidden_load(*args: object) -> None:
        pytest.fail("destination rejection must precede song loading")

    monkeypatch.setattr(piano_roll, "load_song", forbidden_load)
    marker = workspace / "executed"
    song = write_script_song(workspace / "song.py", "synth", marker)
    output = workspace / "roll.png"
    if kind == "link":
        output.symlink_to(song)
    elif kind == "dangling":
        output.symlink_to(workspace / "absent")
    elif kind == "hardlink":
        os.link(song, output)
    elif kind == "directory":
        output.mkdir()
    else:
        output = song
    original = song.read_bytes()
    assert (
        piano_roll.main([str(song), "--output", str(output), *(["--replace"] if replace else [])])
        == 2
    )
    assert song.read_bytes() == original
    assert not marker.exists()
    assert not (workspace / "absent").exists()


@pytest.mark.parametrize("replace", [False, True])
def test_publish_modes(tmp_path: Path, *, replace: bool) -> None:
    song = tmp_path / "song.py"
    song.write_text("input")
    output = tmp_path / "roll.png"
    if replace:
        output.write_bytes(b"old")

    def draw(path: Path) -> None:
        if replace:
            assert output.read_bytes() == b"old"
        path.write_bytes(PNG)

    assert piano_roll._publish(song, output, draw, replace=replace) == output
    assert output.read_bytes() == PNG
    assert sorted(p.name for p in tmp_path.iterdir()) == ["roll.png", "song.py"]


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("failure", ["draw", "commit", "race", "link", "alias"])
def test_precommit_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    *,
    replace: bool,
) -> None:
    song = tmp_path / "song.py"
    song.write_bytes(b"input")
    output = tmp_path / "roll.png"
    if replace:
        output.write_bytes(b"old")

    def deny(*args: object) -> None:
        raise OSError("publication failed")

    if failure == "commit":
        monkeypatch.setattr(os, "replace" if replace else "link", deny)

    def draw(path: Path) -> None:
        path.write_bytes(b"partial")
        if failure == "draw":
            raise RuntimeError("drawing failed")
        if failure in {"race", "link", "alias"}:
            output.unlink(missing_ok=True)
            if failure == "link":
                output.symlink_to(song)
            elif failure == "alias":
                os.link(song, output)
            else:
                output.mkdir()

    with pytest.raises((OSError, ValueError, RuntimeError)):
        piano_roll._publish(song, output, draw, replace=replace)
    assert not list(tmp_path.glob(".roll.png.*"))
    assert song.read_bytes() == b"input"
    if failure in {"draw", "commit"}:
        assert output.read_bytes() == b"old" if replace else not output.exists()


@pytest.mark.parametrize("fail_draw", [False, True])
def test_cleanup_never_changes_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    fail_draw: bool,
) -> None:
    song = tmp_path / "song.py"
    song.write_text("input")
    output = tmp_path / "roll.png"

    def deny(self: Path, *, missing_ok: bool = False) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(Path, "unlink", deny)

    def draw(path: Path) -> None:
        path.write_bytes(PNG)
        if fail_draw:
            raise RuntimeError("primary drawing error")

    if fail_draw:
        with pytest.raises(RuntimeError, match="primary drawing error"):
            piano_roll._publish(song, output, draw, replace=False)
        assert not output.exists()
    else:
        piano_roll._publish(song, output, draw, replace=False)
        assert output.read_bytes() == PNG
    (residual,) = tmp_path.glob(".roll.png.*")
    assert str(residual) in capsys.readouterr().err


@pytest.mark.parametrize(
    "problem", ["suffix", "config", "song", "ancestor", "existing", "permission"]
)
def test_command_errors(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    problem: str,
) -> None:
    song = write_script_song(workspace / "song.py", "synth")
    output = workspace / "roll.png"
    if problem == "suffix":
        output = workspace / "roll.jpg"
    elif problem == "config":
        (workspace / "poser.toml").write_text("invalid")
    elif problem == "song":
        song.write_text("def build(): raise ValueError('broken song')")
    elif problem == "ancestor":
        output.write_text("ancestor")
        output /= "child.png"
    elif problem == "existing":
        output.write_bytes(b"old")
    else:

        def denied(*args: object, **kwargs: object) -> tuple[int, str]:
            raise PermissionError("unwritable parent")

        monkeypatch.setattr(piano_roll.tempfile, "mkstemp", denied)
    assert piano_roll.main([str(song), "--output", str(output)]) == 2
    error = capsys.readouterr().err
    assert "piano-roll:" in error
    assert "Traceback" not in error
    assert not list(workspace.glob(".roll*"))


def test_outside_direct_graph(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = workspace.parent / f"{workspace.name}-outside.py"
    outside.write_text(
        "from poser.core.graph import Graph, Node\n"
        "from poser.core.song import Note\n"
        "def build():\n"
        "    return Graph(120, (Node('lead', 'track', sound='synth', "
        "notes=(Note(0, 60, 2, 100),)),))\n"
    )
    rolls: list[PianoRoll] = []
    original = Composition.piano_roll_drawer

    class RecordingDrawer:
        def __init__(self, inner: PianoRollDrawer) -> None:
            self.inner = inner

        def draw(self, roll: PianoRoll, path: Path) -> None:
            rolls.append(roll)
            self.inner.draw(roll, path)

    def recording(self: Composition) -> PianoRollDrawer:
        return RecordingDrawer(original(self))

    monkeypatch.setattr(Composition, "piano_roll_drawer", recording)
    output = workspace / "graph.png"
    assert piano_roll.main([str(outside), "--output", str(output)]) == 0
    assert output.read_bytes().startswith(PNG)
    assert rolls[0].panels[0].name == "lead"
    assert rolls[0].panels[0].notes[0].pitch == 60


def test_competing_regular_file(tmp_path: Path) -> None:
    song = tmp_path / "song.py"
    song.write_text("input")
    output = tmp_path / "roll.png"

    def draw(path: Path) -> None:
        path.write_bytes(PNG)
        output.write_bytes(b"competitor")

    with pytest.raises(FileExistsError):
        piano_roll._publish(song, output, draw, replace=False)
    assert output.read_bytes() == b"competitor"
    assert not list(tmp_path.glob(".roll.png.*"))


def test_exclusive_commit_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    song = tmp_path / "song.py"
    song.write_text("input")
    output = tmp_path / "roll.png"
    link = os.link

    def race(source: Path, destination: Path) -> None:
        destination.write_bytes(b"competitor")
        link(source, destination)

    monkeypatch.setattr(os, "link", race)

    def draw(path: Path) -> None:
        path.write_bytes(PNG)

    with pytest.raises(FileExistsError):
        piano_roll._publish(song, output, draw, replace=False)
    assert output.read_bytes() == b"competitor"
    assert not list(tmp_path.glob(".roll.png.*"))


def test_unsupported_song(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    song = workspace / "song.txt"
    song.write_text("invalid")
    assert piano_roll.main([str(song), "--output", "roll.png"]) == 2
    assert "cannot read" in capsys.readouterr().err
    assert not (workspace / "roll.png").exists()


@pytest.mark.parametrize("fail_draw", [False, True])
def test_command_cleanup_outcome(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    fail_draw: bool,
) -> None:
    song = write_song(workspace / "song.yaml", ("lead", "synth"))
    output = workspace / "roll.png"

    class Drawer:
        def draw(self, roll: PianoRoll, path: Path) -> None:
            path.write_bytes(PNG)
            if fail_draw:
                raise RuntimeError("primary draw failure")

    def drawer(self: Composition) -> PianoRollDrawer:
        return Drawer()

    def deny(self: Path, *, missing_ok: bool = False) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(Composition, "piano_roll_drawer", drawer)
    monkeypatch.setattr(Path, "unlink", deny)
    assert piano_roll.main([str(song), "--output", str(output)]) == (2 if fail_draw else 0)
    error = capsys.readouterr().err
    assert "piano-roll: warning:" in error
    assert ("primary draw failure" in error) == fail_draw
    assert output.exists() != fail_draw


def test_replace_absent_destination(workspace: Path) -> None:
    song = write_song(workspace / "song.yaml", ("lead", "synth"))
    assert piano_roll.main([str(song), "--output", "roll.png", "--replace"]) == 0
    assert (workspace / "roll.png").read_bytes().startswith(PNG)
    assert not list(workspace.glob(".roll.png.*"))


def test_replace_existing_png(workspace: Path) -> None:
    song = write_song(workspace / "song.yaml", ("lead", "synth"))
    output = workspace / "roll.png"
    output.write_bytes(b"previous artifact")
    assert piano_roll.main([str(song), "--output", str(output), "--replace"]) == 0
    from matplotlib import image

    assert output.read_bytes().startswith(PNG)
    assert image.imread(output).size > 0
    assert not list(workspace.glob(".roll.png.*"))


def test_script_registration() -> None:
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["piano-roll"] == "poser.cli.piano_roll:main"
    assert callable(piano_roll.main)
