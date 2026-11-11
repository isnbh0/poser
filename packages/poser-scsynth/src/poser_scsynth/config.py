"""A scsynth instrument's options: the engine binary and its sound sources.

The layout mirrors `poser.adapters.pedalboard.config`, minus everything a plugin host needs:
there is no plugin path, no preset formats, and no state. The `scsynth` option may be absent at
build time — building reads nothing but its arguments, and a missing binary is a render-time
`load_failed`, not a config error.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from poser.core.jsonvalue import JsonValue
from poser.core.names import Identifier
from poser.core.ports import ConfigError

# The source of the sounds this adapter's owner keeps, in `<sounds dir>/<instrument>/`.
SAVED_SOURCE = "saved"
# The source this package's own sounds ship under, from phase 2 on. Reserved now.
BUILTIN_SOURCE = "builtin"
OPTION_KEYS = frozenset({"scsynth", "sources"})


@dataclass(frozen=True)
class ScsynthConfig:
    name: Identifier
    scsynth: Path | None  # Absolute, when configured.
    sources: Mapping[Identifier, Path]  # Absolute; never SAVED_SOURCE or BUILTIN_SOURCE.

    def __post_init__(self) -> None:
        if not isinstance(self.name, Identifier):
            raise ConfigError(f"instrument name must be an Identifier, got {self.name!r}")
        owner = f"instrument {self.name!r}"
        if self.scsynth is not None and not (
            isinstance(self.scsynth, Path) and self.scsynth.is_absolute()
        ):
            raise ConfigError(f"{owner}: scsynth must be an absolute path, got {self.scsynth!r}")
        if not isinstance(self.sources, Mapping):
            raise ConfigError(f"{owner}: sources must be a mapping, got {self.sources!r}")
        for source, directory in self.sources.items():
            if not isinstance(source, Identifier):
                raise ConfigError(f"{owner}: source name {source!r} is not an Identifier")
            if source == SAVED_SOURCE:
                raise ConfigError(f"{owner}: source name {SAVED_SOURCE!r} is reserved")
            if source == BUILTIN_SOURCE:
                raise ConfigError(f"{owner}: source name {BUILTIN_SOURCE!r} is reserved")
            if not (isinstance(directory, Path) and directory.is_absolute()):
                raise ConfigError(
                    f"{owner}: source {source!r} must be an absolute path, got {directory!r}"
                )
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def from_options(name: str, options: Mapping[str, JsonValue], base: Path) -> ScsynthConfig:
    """Return the config an instrument's options give, resolving relative paths against `base`.

    Raises ConfigError naming the instrument for any option it cannot use. A missing `scsynth`
    is allowed: building an adapter starts no process, and the engine is needed only at render
    time.
    """
    owner = f"instrument {name!r}"
    try:
        identifier = Identifier(name)
    except ValueError as error:
        raise ConfigError(f"{owner}: {error}") from error
    if unknown := sorted(set(options) - OPTION_KEYS):
        raise ConfigError(f"{owner}: unknown option {unknown[0]!r}")

    path = _text(options.get("scsynth"))
    scsynth = None if path is None else base / path

    raw_sources = options.get("sources", {})
    if not isinstance(raw_sources, dict):
        raise ConfigError(f"{owner}: sources must be a table of paths")
    sources: dict[Identifier, Path] = {}
    for source, directory in raw_sources.items():
        if not Identifier.PATTERN.fullmatch(source):
            raise ConfigError(
                f"{owner}: source name {source!r} does not match {Identifier.PATTERN.pattern}"
            )
        if source == SAVED_SOURCE:
            raise ConfigError(f"{owner}: source name {SAVED_SOURCE!r} is reserved")
        if source == BUILTIN_SOURCE:
            raise ConfigError(f"{owner}: source name {BUILTIN_SOURCE!r} is reserved")
        text = _text(directory)
        if text is None:
            raise ConfigError(f"{owner}: source {source!r} must be a non-empty path")
        sources[Identifier(source)] = base / text

    return ScsynthConfig(identifier, scsynth, sources)
