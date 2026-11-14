"""Cache identity: render inputs, and the keys that digest them.

A render input holds exactly what one render unit's renderer passes on: a track's resolved sound
and performance, an effect's recipe and the stems fed into it, a delay's line and its inputs, or a
cycle group's members, inner wires, and external feeds. A unit's key is the digest of its input,
so a key covers everything its render reads and nothing else.
"""

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE, STEM_BIT_DEPTH
from poser.core.graph import Wire, WireKind
from poser.core.jsonvalue import JsonValue, digest
from poser.core.names import NodeName
from poser.core.performance import Performance, performance_to_data
from poser.core.statuses import StemStatus

# Raise whenever rendering code changes what a stem contains for the same inputs, so every
# existing key stops matching.
RENDER_FORMAT = 3

_KEY = re.compile(r"[0-9a-f]{64}")


def _format() -> JsonValue:
    """The stem format every key digests, so a stem written in another format never matches."""
    return {
        "render_format": RENDER_FORMAT,
        "sample_rate": SAMPLE_RATE,
        "channels": NUM_CHANNELS,
        "bit_depth": STEM_BIT_DEPTH,
    }


def _check_key(owner: str, value: object) -> None:
    if not (isinstance(value, str) and _KEY.fullmatch(value)):
        raise ValueError(f"{owner} must be 64 lowercase hex digits, got {value!r}")


def _count(owner: str, value: object, low: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise ValueError(f"{owner} must be an integer of at least {low}, got {value!r}")
    return value


def _tagged(kind: str, **fields: JsonValue) -> JsonValue:
    return {"kind": kind, "format": _format(), **fields}


def sound_key(adapter_id: str, adapter_version: str, identity: str) -> str:
    """Return a digest of a resolved sound or recipe, as its adapter identifies it."""
    if not adapter_version:
        raise ValueError("sound_key needs a non-empty adapter version")
    return digest({"adapter": adapter_id, "version": adapter_version, "identity": identity})


@dataclass(frozen=True)
class StemRef:
    """One upstream stem a unit reads: the upstream unit's key, the stem's position among that
    unit's nodes, and the gain of the wire carrying it. A control wire carries no gain."""

    unit_key: str
    index: int
    gain_db: float = 0.0

    def __post_init__(self) -> None:
        _check_key("StemRef.unit_key", self.unit_key)
        _count("StemRef.index", self.index, 0)
        gain = self.gain_db
        if isinstance(gain, bool) or not isinstance(gain, int | float) or not math.isfinite(gain):
            raise ValueError(f"StemRef.gain_db must be a finite number, got {gain!r}")
        # Normalized, so `1` and `1.0` give one key.
        object.__setattr__(self, "gain_db", float(gain))

    def to_data(self) -> JsonValue:
        return [self.unit_key, self.index, self.gain_db]


@dataclass(frozen=True)
class TrackInput:
    """A track's render input: its resolved sound's `sound_key`, and the performance its
    instrument plays, which holds the stem length and every event snapped to samples."""

    sound: str
    performance: Performance

    def __post_init__(self) -> None:
        _check_key("TrackInput.sound", self.sound)
        if not isinstance(self.performance, Performance):
            raise ValueError(
                f"TrackInput.performance must be a Performance, got {self.performance!r}"
            )

    def to_data(self) -> JsonValue:
        return _tagged("track", sound=self.sound, performance=performance_to_data(self.performance))


@dataclass(frozen=True)
class EffectInput:
    """An effect's render input: its recipe's `sound_key`, the stem length, its audio inputs in
    wire order, and its one control input, if any."""

    recipe: str
    frames: int
    inputs: tuple[StemRef, ...]
    control: StemRef | None = None

    def __post_init__(self) -> None:
        _check_key("EffectInput.recipe", self.recipe)
        _count("EffectInput.frames", self.frames, 1)
        object.__setattr__(self, "inputs", _refs("EffectInput.inputs", self.inputs))
        if self.control is not None and not isinstance(self.control, StemRef):
            raise ValueError(f"EffectInput.control must be a StemRef, got {self.control!r}")

    def to_data(self) -> JsonValue:
        return _tagged(
            "effect",
            recipe=self.recipe,
            frames=self.frames,
            inputs=[ref.to_data() for ref in self.inputs],
            control=None if self.control is None else self.control.to_data(),
        )


@dataclass(frozen=True)
class DelayInput:
    """A delay's render input: the samples its line delays by, the stem length, and its audio
    inputs in wire order."""

    delay_samples: int
    frames: int
    inputs: tuple[StemRef, ...]

    def __post_init__(self) -> None:
        _count("DelayInput.delay_samples", self.delay_samples, 1)
        _count("DelayInput.frames", self.frames, 1)
        object.__setattr__(self, "inputs", _refs("DelayInput.inputs", self.inputs))

    def to_data(self) -> JsonValue:
        return _tagged(
            "delay",
            delay_samples=self.delay_samples,
            frames=self.frames,
            inputs=[ref.to_data() for ref in self.inputs],
        )


@dataclass(frozen=True)
class EffectMemberInput:
    """An effect member of a cycle group: its name and its recipe's `sound_key`."""

    name: NodeName
    recipe: str

    def __post_init__(self) -> None:
        _check_name("EffectMemberInput.name", self.name)
        _check_key("EffectMemberInput.recipe", self.recipe)

    def to_data(self) -> JsonValue:
        return {"name": self.name, "kind": "effect", "recipe": self.recipe}


@dataclass(frozen=True)
class DelayMemberInput:
    """A delay member of a cycle group: its name and the samples its line delays by."""

    name: NodeName
    delay_samples: int

    def __post_init__(self) -> None:
        _check_name("DelayMemberInput.name", self.name)
        _count("DelayMemberInput.delay_samples", self.delay_samples, 1)

    def to_data(self) -> JsonValue:
        return {"name": self.name, "kind": "delay", "delay_samples": self.delay_samples}


@dataclass(frozen=True)
class GroupFeed:
    """One wire into a cycle group from outside it: the member it feeds, the upstream stem, and
    the wire's kind."""

    member: NodeName
    ref: StemRef
    kind: WireKind

    def __post_init__(self) -> None:
        _check_name("GroupFeed.member", self.member)
        if not isinstance(self.ref, StemRef):
            raise ValueError(f"GroupFeed.ref must be a StemRef, got {self.ref!r}")
        if self.kind not in ("audio", "control"):
            raise ValueError(f"GroupFeed.kind must be audio or control, got {self.kind!r}")

    def to_data(self) -> JsonValue:
        return [self.member, self.ref.to_data(), self.kind]


@dataclass(frozen=True)
class GroupInput:
    """A cycle group's render input: the stem length, its members in name order, the wires
    between members, and every wire into the group from outside it, in wire order.

    Member names are in the input because the wires refer to them.
    """

    frames: int
    members: tuple[EffectMemberInput | DelayMemberInput, ...]
    wires: tuple[Wire, ...]
    inputs: tuple[GroupFeed, ...]

    def __post_init__(self) -> None:
        _count("GroupInput.frames", self.frames, 1)
        members = tuple(self.members)
        if not members or not all(
            isinstance(member, EffectMemberInput | DelayMemberInput) for member in members
        ):
            raise ValueError(f"GroupInput.members must hold members, got {self.members!r}")
        names = [member.name for member in members]
        if names != sorted(set(names)):
            raise ValueError(f"GroupInput.members must be distinct and in name order: {names}")
        wires = tuple(self.wires)
        inputs = tuple(self.inputs)
        for wire in wires:
            if not isinstance(wire, Wire) or {wire.from_name, wire.to_name} - set(names):
                raise ValueError(f"GroupInput.wires must join members, got {wire!r}")
        for feed in inputs:
            if not isinstance(feed, GroupFeed) or feed.member not in names:
                raise ValueError(f"GroupInput.inputs must feed members, got {feed!r}")
        object.__setattr__(self, "members", members)
        object.__setattr__(self, "wires", wires)
        object.__setattr__(self, "inputs", inputs)

    def to_data(self) -> JsonValue:
        return _tagged(
            "group",
            frames=self.frames,
            members=[member.to_data() for member in self.members],
            wires=[
                [wire.from_name, wire.to_name, wire.kind, float(wire.gain_db)]
                for wire in self.wires
            ],
            inputs=[feed.to_data() for feed in self.inputs],
        )


type RenderInput = TrackInput | EffectInput | DelayInput | GroupInput


def unit_key(render_input: RenderInput) -> str:
    """Return a render unit's key: the digest of its render input."""
    return digest(render_input.to_data())


def _refs(owner: str, value: object) -> tuple[StemRef, ...]:
    if not isinstance(value, tuple | list):
        raise ValueError(f"{owner} must be a tuple of StemRefs, got {value!r}")
    refs = tuple(value)
    if not all(isinstance(ref, StemRef) for ref in refs):
        raise ValueError(f"{owner} must hold only StemRefs, got {value!r}")
    return refs


def _check_name(owner: str, value: object) -> None:
    if not isinstance(value, NodeName):
        raise ValueError(f"{owner} must be a NodeName, got {value!r}")


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
