"""Headless PNG drawing of authored note events, not sounding or rendered duration."""

from collections.abc import Mapping
from pathlib import Path

from matplotlib import colormaps
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle

from poser.adapters.loading import refuse_options
from poser.core.jsonvalue import JsonValue
from poser.core.piano_roll import PianoRoll
from poser.core.ports import AdapterContext


def build_figure(roll: PianoRoll) -> Figure:
    """Return a live caller-owned figure; the caller must clear it when finished."""
    figure = Figure(figsize=(10, 2.8 * max(1, len(roll.panels)) + 0.8))
    try:
        FigureCanvasAgg(figure)
        figure.suptitle(f"Authored notes — {roll.tempo:g} BPM")
        figure.text(0.5, 0.02, "Velocity: higher velocity = greater opacity", ha="center")
        end = max((note.beat + note.length for p in roll.panels for note in p.notes), default=1)
        first = None
        for index in range(max(1, len(roll.panels))):
            axes = figure.add_subplot(max(1, len(roll.panels)), 1, index + 1, sharex=first)
            if first is None:
                first = axes
            axes.set_xlim(0, end)
            axes.set_ylabel("MIDI pitch")
            if index == max(1, len(roll.panels)) - 1:
                axes.set_xlabel("Beats")
            if not roll.panels:
                axes.set_ylim(48, 72)
                axes.text(0.5, 0.5, "No authored notes", transform=axes.transAxes, ha="center")
                continue
            panel = roll.panels[index]
            axes.set_title(panel.name, loc="left")
            pitches = [note.pitch for note in panel.notes]
            axes.set_ylim(min(pitches) - 1 if pitches else 48, max(pitches) + 1 if pitches else 72)
            color = colormaps["tab20"](index % 20)
            for beat in roll.bars:
                axes.axvline(beat, color="0.8", linewidth=0.6, zorder=0)
            for note in panel.notes:
                axes.add_patch(
                    Rectangle(
                        (note.beat, note.pitch - 0.5),
                        note.length,
                        1,
                        facecolor=color,
                        alpha=0.25 + 0.75 * note.velocity / 127,
                    )
                )
        figure.tight_layout(rect=(0, 0.05, 1, 0.95))
    except BaseException:
        figure.clear()
        raise
    return figure


def write_png(roll: PianoRoll, path: Path) -> None:
    """Write to an explicit path, propagating I/O errors and always clearing our figure."""
    figure = build_figure(roll)
    try:
        figure.savefig(path, format="png")
    finally:
        figure.clear()


class PianoRollDrawer:
    """Draw authored notes to a caller-selected PNG path."""

    def draw(self, roll: PianoRoll, path: Path) -> None:
        write_png(roll, path)


def build_piano_roll_drawer(
    options: Mapping[str, JsonValue], _context: AdapterContext
) -> PianoRollDrawer:
    """Build the plugin-free drawer; no options are accepted."""
    refuse_options(options)
    return PianoRollDrawer()
