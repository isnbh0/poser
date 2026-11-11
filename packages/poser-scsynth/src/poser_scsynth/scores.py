"""OSC scores for `scsynth -N`: bundles, the NRT command file, and a performance as a score.

Layouts verified against scsynth 3.14's reader (`SC_World.cpp`, `nextOSCPacket`) and a real
render: the command file is a sequence of `(int32 length, OSC bundle)` records, both the length
and every OSC field big-endian, and the schedule time is the bundle's own OSC timetag — a
32.32 fixed-point count of seconds. The engine ignores a message whose typetags it cannot use,
and an `/n_set` naming a control the synth has, or has not, is silently out of range or
ignored, so a score that is total by construction stays total in the engine.

The score is total for every performance: unknown pitches still produce a `freq`, unmapped
controllers drop, and unmatched note-offs drop. Equal performances give equal bytes, so the
engine sees a deterministic input.
"""

import math
import struct
from collections.abc import Sequence

from poser.core.performance import Control, NoteOff, NoteOn, Performance

FIRST_NODE_ID = 1000
_BLOCK = 64  # The engine's default block size; NRT output runs in whole blocks.


def _osc_string(text: str) -> bytes:
    """Return `text` as an OSC string: nul-terminated, padded to a multiple of four bytes."""
    raw = text.encode() + b"\0"
    return raw + b"\0" * (-len(raw) % 4)


def _int32(value: int) -> bytes:
    return struct.pack(">i", value)


def _float32(value: float) -> bytes:
    return struct.pack(">f", value)


def _blob(data: bytes) -> bytes:
    padded = data + b"\0" * (-len(data) % 4)
    return _int32(len(data)) + padded


def _timetag(seconds: float) -> bytes:
    """Return an OSC timetag holding `seconds` as 32.32 fixed point.

    NRT scheduling reads only differences of timetags, so no epoch is added. Rounding to the
    timetag's resolution keeps the order of two times a sample apart at any song rate.
    """
    total = round(seconds * 2**32)
    return struct.pack(">II", total >> 32, total & 0xFFFFFFFF)


def _message(address: str, typetags: str, args: Sequence[bytes]) -> bytes:
    return _osc_string(address) + _osc_string("," + typetags) + b"".join(args)


def osc_bundle(seconds: float, messages: Sequence[bytes]) -> bytes:
    """Return one OSC bundle at `seconds` holding `messages`."""
    body = b"#bundle\0" + _timetag(seconds)
    for message in messages:
        body += _int32(len(message)) + message
    return body


def command_file(records: Sequence[tuple[float, bytes]]) -> bytes:
    """Return the NRT command file for `(seconds, bundle)` records.

    Each record is its bundle's byte length as a big-endian int32, then the bundle, exactly what
    `nextOSCPacket` reads.
    """
    parts: list[bytes] = []
    for _, bundle in records:
        parts.append(_int32(len(bundle)))
        parts.append(bundle)
    return b"".join(parts)


def _recv_synthdef(synthdef: bytes) -> bytes:
    return _message("/d_recv", "b", [_blob(synthdef)])


def _new_node(name: str, node_id: int, freq: float, amp: float) -> bytes:
    args = [
        _osc_string(name),
        _int32(node_id),
        _int32(0),  # Add action 0: a child of the default group.
        _int32(0),  # The default group.
        _osc_string("freq"),
        _float32(freq),
        _osc_string("amp"),
        _float32(amp),
        _osc_string("gate"),
        _float32(1.0),
    ]
    return _message("/s_new", "siii" + "sfsfsf", args)


def _set_node(node_id: int, pairs: Sequence[tuple[str, float]]) -> bytes:
    args: list[bytes] = [_int32(node_id)]
    for name, value in pairs:
        args.append(_osc_string(name))
        args.append(_float32(value))
    return _message("/n_set", "i" + "sf" * len(pairs), args)


def hz(pitch: int) -> float:
    """Return a MIDI pitch's frequency: twelve-tone equal temperament, MIDI 69 = 440 Hz."""
    return 440.0 * 2.0 ** ((pitch - 69) / 12.0)


def build_score(
    synthdef: bytes, meta: dict[str, object], performance: Performance, sample_rate: int
) -> bytes:
    """Return the NRT command file that plays `performance` on the synthdef.

    The synthdef is received at time 0 under the name `poser`. Every `NoteOn` opens a node whose
    id counts from 1000 in event order; a `NoteOff` closes the most recent still-open node of its
    pitch, and one with no open node drops. A mapped controller goes to the most recent
    still-open node and drops when none is open; an unmapped controller drops. A trailing empty
    bundle past the last frame keeps the engine running past the song's end.
    """
    mapping = meta.get("cc")
    controls = mapping if isinstance(mapping, dict) else {}
    bundles: list[tuple[float, bytes]] = [(0.0, osc_bundle(0.0, [_recv_synthdef(synthdef)]))]
    opened: dict[int, list[int]] = {}
    next_id = FIRST_NODE_ID
    for event in performance.events:
        seconds = event.sample / sample_rate
        match event:
            case NoteOn():
                node_id = next_id
                next_id += 1
                opened.setdefault(event.pitch, []).append(node_id)
                note = _new_node("poser", node_id, hz(event.pitch), event.velocity / 127)
                bundles.append((seconds, osc_bundle(seconds, [note])))
            case NoteOff():
                if stack := opened.get(event.pitch):
                    release = _set_node(stack.pop(), [("gate", 0.0)])
                    bundles.append((seconds, osc_bundle(seconds, [release])))
            case Control():
                name = controls.get(str(event.control))
                live = [node for stack in opened.values() for node in stack]
                if isinstance(name, str) and name and live:
                    change = _set_node(max(live), [(name, event.value / 127)])
                    bundles.append((seconds, osc_bundle(seconds, [change])))
    end = performance.samples / sample_rate + 1
    bundles.append((end, osc_bundle(end, [])))
    return command_file(bundles)


def output_frames(performance_samples: int, sample_rate: int) -> int:
    """Return the frame count `scsynth -N` writes for a score ending at `performance_samples`.

    The engine runs in whole blocks past the last timetag and then one more block, so the file is
    longer than the performance. The adapter truncates; this is the stub's length too.
    """
    last = performance_samples / sample_rate
    blocks = math.ceil(last * sample_rate / _BLOCK)
    return (blocks + 1) * _BLOCK
