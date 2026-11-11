#!/usr/bin/env python3
"""A stub `scsynth` executable for machine-independent adapter tests.

It mimics `scsynth -N cmd _ out rate header fmt [-o channels]`: it validates the command file
against the real NRT record layout — `(int32 length, bundle)` with the schedule time in the
bundle's timetag — and writes a deterministic IEEE-float WAV of the engine's length, one block
past the score's end. Environment variables stage failures, mirroring what scsynth 3.14 does
and does not do:

- `SCSTUB_EXIT=7` — print a message to stderr and exit 7 (a crashed engine).
- `SCSTUB_SLEEP=2` — sleep N seconds past any timeout before writing (a hung engine).
- `SCSTUB_SHORT=1` — write one block instead of the score's full length (padding exercise).
- `SCSTUB_NAN=1` — write NaN into the first block (the adapter owns finiteness).

The real engine's behaviors mirrored here: malformed records are a loud failure; a score whose
messages are unusable still renders silence silently.
"""

import math
import os
import struct
import sys
import time
from pathlib import Path

BLOCK = 64


def timetag_seconds(bundle: bytes) -> float:
    if bundle[:8] != b"#bundle\0":
        raise ValueError(f"record is not a bundle: {bundle[:8]!r}")
    seconds, fraction = struct.unpack(">II", bundle[8:16])
    return (seconds << 32 | fraction) / 2**32


def read_records(path: Path) -> list[float]:
    """Validate the command file's records and return the bundle timetags, in file order."""
    data = path.read_bytes()
    records = []
    position = 0
    while position < len(data):
        if len(data) - position < 4:
            raise ValueError("truncated record length")
        (length,) = struct.unpack(">i", data[position : position + 4])
        if length < 16:  # "#bundle\0" + timetag.
            raise ValueError(f"bad record length {length}")
        position += 4
        if len(data) - position < length:
            raise ValueError(
                f"truncated record: {length} bytes claimed, {len(data) - position} left"
            )
        records.append(timetag_seconds(data[position : position + length]))
        position += length
    if not records:
        raise ValueError("command file is empty")
    return records


def wav_bytes(rate: int, channels: int, frames: int) -> bytes:
    """Return an IEEE-float WAV of `frames` frames, one sine in every channel."""
    fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * channels * 4, channels * 4, 32)
    data = bytearray()
    for frame in range(frames):
        value = math.sin(2 * math.pi * 440 * frame / rate) * 0.25
        if os.environ.get("SCSTUB_NAN") == "1" and frame < BLOCK:
            value = math.nan
        data += struct.pack("<f", value) * channels
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + bytes(data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def main(argv: list[str]) -> int:
    mode = os.environ.get("SCSTUB_EXIT")
    if mode:
        print("stub was told to die this way", file=sys.stderr)
        return int(mode)
    if sleep := os.environ.get("SCSTUB_SLEEP"):
        time.sleep(float(sleep))
    if "-N" not in argv:
        print("stub: no -N in argv", file=sys.stderr)
        return 1
    at = argv.index("-N")
    score_path, input_path, output_path, rate, header, fmt = argv[at + 1 : at + 7]
    if input_path != "_":
        print("stub: expected _ for the input file", file=sys.stderr)
        return 1
    if header != "WAV" or fmt != "float":
        print(f"stub: only WAV float is supported, got {header} {fmt}", file=sys.stderr)
        return 1
    try:
        timetags = read_records(Path(score_path))
    except ValueError as error:
        print(f"stub: bad command file: {error}", file=sys.stderr)
        return 1
    frames = round(timetags[-1] * int(rate) / BLOCK) * BLOCK + BLOCK
    if os.environ.get("SCSTUB_SHORT") == "1":
        frames = BLOCK
    Path(output_path).write_bytes(wav_bytes(int(rate), 2, frames))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
