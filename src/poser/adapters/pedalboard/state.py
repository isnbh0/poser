"""A plugin's `raw_state`: its framing under Pedalboard, and its base64 text form.

`raw_state` under Pedalboard is JUCE's binary XML form: `VC2!`, the XML text's length as a 4-byte
little-endian integer, the text, and a zero byte. The text holds the VST3 component state in JUCE's
`MemoryBlock` base64 form. Serum's component state is the same data as an `.fxp` chunk, as measured
with Serum 1.368 and Pedalboard 0.9.26. This module imports no plugin code.
"""

import base64
import binascii

STATE_MAGIC = b"VC2!"
STATE_XML_HEAD = b'<?xml version="1.0" encoding="UTF-8"?> <VST3PluginState><IComponent>'
STATE_XML_TAIL = b"</IComponent></VST3PluginState>"
JUCE_BASE64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"


def juce_base64_encode(data: bytes) -> str:
    """Encode as JUCE's `MemoryBlock::toBase64Encoding`: size, `.`, then 6 bits per character.

    The bits run from the first byte's lowest bit upward, so every 3 bytes make exactly 4
    characters. Working in 3-byte groups keeps the time linear for presets of a megabyte or more.
    """
    padded = data + b"\x00" * (-len(data) % 3)
    chars = []
    for start in range(0, len(padded), 3):
        value = int.from_bytes(padded[start : start + 3], "little")
        chars += [JUCE_BASE64[(value >> shift) & 63] for shift in (0, 6, 12, 18)]
    return f"{len(data)}." + "".join(chars[: (len(data) * 8 + 5) // 6])


def juce_base64_decode(text: str) -> bytes:
    """Decode JUCE's `MemoryBlock` base64, the inverse of `juce_base64_encode`."""
    size_text, dot, body = text.partition(".")
    if not dot:
        raise ValueError(f"no '.' in JUCE base64 text {text[:20]!r}")
    if not size_text.isdigit():
        raise ValueError(f"JUCE base64 size {size_text!r} is not a number")
    size = int(size_text)
    if len(body) * 6 < size * 8:
        raise ValueError(f"{len(body)} characters cannot hold {size} bytes")
    digits = []
    for char in body:
        digit = JUCE_BASE64.find(char)
        if digit < 0:
            raise ValueError(f"{char!r} is not a JUCE base64 character")
        digits.append(digit)
    digits += [0] * (-len(digits) % 4)
    out = bytearray()
    for start in range(0, len(digits), 4):
        a, b, c, d = digits[start : start + 4]
        out += (a | b << 6 | c << 12 | d << 18).to_bytes(3, "little")
    return bytes(out[:size])


def build_state(component: bytes) -> bytes:
    """Frame a VST3 component state as Pedalboard's `raw_state`."""
    xml = STATE_XML_HEAD + juce_base64_encode(component).encode("ascii") + STATE_XML_TAIL
    return STATE_MAGIC + len(xml).to_bytes(4, "little") + xml + b"\x00"


def component_state(raw_state: bytes) -> bytes:
    """Return the component state inside a `raw_state`, in exactly the framing `build_state`
    writes."""
    if raw_state[:4] != STATE_MAGIC:
        raise ValueError("raw_state does not start with VC2!")
    length = int.from_bytes(raw_state[4:8], "little")
    if len(raw_state) != 8 + length + 1 or raw_state[-1:] != b"\x00":
        raise ValueError("raw_state length does not match its header and zero byte")
    xml = raw_state[8:-1]
    if not (xml.startswith(STATE_XML_HEAD) and xml.endswith(STATE_XML_TAIL)):
        raise ValueError("raw_state XML is not a single IComponent state")
    text = xml[len(STATE_XML_HEAD) : -len(STATE_XML_TAIL)].decode("ascii", "replace")
    return juce_base64_decode(text)


def encode_state(data: bytes) -> str:
    """Return a `raw_state` as standard base64 text."""
    return base64.b64encode(data).decode("ascii")


def decode_state(text: object) -> bytes:
    """Return the `raw_state` in standard base64 text, raising ValueError unless it is non-empty
    valid base64."""
    if not isinstance(text, str):
        raise ValueError("state is not a string")  # noqa: TRY004 -- callers catch ValueError.
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"state is not valid base64: {error}") from error
    if not data:
        raise ValueError("state is empty")
    return data
