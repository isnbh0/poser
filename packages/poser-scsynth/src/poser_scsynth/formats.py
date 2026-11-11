"""Validation of the v1 sound artifact: one SCgf v2 definition and a JSON sidecar.

SCgf's lengths, reference indices and rates are checked before invoking native code. Engine-
specific validation (installed UGens and their inputs) remains the engine's responsibility.
The format is documented at https://doc.sccode.org/Reference/Synth-Definition-File-Format.html.
"""

import base64
import hashlib
import math
import struct
from dataclasses import dataclass, field
from typing import Self, cast

from poser.core.jsonvalue import Json, json_object


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def take(self, size: int) -> bytes:
        if size < 0 or size > len(self.data) - self.position:
            raise ValueError("truncated synthdef")
        start = self.position
        self.position += size
        return self.data[start : self.position]

    def integer(self, width: int = 4) -> int:
        return int.from_bytes(self.take(width), "big", signed=True)

    def count(self, width: int = 4) -> int:
        count = self.integer(width)
        if count < 0 or count > len(self.data) - self.position:
            raise ValueError("invalid synthdef count")
        return count

    def text(self) -> str:
        size = self.take(1)[0]
        text = self.take(size).decode("utf-8")
        if not text or "\0" in text:
            raise ValueError("invalid synthdef name")
        return text

    def floats(self, count: int) -> None:
        data = self.take(4 * count)
        if any(not math.isfinite(value) for (value,) in struct.iter_unpack(">f", data)):
            raise ValueError("non-finite synthdef value")

    def rate(self) -> int:
        rate = self.integer(1)
        if rate not in (0, 1, 2, 3):
            raise ValueError("invalid synthdef calculation rate")
        return rate


def synthdef_name(data: bytes) -> str:
    """Validate one SCgf v2 definition and return its declared name.

    Rejects truncated bodies, bad counts and out-of-bounds UGen/parameter references, not just
    the header: scsynth can report a load error yet exit zero with a silent WAV.
    """
    reader = _Reader(data)
    if reader.take(4) != b"SCgf" or reader.integer() != 2 or reader.integer(2) != 1:
        raise ValueError("expected one SCgf v2 synthdef")
    name = reader.text()
    constants = reader.count()
    reader.floats(constants)
    params = reader.count()
    reader.floats(params)
    for _ in range(reader.count()):
        reader.text()
        if not 0 <= reader.integer() < params:
            raise ValueError("invalid synthdef parameter index")
    outputs: list[int] = []
    for _ in range(reader.count()):
        reader.text()
        reader.rate()
        inputs = reader.count()
        count = reader.count()
        reader.integer(2)  # UGen-specific special index.
        for _ in range(inputs):
            source, output = reader.integer(), reader.integer()
            if source == -1:
                valid = 0 <= output < constants
            else:
                valid = 0 <= source < len(outputs) and 0 <= output < outputs[source]
            if not valid:
                raise ValueError("invalid synthdef UGen input reference")
        for _ in range(count):
            reader.rate()
        outputs.append(count)
    for _ in range(reader.count(2)):
        reader.text()
        reader.floats(params)
    if reader.position != len(data):
        raise ValueError("unexpected bytes after synthdef")
    return name


def _parameter_name(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def sound_meta(value: object) -> dict[str, object]:
    """Validate the JSON sidecar and return it; raises ValueError for malformed mappings."""
    if not isinstance(value, dict):
        raise ValueError("sound meta must be a table")  # noqa: TRY004 -- malformed file data.
    mapping = value.get("cc", {})
    if not isinstance(mapping, dict) or not all(
        isinstance(key, str)
        and key.isascii()
        and key.isdigit()
        and str(int(key)) == key
        and 0 <= int(key) <= 119
        and _parameter_name(param)
        for key, param in mapping.items()
    ):
        raise ValueError("cc must map controller numbers 0-119 to UTF-8, non-empty parameter names")
    return value


@dataclass(frozen=True)
class SoundRecipe:
    """A renderable artifact, established once at the file or payload boundary.

    Bytes and canonical metadata are immutable. The name and controller map cannot be supplied
    separately from their source: construction derives them from the checked artifact.
    """

    synthdef: bytes
    meta: Json
    name: str = field(init=False)
    controls: tuple[tuple[int, str], ...] = field(init=False)

    def __post_init__(self) -> None:
        name = synthdef_name(self.synthdef)
        meta = sound_meta(self.meta.value())
        mapping = cast("dict[str, str]", meta.get("cc", {}))  # sound_meta checked it.
        controls = tuple((int(key), value) for key, value in sorted(mapping.items()))
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "controls", controls)

    @classmethod
    def from_payload(cls, payload: Json) -> Self:
        fields = json_object(payload.value())
        if fields is None or set(fields) != {"synthdef", "meta"}:
            raise ValueError("not a scsynth payload")
        encoded = fields["synthdef"]
        if not isinstance(encoded, str):
            raise ValueError("synthdef must be base64 text")  # noqa: TRY004 -- malformed payload.
        return cls(base64.b64decode(encoded, validate=True), Json.of(fields["meta"]))

    @property
    def payload(self) -> Json:
        return Json.of(
            {"synthdef": base64.b64encode(self.synthdef).decode(), "meta": self.meta.value()}
        )

    def identity(self, version: str) -> str:
        return hashlib.sha256(
            b"\0".join([b"poser-scsynth", version.encode(), self.synthdef, self.meta.text.encode()])
        ).hexdigest()
