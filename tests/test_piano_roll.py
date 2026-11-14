"""Authored-note projection and headless drawing contracts."""

import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from typing import cast

import pytest
from matplotlib import image
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from poser.adapters import piano_roll
from poser.core.graph import DelayNode, EffectNode, Graph, Wire
from poser.core.piano_roll import PianoRoll, PianoRollPanel, project_piano_roll
from poser.core.rules import ControlChange, Note
from poser.core.song import Song, Track


def song() -> Song:
    notes = (Note(0.25, 0, 1.5, 1), Note(0.5, 127, 2.25, 127), Note(0.25, 0, 1.5, 1))
    return Song(
        123.5,
        (
            Track("lead", "synth", notes, effects=("echo",)),
            Track("bass", "bass", (Note(3.125, 36, 0.5, 64),)),
        ),
    )


def test_projection_preserves_authored_events_without_compiling() -> None:
    source = song()
    before = repr(source)
    roll = project_piano_roll(source.graph())
    assert roll.tempo == 123.5
    assert tuple(panel.name for panel in roll.panels) == ("lead", "bass")
    assert tuple(panel.notes for panel in roll.panels) == tuple(t.notes for t in source.tracks)
    assert [(n.beat, n.pitch, n.length, n.velocity) for n in roll.panels[0].notes] == [
        (0.25, 0, 1.5, 1),
        (0.5, 127, 2.25, 127),
        (0.25, 0, 1.5, 1),
    ]
    assert repr(source) == before
    changed = replace(
        source,
        tracks=tuple(
            replace(t, sound="different", gain=-12, cc=(ControlChange(0, 1, 100),), effects=())
            for t in source.tracks
        ),
    )
    assert project_piano_roll(changed.graph()) == roll


def test_graph_filtering_order_and_routing_independence() -> None:
    tracks = song().graph().nodes
    graph = Graph(
        123.5,
        (tracks[2], DelayNode("delay", delay_samples=512), tracks[1], tracks[0]),
        (Wire("lead", "delay", "audio"),),
    )
    roll = project_piano_roll(graph)
    assert tuple(p.name for p in roll.panels) == ("bass", "lead")
    assert roll == project_piano_roll(replace(graph, wires=()))
    assert project_piano_roll(Graph(120, (EffectNode("fx", effect="unknown"),))).panels == ()


@pytest.mark.parametrize("tempo", [0, -1, float("nan"), float("inf"), -float("inf"), True, "120"])
def test_invalid_tempo(tempo: object) -> None:
    with pytest.raises(ValueError, match="positive number"):
        PianoRoll(cast("float", tempo), ())


@pytest.mark.parametrize("name", ["", "bad name", "../bad", 123])
def test_invalid_panel_name(name: object) -> None:
    with pytest.raises(ValueError, match="panel name"):
        PianoRollPanel(cast("str", name), ())


def test_invalid_collections_and_duplicate_names() -> None:
    for notes in (None, (1,), ({"pitch": 60},)):
        with pytest.raises(ValueError, match="Panel notes"):
            PianoRollPanel("valid", cast("tuple[Note, ...]", notes))
    for panels in (None, (1,), (PianoRollPanel("a", ()), PianoRollPanel("A", ()))):
        with pytest.raises(ValueError, match=r"Panels|duplicate"):
            PianoRoll(120, cast("tuple[PianoRollPanel, ...]", panels))
    with pytest.raises(ValueError, match="finite"):
        PianoRollPanel("a", (Note(1e308, 60, 1e308, 100),))


def test_normalization_and_immutability() -> None:
    notes = [Note(0, 60, 1, 100)]
    panel = PianoRollPanel("track.name", cast("tuple[Note, ...]", notes))
    panels = [panel]
    roll = PianoRoll(120, cast("tuple[PianoRollPanel, ...]", panels))
    notes.clear()
    panels.clear()
    assert roll.panels == (panel,)
    assert panel.notes == (Note(0, 60, 1, 100),)
    for target, field, value in (
        (roll, "tempo", 99),
        (panel, "notes", ()),
        (panel.notes[0], "beat", 99),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, field, value)


def test_figure_geometry_and_labels() -> None:
    roll = project_piano_roll(song().graph())
    figure = piano_roll.build_figure(roll)
    try:
        assert len(figure.axes) == 2
        assert any("123.5 BPM" in text.get_text() for text in figure.texts)
        assert any("Velocity" in text.get_text() for text in figure.texts)
        colors = []
        for axes, panel in zip(figure.axes, roll.panels, strict=True):
            assert axes.get_title(loc="left") == panel.name
            assert axes.get_ylabel() == "MIDI pitch"
            assert axes.get_xlim() == (0, 3.625)
            assert len(axes.patches) == len(panel.notes)
            for patch, note in zip(axes.patches, panel.notes, strict=True):
                assert isinstance(patch, Rectangle)
                assert (patch.get_x(), patch.get_y()) == (note.beat, note.pitch - 0.5)
                assert (patch.get_width(), patch.get_height()) == (note.length, 1)
                assert patch.get_alpha() == pytest.approx(0.25 + 0.75 * note.velocity / 127)
                assert axes.get_ylim()[0] <= note.pitch - 0.5
                assert axes.get_ylim()[1] >= note.pitch + 0.5
            colors.append(axes.patches[0].get_facecolor()[:3])
        assert colors[0] != colors[1]
        assert figure.axes[-1].get_xlabel() == "Beats"
        assert figure.axes[0].get_shared_x_axes().joined(*figure.axes)
        figure.canvas.draw()
    finally:
        figure.clear()


@pytest.mark.parametrize("panels", [(), (PianoRollPanel("empty", ()),)])
def test_empty_views(panels: tuple[PianoRollPanel, ...], tmp_path: Path) -> None:
    roll = PianoRoll(120, panels)
    figure = piano_roll.build_figure(roll)
    try:
        axes = figure.axes[0]
        assert axes.get_xlim() == (0, 1)
        assert axes.get_xlabel() == "Beats"
        if not panels:
            assert any(t.get_text() == "No authored notes" for t in axes.texts)
        else:
            assert axes.get_title(loc="left") == "empty"
    finally:
        figure.clear()
    path = tmp_path / "empty.png"
    piano_roll.write_png(roll, path)
    assert image.imread(path).size > 0


def test_png_and_io_failure(tmp_path: Path) -> None:
    roll = project_piano_roll(song().graph())
    path = tmp_path / "notes.png"
    piano_roll.write_png(roll, path)
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert image.imread(path).size > 0
    with pytest.raises(FileNotFoundError):
        piano_roll.write_png(roll, tmp_path / "missing" / "notes.png")
    assert not (tmp_path / "missing").exists()


def test_writer_clears_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    roll = PianoRoll(120, ())
    figure = piano_roll.build_figure(roll)
    monkeypatch.setattr(piano_roll, "build_figure", lambda _: figure)
    with pytest.raises(FileNotFoundError):
        piano_roll.write_png(roll, tmp_path / "absent" / "notes.png")
    assert not figure.axes


def test_builder_clears_partial_figure(monkeypatch: pytest.MonkeyPatch) -> None:
    figure = Figure()
    monkeypatch.setattr(piano_roll, "Figure", lambda **kwargs: figure)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("construction failed")

    monkeypatch.setattr(figure, "tight_layout", fail)
    with pytest.raises(RuntimeError, match="construction failed"):
        piano_roll.build_figure(PianoRoll(120, ()))
    assert not figure.axes
    assert not figure.texts


def test_headless_without_plugins(tmp_path: Path) -> None:
    code = """
import sys
from pathlib import Path
from poser.core.piano_roll import PianoRoll
from poser.adapters.piano_roll import build_figure, write_png
roll = PianoRoll(120, ())
figure = build_figure(roll)
assert figure.axes
figure.clear()
write_png(roll, Path(sys.argv[1]))
assert "matplotlib.pyplot" not in sys.modules
assert "pedalboard" not in sys.modules
assert not any(name.startswith("poser.adapters.pedalboard") for name in sys.modules)
"""
    env = {key: value for key, value in os.environ.items() if key != "DISPLAY"}
    env["MPLBACKEND"] = "Agg"
    subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "headless.png")],
        check=True,
        env=env,
        timeout=30,
    )


def test_a_fractional_pitch_draws_at_its_exact_height(tmp_path: Path) -> None:
    source = Song(100, (Track("lead", "synth", (Note(0, 60.5, 1, 100), Note(1, 64, 1, 90))),))
    roll = project_piano_roll(source.graph())

    assert [note.pitch for note in roll.panels[0].notes] == [60.5, 64]
    path = tmp_path / "quarter.png"
    piano_roll.write_png(roll, path)
    assert image.imread(path).size > 0
