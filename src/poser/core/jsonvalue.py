"""JSON data: the type of parsed JSON, checks for it, and an immutable JSON value."""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self, TypeGuard, cast

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


def json_object(value: object) -> Mapping[str, object] | None:
    """Return parsed JSON as a mapping of untyped values, or None when it is not an object.

    The declared return type makes the type checker require each value to be checked before use.
    """
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return None


def is_json_value(value: object) -> TypeGuard[JsonValue]:
    """Return whether the value is JSON data: None, bool, int, finite float, str, and lists and
    str-keyed dicts of these, recursively."""
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and is_json_value(item) for key, item in value.items())
    return False


def _canonical(data: JsonValue) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data: JsonValue) -> str:
    """Return the SHA-256 hex digest of the data's canonical JSON."""
    return hashlib.sha256(_canonical(data).encode()).hexdigest()


@dataclass(frozen=True)
class Json:
    """An immutable JSON value, held as its canonical text."""

    text: str  # Canonical JSON: sorted keys, no spaces.

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError(f"Json text must be a str, got {self.text!r}")
        try:
            parsed = json.loads(self.text, parse_constant=_refuse_constant)
        except ValueError as error:
            raise ValueError(f"Json text must be JSON: {error}") from error
        if not is_json_value(parsed) or _canonical(parsed) != self.text:
            raise ValueError(f"Json text must be canonical JSON, got {self.text!r}")

    @classmethod
    def of(cls, value: object) -> Self:
        """Return the JSON value, raising ValueError unless `value` is JSON data."""
        if not is_json_value(value):
            raise ValueError(f"not a JSON value: {value!r}")
        return cls(_canonical(value))

    def value(self) -> JsonValue:
        """Return a new parsed copy of the value."""
        return cast("JsonValue", json.loads(self.text))  # __post_init__ checked the text.


def _refuse_constant(name: str) -> None:
    raise ValueError(f"JSON has no {name}")
