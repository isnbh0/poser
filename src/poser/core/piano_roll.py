"""Immutable views of authored notes, independent of routing and rendered audio."""

from dataclasses import dataclass

from poser.core.graph import Graph
from poser.core.rules import (
    NODE_NAME,
    Note,
    duplicate_name_problems,
    is_number,
    song_field_problems,
)


@dataclass(frozen=True)
class PianoRollPanel:
    """One named note panel. Invalid names or note collections raise ValueError."""

    name: str
    notes: tuple[Note, ...]

    def __post_init__(self) -> None:
        if not (isinstance(self.name, str) and NODE_NAME.fullmatch(self.name)):
            raise ValueError(f"Invalid panel name: {self.name!r}")
        if not isinstance(self.notes, list | tuple):
            raise ValueError("Panel notes must be a list or tuple of Note values")
        for note in self.notes:
            if not isinstance(note, Note):
                raise ValueError("Panel notes must contain only Note values")
            if not is_number(note.beat + note.length):
                raise ValueError("Note end must be finite")
        object.__setattr__(self, "notes", tuple(self.notes))


@dataclass(frozen=True)
class PianoRoll:
    """Authored events at a tempo; invalid tempo or panels raise ValueError.

    Lists are accepted and copied to tuples. Panel names are unique ignoring case,
    as in songs and graphs. Empty panels and an empty view are valid.
    """

    tempo: float
    panels: tuple[PianoRollPanel, ...]

    def __post_init__(self) -> None:
        if problems := song_field_problems({"bpm": self.tempo}, "PianoRoll"):
            raise ValueError("\n".join(problems))
        if not isinstance(self.panels, list | tuple):
            raise ValueError("Panels must be a list or tuple of PianoRollPanel values")
        if any(not isinstance(panel, PianoRollPanel) for panel in self.panels):
            raise ValueError("Panels must contain only PianoRollPanel values")
        if problems := duplicate_name_problems(self.panels, "PianoRoll", group="panels"):
            raise ValueError("\n".join(problems))
        object.__setattr__(self, "panels", tuple(self.panels))


def project_piano_roll(graph: Graph) -> PianoRoll:
    """Project the track nodes' authored notes without consulting instruments."""
    tracks = (node for node in graph.nodes if node.kind == "track")
    return PianoRoll(graph.bpm, tuple(PianoRollPanel(node.name, node.notes) for node in tracks))
