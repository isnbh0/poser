"""A performance as a deterministic scsynth NRT score.

The command file contains big-endian `(int32 byte length, OSC bundle)` records. Each bundle
owns its 32.32 fixed-point timetag; there is no separate timestamp field. Message typetags are
derived from the argument values, never supplied independently.
"""

import struct
from collections.abc import Sequence

from poser.core.jsonvalue import Json
from poser.core.performance import Control, NoteOff, NoteOn, Performance
from poser_scsynth.formats import SoundRecipe

FIRST_NODE_ID = 1000


def _osc_string(text: str) -> bytes:
    raw = text.encode() + b"\0"
    return raw + b"\0" * (-len(raw) % 4)


def _int32(value: int) -> bytes:
    return struct.pack(">i", value)


def _message(address: str, args: Sequence[int | float | str | bytes]) -> bytes:
    tags: list[str] = []
    encoded: list[bytes] = []
    for arg in args:
        match arg:
            case int():
                tags.append("i")
                encoded.append(_int32(arg))
            case float():
                tags.append("f")
                encoded.append(struct.pack(">f", arg))
            case str():
                tags.append("s")
                encoded.append(_osc_string(arg))
            case bytes():
                tags.append("b")
                encoded.append(_int32(len(arg)) + arg + b"\0" * (-len(arg) % 4))
    return _osc_string(address) + _osc_string("," + "".join(tags)) + b"".join(encoded)


def osc_bundle(seconds: float, messages: Sequence[bytes]) -> bytes:
    """Return one OSC bundle; NRT uses relative 32.32 seconds, not an absolute NTP epoch."""
    ticks = round(seconds * 2**32)
    body = b"#bundle\0" + struct.pack(">II", ticks >> 32, ticks & 0xFFFFFFFF)
    return body + b"".join(_int32(len(message)) + message for message in messages)


def command_file(bundles: Sequence[bytes]) -> bytes:
    """Frame bundles exactly as scsynth's `SC_World.cpp:nextOSCPacket` reads them."""
    return b"".join(_int32(len(bundle)) + bundle for bundle in bundles)


def hz(pitch: float) -> float:
    """The MIDI pitch scale: 69 = 440 Hz, and 1 = a twelve-tone equal-tempered semitone."""
    return 440.0 * 2.0 ** ((pitch - 69) / 12.0)


def build_score(
    synthdef: bytes, meta: dict[str, object], performance: Performance, sample_rate: int
) -> bytes:
    """Check a raw artifact at the boundary and compile its performance to a command file."""
    return compile_score(SoundRecipe(synthdef, Json.of(meta)), performance, sample_rate)


def compile_score(recipe: SoundRecipe, performance: Performance, sample_rate: int) -> bytes:
    """Compile an already-checked recipe, without any engine or filesystem access.

    Notes own distinct ids; note-offs release per-pitch LIFO by exact pitch, so 69.5 never
    releases a 69 node; unmatched offs drop. Controllers
    are track-wide state: every mapped change sets the root group (all existing voices, even
    during release), and is latched into every future note. Thus controls before simultaneous
    note-ons and during silent gaps have the same meaning as the core's automation timeline.
    Unmapped controls drop. The final empty bundle runs the engine past the stem's last frame.
    """
    bundles = [osc_bundle(0.0, [_message("/d_recv", [recipe.synthdef])])]
    mapping = dict(recipe.controls)
    current: dict[str, float] = {}
    opened: dict[float, list[int]] = {}
    next_id = FIRST_NODE_ID
    for event in performance.events:
        seconds = event.sample / sample_rate
        match event:
            case NoteOn():
                node_id = next_id
                next_id += 1
                opened.setdefault(event.pitch, []).append(node_id)
                args: list[int | float | str | bytes] = [
                    recipe.name,
                    node_id,
                    0,
                    0,
                    "freq",
                    hz(event.pitch),
                    "amp",
                    event.velocity / 127,
                    "gate",
                    1.0,
                ]
                for param, value in current.items():
                    args.extend([param, value])
                bundles.append(osc_bundle(seconds, [_message("/s_new", args)]))
            case NoteOff():
                if stack := opened.get(event.pitch):
                    release = _message("/n_set", [stack.pop(), "gate", 0.0])
                    bundles.append(osc_bundle(seconds, [release]))
            case Control():
                if param := mapping.get(event.control):
                    value = event.value / 127
                    current[param] = value
                    # /n_set on a group applies to its descendant synths. Setting group 0 is
                    # valid even before the first note and never addresses a freed node id.
                    bundles.append(osc_bundle(seconds, [_message("/n_set", [0, param, value])]))
    bundles.append(osc_bundle(performance.samples / sample_rate + 1, []))
    return command_file(bundles)
