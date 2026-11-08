"""Preset formats: the files a Pedalboard instrument turns into a plugin's `raw_state`.

`PresetFormat` is this adapter's inner port. `FxpFormat` reads opaque-chunk `.fxp` programs, and
`SavedFormat` reads and writes the sounds `save-sound` keeps. This module imports no plugin code.

Serum 2's `.SerumPreset` format is deferred: whether a `.SerumPreset` file can become Pedalboard
`raw_state` is unverified (see `../poser-studio/notes/serum2-for-poser.md`). Once it is, it is a
`PresetFormat` registered by import path.
"""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from poser.adapters.pedalboard.state import build_state, decode_state, encode_state
from poser.core.jsonvalue import json_object
from poser.core.names import Suffix

FXP_HEADER_SIZE = 60
SAVED_KEYS = frozenset({"description", "state"})


class PresetError(Exception):
    """A preset file cannot be used."""


@runtime_checkable
class PresetFormat(Protocol):
    """A kind of preset file."""

    @property
    def suffix(self) -> Suffix:
        """The suffix of this format's files, matched ignoring case."""
        ...

    def read_state(self, path: Path) -> bytes:
        """Return the plugin's `raw_state` from the file. Raises PresetError."""
        ...

    def describe(self, path: Path) -> str | None:
        """Return the file's description, or None to describe it by its folder."""
        ...


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


class FxpFormat:
    """Opaque-chunk `.fxp` programs, whose chunk is the plugin's component state."""

    @property
    def suffix(self) -> Suffix:
        return Suffix(".fxp")

    def read_state(self, path: Path) -> bytes:
        return build_state(read_chunk(path))

    def describe(self, path: Path) -> str | None:  # noqa: ARG002 -- the folder rule describes it.
        return None


@dataclass(frozen=True)
class SavedSound:
    """A sound the owner saved: what it is for, and the plugin's `raw_state`."""

    description: str
    state: bytes

    def __post_init__(self) -> None:
        if not (isinstance(self.description, str) and self.description.strip()):
            raise ValueError(f"a saved sound needs a description, got {self.description!r}")
        if not (isinstance(self.state, bytes) and self.state):
            raise ValueError("a saved sound needs a non-empty state")


class SavedFormat:
    """Saved sounds: JSON files holding exactly `description` and `state`, the standard base64 of
    the plugin's `raw_state`."""

    @property
    def suffix(self) -> Suffix:
        return Suffix(".json")

    def read_sound(self, path: Path) -> SavedSound:
        try:
            data = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise PresetError(f"cannot read {path.name}: {error}") from error
        fields = json_object(data)
        if fields is None or set(fields) != SAVED_KEYS:
            raise PresetError(f"must hold exactly the keys {sorted(SAVED_KEYS)}")
        description = fields["description"]
        if not isinstance(description, str) or not description.strip():
            raise PresetError("description must be a non-empty string")
        try:
            state = decode_state(fields["state"])
        except ValueError as error:
            raise PresetError(str(error)) from error
        return SavedSound(description, state)

    def write_sound(self, path: Path, sound: SavedSound) -> None:
        """Write a sound file through a temporary file, so a reader never sees a partial one."""
        text = json.dumps({"description": sound.description, "state": encode_state(sound.state)})
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".json")
        temp = Path(name)
        try:
            with os.fdopen(fd, "w") as output:
                output.write(text + "\n")
            # mkstemp creates the file owner-only; give it the usual umask-based mode.
            umask = os.umask(0)
            os.umask(umask)
            temp.chmod(0o666 & ~umask)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    def read_state(self, path: Path) -> bytes:
        return self.read_sound(path).state

    def describe(self, path: Path) -> str | None:
        return self.read_sound(path).description


PRESET_FORMATS: Mapping[str, PresetFormat] = {"fxp": FxpFormat(), "saved": SavedFormat()}
