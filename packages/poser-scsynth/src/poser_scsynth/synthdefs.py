"""SCgf v2 synth definitions, emitted as bytes, and the shipped demo voice.

The emitter writes the file format scsynth's reader (`GraphDef_Read`) demands: everything
big-endian, int32 counts for constants, parameters, parameter names, and unit generators, int16
for the file's definition count and the variant count, and one output-rate byte per output. The
bytes hold no timestamps and follow a fixed parameter order, so one definition always gives one
byte string — the identity the adapter digests.

The demo voice is a stereo additive bank: six sine partials at multiples of the note's
frequency, fixed relative amplitudes, a per-note Linen envelope with a gate release, and both
channels fed the same signal. No plugin makes this sound; that is the point of the adapter.
"""

import struct
from dataclasses import dataclass

AUDIO_RATE = 2
CONTROL_RATE = 1
SCALAR_RATE = 0

DONE_FREE_SELF = 2

# The demo voice's shape, in parameter order and partial order. Partials are integer multiples
# of the note frequency with fixed relative amplitudes — an organ-like additive bank.
VOICE_NAME = "additive-voice"
VOICE_PARTIALS: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
VOICE_AMPLITUDES: tuple[float, ...] = (1.0, 0.55, 0.35, 0.22, 0.14, 0.08)
VOICE_ATTACK_S = 0.005
VOICE_RELEASE_S = 0.15


@dataclass(frozen=True)
class Input:
    """One UGen input: a UGen output, or a constant when `ugen` is `CONSTANT`."""

    ugen: int
    output: int


CONSTANT = -1


@dataclass(frozen=True)
class UGen:
    """One unit generator: its class, rate, special index, inputs, and output rates."""

    name: str
    rate: int
    inputs: tuple[Input, ...]
    outputs: tuple[int, ...] = (AUDIO_RATE,)
    special: int = 0


@dataclass(frozen=True)
class SynthDef:
    """One synth definition: constants, parameters, their names, and its unit generators."""

    name: str
    constants: tuple[float, ...]
    params: tuple[float, ...]
    param_names: tuple[tuple[str, int], ...]
    ugens: tuple[UGen, ...]


class _Constants:
    """A constants table that reuses equal values, so no byte is written twice."""

    def __init__(self) -> None:
        self._values: list[float] = []
        self._index: dict[float, int] = {}

    def push(self, value: float) -> int:
        if value in self._index:
            return self._index[value]
        self._values.append(value)
        self._index[value] = len(self._values) - 1
        return len(self._values) - 1

    def all(self) -> tuple[float, ...]:
        return tuple(self._values)


def _pstring(text: str) -> bytes:
    raw = text.encode()
    if len(raw) > 255:
        raise ValueError(f"a pstring holds at most 255 bytes, got {len(raw)}")
    return bytes([len(raw)]) + raw


def emit(synthdef: SynthDef) -> bytes:
    """Return `synthdef` as SCgf v2 bytes, version 2, one definition, no variants."""
    out = bytearray()
    out += b"SCgf"
    out += struct.pack(">i", 2)
    out += struct.pack(">h", 1)
    out += _pstring(synthdef.name)
    out += struct.pack(">i", len(synthdef.constants))
    for value in synthdef.constants:
        out += struct.pack(">f", value)
    out += struct.pack(">i", len(synthdef.params))
    for value in synthdef.params:
        out += struct.pack(">f", value)
    out += struct.pack(">i", len(synthdef.param_names))
    for name, index in synthdef.param_names:
        out += _pstring(name) + struct.pack(">i", index)
    out += struct.pack(">i", len(synthdef.ugens))
    for ugen in synthdef.ugens:
        out += _pstring(ugen.name)
        out += struct.pack(">biih", ugen.rate, len(ugen.inputs), len(ugen.outputs), ugen.special)
        for wire in ugen.inputs:
            out += struct.pack(">ii", wire.ugen, wire.output)
        for rate in ugen.outputs:
            out += struct.pack(">b", rate)
    out += struct.pack(">h", 0)  # No variants.
    return bytes(out)


def additive_voice() -> SynthDef:
    """Return the shipped demo voice: a six-partial additive bank with a gate envelope."""
    constants = _Constants()
    zero = constants.push(0.0)  # SinOsc phase; the output bus.
    attack = constants.push(VOICE_ATTACK_S)
    sus = constants.push(1.0)
    release = constants.push(VOICE_RELEASE_S)
    done = constants.push(float(DONE_FREE_SELF))

    ugens: list[UGen] = [
        UGen("Control", CONTROL_RATE, (), (CONTROL_RATE, CONTROL_RATE, CONTROL_RATE))
    ]
    scaled: list[int] = []  # The UGen holding each partial at its relative amplitude.
    for partial, amplitude in zip(VOICE_PARTIALS, VOICE_AMPLITUDES, strict=True):
        multiple = constants.push(float(partial))
        ratio = constants.push(amplitude)
        frequency = len(ugens)
        # The partial's multiple of the note frequency is a control-rate product: control-rate
        # inputs into an audio-rate operator read as zero, so the multiply must run at the
        # control rate, exactly as the language's own compiler emits it.
        ugens.append(
            UGen("BinaryOpUGen", CONTROL_RATE, (Input(0, 0), Input(CONSTANT, multiple)), special=2)
        )
        oscillator = len(ugens)
        ugens.append(UGen("SinOsc", AUDIO_RATE, (Input(frequency, 0), Input(CONSTANT, zero))))
        scaled.append(len(ugens))
        ugens.append(
            UGen(
                "BinaryOpUGen",
                AUDIO_RATE,
                (Input(oscillator, 0), Input(CONSTANT, ratio)),
                special=2,
            )
        )
    total = scaled[0]
    for other in scaled[1:]:
        addition = len(ugens)
        ugens.append(
            UGen("BinaryOpUGen", AUDIO_RATE, (Input(total, 0), Input(other, 0)), special=0)
        )
        total = addition
    envelope = len(ugens)
    # The envelope runs at the control rate, as the language's own compiler emits it: scsynth's
    # audio-rate Linen misreads its gate in non-realtime renders, while a control-rate Linen's
    # output feeds the audio-rate multiply as an ordinary control wire.
    ugens.append(
        UGen(
            "Linen",
            CONTROL_RATE,
            (
                Input(0, 2),
                Input(CONSTANT, attack),
                Input(CONSTANT, sus),
                Input(CONSTANT, release),
                Input(CONSTANT, done),
            ),
            (CONTROL_RATE,),
        )
    )
    shaped = len(ugens)
    ugens.append(UGen("BinaryOpUGen", AUDIO_RATE, (Input(total, 0), Input(envelope, 0)), special=2))
    final = len(ugens)
    ugens.append(UGen("BinaryOpUGen", AUDIO_RATE, (Input(shaped, 0), Input(0, 1)), special=2))
    ugens.append(
        UGen(
            "Out",
            AUDIO_RATE,
            # One bus input, then each channel's signal: both channels carry the same signal.
            (Input(CONSTANT, zero), Input(final, 0), Input(final, 0)),
            (),
        )
    )
    return SynthDef(
        VOICE_NAME,
        constants.all(),
        (440.0, 0.5, 1.0),
        (("freq", 0), ("amp", 1), ("gate", 2)),
        tuple(ugens),
    )


VOICE_BYTES = emit(additive_voice())
VOICE_META: dict[str, object] = {"cc": {}}
