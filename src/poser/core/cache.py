"""Cache identity: the keys that decide when a stem is current."""

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE, STEM_BIT_DEPTH, TAIL_S, song_duration_s
from poser.core.jsonvalue import digest
from poser.core.song import Song, Track
from poser.core.statuses import StemStatus

# Raise whenever rendering code changes what a stem contains for the same inputs, so every
# existing stamp stops matching.
RENDER_FORMAT = 2

_KEY = re.compile(r"[0-9a-f]{64}")


def track_key(song: Song, track: Track) -> str:
    """Return a digest of everything from the song that reaches the track's instrument.

    Gain is left out, because it applies only at mixdown. The song's duration is in, because every
    stem shares it. Numbers are normalized so that `1` and `1.0` give the same key.
    """
    return digest(
        {
            "format": RENDER_FORMAT,
            "sample_rate": SAMPLE_RATE,
            "channels": NUM_CHANNELS,
            "bit_depth": STEM_BIT_DEPTH,
            "tail_s": TAIL_S,
            "bpm": float(song.bpm),
            "duration_s": song_duration_s(song),
            "name": track.name,
            "sound": track.sound,
            "notes": [
                [float(note.beat), int(note.pitch), float(note.length), int(note.velocity)]
                for note in track.notes
            ],
            "cc": [
                [float(change.beat), int(change.control), int(change.value), change.shape]
                for change in track.cc
            ],
        }
    )


def sound_key(adapter_id: str, adapter_version: str, identity: str) -> str:
    """Return a digest of a resolved sound, as its instrument adapter identifies it."""
    if not adapter_version:
        raise ValueError("sound_key needs a non-empty adapter version")
    return digest({"adapter": adapter_id, "version": adapter_version, "identity": identity})


@dataclass(frozen=True)
class Stamp:
    """The inputs a stem was rendered from, and what the render measured."""

    track_key: str
    sound_key: str
    status: StemStatus
    peak_dbfs: float

    def __post_init__(self) -> None:
        for field in ("track_key", "sound_key"):
            key = getattr(self, field)
            if not (isinstance(key, str) and _KEY.fullmatch(key)):
                raise ValueError(f"Stamp.{field} must be 64 lowercase hex digits, got {key!r}")
        if not isinstance(self.status, StemStatus):
            raise ValueError(f"Stamp.status must be a StemStatus, got {self.status!r}")
        peak = self.peak_dbfs
        if not (isinstance(peak, int | float) and not isinstance(peak, bool)) or not (
            math.isfinite(peak)
        ):
            raise ValueError(f"Stamp.peak_dbfs must be a finite number, got {peak!r}")


class StampProblem(StrEnum):
    """Why a stem's stamp does not vouch for it."""

    MISSING = "stem has no render stamp"
    CHANGED = "stem changed after it was rendered"
