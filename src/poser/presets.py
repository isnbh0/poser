"""Installed presets: a plugin's `.fxp` files, used as sounds without copying them.

A preset's name is `<plugin name>:<path in the plugin's presets directory, without .fxp>`, such as
`Serum:Bass/BA FunkMogue [CFA]`. Rendering builds the plugin's `raw_state` from the preset's chunk
and restores it like a saved sound. This module is the only one that knows the `.fxp` format and
the `raw_state` framing, and it imports no plugin code.

`raw_state` under Pedalboard is JUCE's binary XML form: `VC2!`, the XML text's length as a 4-byte
little-endian integer, the text, and a zero byte. The text holds the VST3 component state in JUCE's
`MemoryBlock` base64 form. Serum's component state is the same data as an `.fxp` chunk, as measured
with Serum 1.368 and Pedalboard 0.9.26.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

# Only for annotations: importing `plugin_check` at run time would load `mido`.
if TYPE_CHECKING:
    from poser.plugin_check import PluginSpec

FXP_HEADER_SIZE = 60
STATE_MAGIC = b"VC2!"
STATE_XML_HEAD = b'<?xml version="1.0" encoding="UTF-8"?> <VST3PluginState><IComponent>'
STATE_XML_TAIL = b"</IComponent></VST3PluginState>"
JUCE_BASE64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"
PRESET_DESCRIPTION = "preset"


class PresetError(Exception):
    """A preset file or presets directory cannot be used."""


def preset_files(spec: "PluginSpec") -> dict[str, Path]:
    """Map each of a plugin's preset names to its file.

    Lookup goes through this listing, not `Path.exists`, because a default macOS volume would
    match `Serum:bass/Deep` to `Bass/Deep.fxp`.
    """
    if spec.presets is None:
        return {}
    root = Path(spec.presets)
    if not root.exists():
        raise PresetError(f"presets directory {root} for {spec.name!r} does not exist")
    if not root.is_dir():
        raise PresetError(f"presets path {root} for {spec.name!r} is not a directory")

    def unreadable(error: OSError) -> None:
        raise PresetError(f"cannot read presets folder {error.filename}: {error.strerror}")

    # Symlinked folders count, since a user may link a preset pack in. `seen` stops link loops.
    files = {}
    seen = set()
    for folder, dirs, names in os.walk(root, onerror=unreadable, followlinks=True):
        real = os.path.realpath(folder)
        if real in seen:
            dirs.clear()
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in names:
            path = Path(folder, name)
            if name.startswith(".") or path.suffix.lower() != ".fxp" or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()[: -len(path.suffix)]
            files[f"{spec.name}:{relative}"] = path
    return files


def preset_description(name: str) -> str:
    """Describe a preset by its folder: `Serum:Bass (Hard)/X` is `Bass (Hard) preset`."""
    folder = name.split(":", 1)[1].rpartition("/")[0]
    return f"{folder} {PRESET_DESCRIPTION}" if folder else PRESET_DESCRIPTION


def read_chunk(path: Path) -> bytes:
    """Return the plugin data of an opaque-chunk `.fxp` program."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise PresetError(f"cannot read {path.name}: {error}") from error
    if len(data) < FXP_HEADER_SIZE:
        raise PresetError(f"{path.name} is too short for an .fxp header")
    if data[0:4] != b"CcnK":
        raise PresetError(f"{path.name} is not an .fxp file")
    if data[8:12] == b"FxCk":
        raise PresetError(f"{path.name} is a parameter preset, which is not supported")
    if data[8:12] != b"FPCh":
        raise PresetError(f"{path.name} is not a single-program chunk preset")
    size = int.from_bytes(data[56:60], "big")
    if FXP_HEADER_SIZE + size != len(data):
        raise PresetError(
            f"{path.name} declares a {size}-byte chunk but holds {len(data) - FXP_HEADER_SIZE}"
        )
    if size == 0:
        raise PresetError(f"{path.name} has an empty chunk")
    return data[FXP_HEADER_SIZE:]


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
    """Return the component state inside a `raw_state` in exactly the framing `build_state` writes."""
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
