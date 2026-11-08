"""A Pedalboard instrument's options: its plugin, its preset sources, and its preset formats."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from poser.core.jsonvalue import JsonValue
from poser.core.names import Identifier
from poser.core.ports import ConfigError

INITIALIZATION_TIMEOUT_S = 30.0
# The source of the sounds `save-sound` keeps, in `<sounds dir>/<instrument>/`.
SAVED_SOURCE = "saved"
DEFAULT_FORMATS = ("fxp", "saved")
OPTION_KEYS = frozenset({"path", "plugin_name", "sources", "formats"})


@dataclass(frozen=True)
class PedalboardConfig:
    name: Identifier
    path: Path  # Absolute.
    plugin_name: str | None
    sources: Mapping[Identifier, Path]  # Absolute; never SAVED_SOURCE; read-only.
    formats: tuple[str, ...]  # Non-empty; names in PRESET_FORMATS or import paths.

    def __post_init__(self) -> None:
        if not isinstance(self.name, Identifier):
            raise ConfigError(f"instrument name must be an Identifier, got {self.name!r}")
        owner = f"instrument {self.name!r}"
        if not (isinstance(self.path, Path) and self.path.is_absolute()):
            raise ConfigError(f"{owner}: path must be an absolute path, got {self.path!r}")
        if self.plugin_name is not None and not (
            isinstance(self.plugin_name, str) and self.plugin_name.strip()
        ):
            raise ConfigError(f"{owner}: plugin_name must be non-empty text")
        if not isinstance(self.sources, Mapping):
            raise ConfigError(f"{owner}: sources must be a mapping, got {self.sources!r}")
        for source, directory in self.sources.items():
            if not isinstance(source, Identifier):
                raise ConfigError(f"{owner}: source name {source!r} is not an Identifier")
            if source == SAVED_SOURCE:
                raise ConfigError(f"{owner}: source name {SAVED_SOURCE!r} is reserved")
            if not (isinstance(directory, Path) and directory.is_absolute()):
                raise ConfigError(
                    f"{owner}: source {source!r} must be an absolute path, got {directory!r}"
                )
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        if not (
            isinstance(self.formats, tuple)
            and all(isinstance(fmt, str) and fmt for fmt in self.formats)
        ):
            raise ConfigError(f"{owner}: formats must be a tuple of names, got {self.formats!r}")
        if not self.formats:
            raise ConfigError(f"{owner}: formats must name at least one preset format")
        if len(set(self.formats)) != len(self.formats):
            raise ConfigError(f"{owner}: formats has a duplicate in {list(self.formats)}")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def from_options(name: str, options: Mapping[str, JsonValue], base: Path) -> PedalboardConfig:
    """Return the config an instrument's options give, resolving relative paths against `base`.

    Raises ConfigError naming the instrument for any option it cannot use.
    """
    owner = f"instrument {name!r}"
    try:
        identifier = Identifier(name)
    except ValueError as error:
        raise ConfigError(f"{owner}: {error}") from error
    if unknown := sorted(set(options) - OPTION_KEYS):
        raise ConfigError(f"{owner}: unknown option {unknown[0]!r}")

    path = _text(options.get("path"))
    if path is None:
        raise ConfigError(f"{owner}: path must be non-empty text")

    # Absent means the bundle's only plugin; when present, it must name one.
    plugin_name = options.get("plugin_name")
    if "plugin_name" in options and _text(plugin_name) is None:
        raise ConfigError(f"{owner}: plugin_name must be non-empty text")

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
        text = _text(directory)
        if text is None:
            raise ConfigError(f"{owner}: source {source!r} must be a non-empty path")
        sources[Identifier(source)] = base / text

    raw_formats = options.get("formats", list(DEFAULT_FORMATS))
    if not isinstance(raw_formats, list) or not all(
        isinstance(fmt, str) and fmt for fmt in raw_formats
    ):
        raise ConfigError(f"{owner}: formats must be a list of format names")
    formats = tuple(fmt for fmt in raw_formats if isinstance(fmt, str))
    if len(set(formats)) != len(formats):
        raise ConfigError(f"{owner}: formats has a duplicate in {list(formats)}")

    return PedalboardConfig(
        name=identifier,
        path=base / path,
        plugin_name=plugin_name if isinstance(plugin_name, str) else None,
        sources=sources,
        formats=formats,
    )
