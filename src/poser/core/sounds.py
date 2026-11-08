"""Sound references: which instrument plays a track, and which of its sounds."""

from dataclasses import dataclass
from typing import override

from poser.core.names import Identifier


@dataclass(frozen=True)
class SoundRef:
    """An instrument, and optionally one of its sounds. The core never interprets `name`."""

    instrument: Identifier
    name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instrument, Identifier):
            raise ValueError(f"SoundRef.instrument must be an Identifier, got {self.instrument!r}")
        if not (self.name is None or (isinstance(self.name, str) and self.name)):
            raise ValueError(f"SoundRef.name must be None or non-empty text, got {self.name!r}")

    @override
    def __str__(self) -> str:
        return self.instrument if self.name is None else f"{self.instrument}:{self.name}"


def parse_sound_ref(text: str) -> SoundRef:
    """Return the reference `instrument` or `instrument:name`, split at the first `:`."""
    if not isinstance(text, str):
        raise ValueError(f"a sound reference must be text, got {text!r}")
    instrument, colon, name = text.partition(":")
    if colon and not name:
        raise ValueError(f"a sound reference must name a sound after ':', got {text!r}")
    return SoundRef(Identifier(instrument), name if colon else None)


@dataclass(frozen=True)
class SoundEntry:
    """A listed sound."""

    ref: SoundRef
    description: str

    def __post_init__(self) -> None:
        if not isinstance(self.ref, SoundRef):
            raise ValueError(f"SoundEntry.ref must be a SoundRef, got {self.ref!r}")
        if not (isinstance(self.description, str) and self.description):
            raise ValueError(
                f"SoundEntry.description must be non-empty text, got {self.description!r}"
            )
