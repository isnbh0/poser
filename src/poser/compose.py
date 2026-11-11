"""The composition root: builds a workspace's adapters from its `poser.toml`.

This is the only module that knows which adapters exist. It names poser's own adapters by import
path in `BUILTINS`, so importing it loads no adapter library, and building imports only the
adapters a command uses. A workspace selects any other adapter by import path, `module:attribute`,
naming a factory:

- an instrument's or effect's factory takes `(name, options, context)`;
- the render store's takes `(options, context, stem_suffix)`;
- every other one takes `(options, context)`.

A factory raises ConfigError for options it cannot use; any other exception is a bug in the
adapter and propagates. Building an adapter loads no plugin, starts no process, writes no file,
and reads nothing but its arguments and imported modules, so a guide can be composed on a
machine without the plugins.

Deferred until adapters are published by other people: discovering adapters through entry points
(`importlib.metadata.entry_points(group="poser.adapters")`) and a versioned adapter contract.
"""

import copy
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast

from poser.adapters.loading import load_object
from poser.core.analysis import AnalysisTools
from poser.core.guide import InstrumentNote
from poser.core.jsonvalue import JsonValue, is_json_value
from poser.core.names import Identifier, Suffix
from poser.core.ports import (
    AdapterContext,
    AudioFiles,
    ConfigError,
    Effect,
    Guided,
    Instrument,
    Measurer,
    Mixer,
    RecordWriter,
    RenderStore,
    ReportSection,
    SongFormat,
    Visualizer,
)

INSTRUMENT = "instrument"
EFFECT = "effect"
BUILTINS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {
        INSTRUMENT: MappingProxyType({"pedalboard": "poser.adapters.pedalboard.instrument:create"}),
        EFFECT: MappingProxyType(
            {"pedalboard": "poser.adapters.pedalboard.instrument:create_effect"}
        ),
        "song_formats": MappingProxyType(
            {
                "yaml": "poser.adapters.song_formats:create_yaml",
                "script": "poser.adapters.song_formats:create_script",
            }
        ),
        "record_writer": MappingProxyType({"yaml": "poser.adapters.song_formats:create_yaml"}),
        "audio_files": MappingProxyType({"wav": "poser.adapters.wav:create"}),
        "render_store": MappingProxyType({"files": "poser.adapters.filestore:create"}),
        "mixer": MappingProxyType({"gain": "poser.adapters.mixing:create"}),
        "measurer": MappingProxyType({"standard": "poser.adapters.measuring:create"}),
        "visualizer": MappingProxyType({"spectrogram": "poser.adapters.spectrogram:create"}),
        "sections": MappingProxyType[str, str]({}),
    }
)
# Each `[adapters]` key and its default, in the order `guide_parts` follows. A tuple is a list key.
DEFAULTS: Mapping[str, str | tuple[str, ...]] = MappingProxyType(
    {
        "song_formats": ("yaml", "script"),
        "record_writer": "yaml",
        "audio_files": "wav",
        "render_store": "files",
        "mixer": "gain",
        "measurer": "standard",
        "visualizer": "spectrogram",
        "sections": (),
    }
)
# List keys that cannot be empty: a workspace with no song format can load no song.
NON_EMPTY_LISTS = frozenset({"song_formats"})
TOP_LEVEL_KEYS = frozenset({"instruments", "effects", "adapters"})
ADAPTER_KEY = "adapter"  # An instrument table's spelling of `use`.
GUIDE_KEY = "guide"  # An instrument table's note for the guide; compose's, not the adapter's.
USE_KEY = "use"
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
IMPORT_PATH = re.compile(rf"{_NAME}(\.{_NAME})*:{_NAME}(\.{_NAME})*")


def _read_only(options: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return MappingProxyType(copy.deepcopy(dict(options)))


@dataclass(frozen=True)
class AdapterSpec:
    """An adapter named by a short name or an import path, with its options."""

    use: str
    options: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", _read_only(self.options))


@dataclass(frozen=True)
class InstrumentSpec:
    name: Identifier
    spec: AdapterSpec
    guide: str | None


@dataclass(frozen=True)
class EffectSpec:
    name: Identifier
    spec: AdapterSpec


@dataclass(frozen=True)
class WorkspaceConfig:
    path: Path
    instruments: tuple[InstrumentSpec, ...]
    effects: tuple[EffectSpec, ...]
    # Every `[adapters]` key, with defaults filled in; list keys hold tuples.
    adapters: Mapping[str, AdapterSpec | tuple[AdapterSpec, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapters", MappingProxyType(dict(self.adapters)))


# Reading poser.toml


def _table(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def _check_use(path: Path, where: str, key: str, use: str) -> None:
    if use in BUILTINS[key] or IMPORT_PATH.fullmatch(use):
        return
    names = ", ".join(repr(name) for name in BUILTINS[key])
    builtins = f"a built-in ({names}) or " if names else ""
    raise ConfigError(f"{path}: {where}: {use!r} is not {builtins}an import path module:attribute")


def _options(path: Path, where: str, table: Mapping[str, object]) -> dict[str, JsonValue]:
    options: dict[str, JsonValue] = {}
    for name, value in table.items():
        if not is_json_value(value):
            raise ConfigError(f"{path}: {where}.{name}: {value!r} is not a JSON value")
        options[name] = value
    return options


def _spec(path: Path, where: str, key: str, value: object) -> AdapterSpec:
    if isinstance(value, str):
        _check_use(path, where, key, value)
        return AdapterSpec(value)
    table = _table(value)
    if table is None or not isinstance(table.get(USE_KEY), str):
        raise ConfigError(f"{path}: {where}: must be a name or a table with a string `use`")
    use = cast("str", table[USE_KEY])
    _check_use(path, where, key, use)
    rest = {name: item for name, item in table.items() if name != USE_KEY}
    return AdapterSpec(use, _options(path, where, rest))


def _recipes(
    path: Path, value: object, where: str, key: str, noun: str, *, guides: bool
) -> list[tuple[Identifier, AdapterSpec, str | None]]:
    """Read an `[instruments]` or `[effects]` table: unique names, each naming an adapter."""
    table = _table(value)
    if table is None:
        raise ConfigError(f"{path}: {where}: must be a table of {noun}s")
    specs: list[tuple[Identifier, AdapterSpec, str | None]] = []
    seen: dict[str, str] = {}
    for name, item in table.items():
        owner = f"{where}.{name}"
        if not Identifier.PATTERN.fullmatch(name):
            raise ConfigError(
                f"{path}: {owner}: the name does not match {Identifier.PATTERN.pattern}"
            )
        if (other := seen.get(name.lower())) is not None:
            raise ConfigError(f"{path}: {owner}: the name differs only in case from {other!r}")
        seen[name.lower()] = name
        body = _table(item)
        if body is None:
            raise ConfigError(f"{path}: {owner}: must be a table")
        adapter = body.get(ADAPTER_KEY)
        if not isinstance(adapter, str):
            raise ConfigError(f"{path}: {owner}.{ADAPTER_KEY}: must be a string")
        _check_use(path, f"{owner}.{ADAPTER_KEY}", key, adapter)
        guide = body.get(GUIDE_KEY)
        if guides and GUIDE_KEY in body and not (isinstance(guide, str) and guide.strip()):
            raise ConfigError(f"{path}: {owner}.{GUIDE_KEY}: must be non-blank text")
        skip = {ADAPTER_KEY, GUIDE_KEY} if guides else {ADAPTER_KEY}
        rest = {n: v for n, v in body.items() if n not in skip}
        spec = AdapterSpec(adapter, _options(path, owner, rest))
        specs.append((Identifier(name), spec, guide if isinstance(guide, str) else None))
    return specs


def _instruments(path: Path, value: object) -> tuple[InstrumentSpec, ...]:
    specs = _recipes(path, value, "instruments", INSTRUMENT, "instrument", guides=True)
    if not specs:
        raise ConfigError(f"{path}: instruments: must name at least one instrument")
    return tuple(InstrumentSpec(name, spec, guide) for name, spec, guide in specs)


def _effects(path: Path, value: object) -> tuple[EffectSpec, ...]:
    """Read the optional `[effects]` table: zero effects is valid."""
    return tuple(
        EffectSpec(name, spec)
        for name, spec, _ in _recipes(path, value, "effects", EFFECT, "effect", guides=False)
    )


def _adapters(path: Path, value: object) -> dict[str, AdapterSpec | tuple[AdapterSpec, ...]]:
    table = _table(value)
    if table is None:
        raise ConfigError(f"{path}: adapters: must be a table")
    for key in table:
        if key not in DEFAULTS:
            raise ConfigError(f"{path}: adapters.{key}: unknown key")
    adapters: dict[str, AdapterSpec | tuple[AdapterSpec, ...]] = {}
    for key, default in DEFAULTS.items():
        where = f"adapters.{key}"
        raw = table.get(key, list(default) if isinstance(default, tuple) else default)
        if isinstance(default, tuple):
            if not isinstance(raw, list):
                raise ConfigError(f"{path}: {where}: must be a list")
            items = cast("list[object]", raw)
            if not items and key in NON_EMPTY_LISTS:
                raise ConfigError(f"{path}: {where}: must name at least one adapter")
            adapters[key] = tuple(
                _spec(path, f"{where}[{index}]", key, item) for index, item in enumerate(items)
            )
        else:
            adapters[key] = _spec(path, where, key, raw)
    return adapters


def read_config(path: Path) -> WorkspaceConfig:
    """Read and check a workspace config's structure, importing no adapter.

    Raises ConfigError naming the file and the key for anything it cannot use.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path}: cannot read: {error}") from error
    return parse_config(text, path)


def parse_config(text: str, path: Path) -> WorkspaceConfig:
    """Parse configuration text as if it were located at path, without filesystem access."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: cannot read: {error}") from error
    for key in data:
        if key not in TOP_LEVEL_KEYS:
            raise ConfigError(
                f"{path}: {key}: unknown key; use `instruments`, `effects`, and `adapters`"
            )
    if "instruments" not in data:
        raise ConfigError(f"{path}: instruments: missing; name at least one instrument")
    instruments = _instruments(path, data["instruments"])
    effects = _effects(path, data.get("effects", {}))
    adapters = _adapters(path, data.get("adapters", {}))
    return WorkspaceConfig(path, instruments, effects, adapters)


# Building adapters


class Composition:
    """A workspace's adapters, each built on first use and kept."""

    def __init__(self, config: WorkspaceConfig, context: AdapterContext) -> None:
        self._config = config
        self._context = context
        self._built: dict[str, object] = {}

    @property
    def config(self) -> WorkspaceConfig:
        return self._config

    @property
    def context(self) -> AdapterContext:
        return self._context

    def _fail(self, where: str, problem: str) -> ConfigError:
        return ConfigError(f"{self._config.path}: {where}: {problem}")

    def _make[T](
        self,
        where: str,
        key: str,
        spec: AdapterSpec,
        port: type[T],
        *args: object,
        owner: str | None = None,
    ) -> T:
        """Build one adapter, naming `where` in any ConfigError.

        A factory's error that starts with `owner`, such as `instrument 'serum': `, drops it, so
        the message names the instrument once.
        """
        target = BUILTINS[key].get(spec.use, spec.use)
        try:
            factory = load_object(target)
        except ConfigError as error:
            raise self._fail(where, str(error)) from error
        if not callable(factory):
            raise self._fail(where, f"{spec.use!r} is not a factory")
        try:
            made = factory(*args)
        except ConfigError as error:
            problem = str(error)
            if owner is not None:
                problem = problem.removeprefix(f"{owner}: ")
            raise self._fail(where, problem) from error
        if not isinstance(made, port):
            raise self._fail(
                where, f"{spec.use!r} made a {type(made).__name__}, which is not a {port.__name__}"
            )
        return made

    def _suffix(self, where: str, name: str, value: object) -> None:
        if not isinstance(value, Suffix):
            raise self._fail(where, f"{name} {value!r} is not a Suffix")

    def _single(self, key: str) -> AdapterSpec:
        return cast("AdapterSpec", self._config.adapters[key])

    def _listed(self, key: str) -> tuple[AdapterSpec, ...]:
        return cast("tuple[AdapterSpec, ...]", self._config.adapters[key])

    def _cached[T](self, key: str, build: Callable[[], T]) -> T:
        if key not in self._built:
            self._built[key] = build()
        return cast("T", self._built[key])

    def _options(self, spec: AdapterSpec) -> Mapping[str, JsonValue]:
        return _read_only(spec.options)

    def instruments(self) -> Mapping[Identifier, Instrument]:
        """Every instrument, by name, in config order."""

        def build() -> Mapping[Identifier, Instrument]:
            built: dict[Identifier, Instrument] = {}
            for item in self._config.instruments:
                where = f"instruments.{item.name}"
                spec = item.spec
                options = self._options(spec)
                instrument = self._make(
                    where,
                    INSTRUMENT,
                    spec,
                    Instrument,
                    item.name,
                    options,
                    self._context,
                    owner=f"instrument {item.name!r}",
                )
                if not isinstance(instrument.adapter_id, Identifier):
                    raise self._fail(
                        where, f"adapter_id {instrument.adapter_id!r} is not an Identifier"
                    )
                version = instrument.adapter_version
                if not (isinstance(version, str) and version):
                    raise self._fail(where, f"adapter_version {version!r} is not a non-empty str")
                built[item.name] = instrument
            return MappingProxyType(built)

        return self._cached("instruments", build)

    def effects(self) -> Mapping[Identifier, Effect]:
        """Every effect recipe, by name, in config order."""

        def build() -> Mapping[Identifier, Effect]:
            built: dict[Identifier, Effect] = {}
            for item in self._config.effects:
                where = f"effects.{item.name}"
                spec = item.spec
                options = self._options(spec)
                effect = self._make(
                    where,
                    EFFECT,
                    spec,
                    Effect,
                    item.name,
                    options,
                    self._context,
                    owner=f"effect {item.name!r}",
                )
                if not isinstance(effect.adapter_id, Identifier):
                    raise self._fail(
                        where, f"adapter_id {effect.adapter_id!r} is not an Identifier"
                    )
                version = effect.adapter_version
                if not (isinstance(version, str) and version):
                    raise self._fail(where, f"adapter_version {version!r} is not a non-empty str")
                built[item.name] = effect
            return MappingProxyType(built)

        return self._cached("effects", build)

    def _plain[T](self, key: str, port: type[T]) -> T:
        spec = self._single(key)
        return self._make(f"adapters.{key}", key, spec, port, self._options(spec), self._context)

    def song_formats(self) -> tuple[SongFormat, ...]:
        def build() -> tuple[SongFormat, ...]:
            built: list[SongFormat] = []
            claimed: dict[Suffix, str] = {}
            for index, spec in enumerate(self._listed("song_formats")):
                where = f"adapters.song_formats[{index}]"
                fmt = self._make(
                    where, "song_formats", spec, SongFormat, self._options(spec), self._context
                )
                suffixes = fmt.suffixes
                if not isinstance(suffixes, Sequence) or isinstance(suffixes, str):
                    raise self._fail(where, f"suffixes {suffixes!r} is not a tuple of Suffix")
                for suffix in cast("Sequence[object]", suffixes):
                    self._suffix(where, "suffixes entry", suffix)
                    owner = claimed.get(cast("Suffix", suffix))
                    if owner is not None:
                        raise self._fail(
                            "adapters.song_formats",
                            f"{suffix!r} is read by both {owner!r} and {spec.use!r}",
                        )
                    claimed[cast("Suffix", suffix)] = spec.use
                built.append(fmt)
            return tuple(built)

        return self._cached("song_formats", build)

    def record_writer(self) -> RecordWriter:
        def build() -> RecordWriter:
            writer = self._plain("record_writer", RecordWriter)
            self._suffix("adapters.record_writer", "suffix", writer.suffix)
            return writer

        return self._cached("record_writer", build)

    def audio_files(self) -> AudioFiles:
        def build() -> AudioFiles:
            files = self._plain("audio_files", AudioFiles)
            self._suffix("adapters.audio_files", "suffix", files.suffix)
            return files

        return self._cached("audio_files", build)

    def render_store(self) -> RenderStore:
        def build() -> RenderStore:
            stem_suffix = self.audio_files().suffix
            spec = self._single("render_store")
            where = "adapters.render_store"
            store = self._make(
                where,
                "render_store",
                spec,
                RenderStore,
                self._options(spec),
                self._context,
                stem_suffix,
            )
            self._suffix(where, "stem_suffix", store.stem_suffix)
            return store

        return self._cached("render_store", build)

    def mixer(self) -> Mixer:
        return self._cached("mixer", lambda: self._plain("mixer", Mixer))

    def measurer(self) -> Measurer:
        return self._cached("measurer", lambda: self._plain("measurer", Measurer))

    def visualizer(self) -> Visualizer:
        def build() -> Visualizer:
            visualizer = self._plain("visualizer", Visualizer)
            self._suffix("adapters.visualizer", "suffix", visualizer.suffix)
            return visualizer

        return self._cached("visualizer", build)

    def sections(self) -> tuple[ReportSection, ...]:
        def build() -> tuple[ReportSection, ...]:
            built: list[ReportSection] = []
            names: set[Identifier] = set()
            for index, spec in enumerate(self._listed("sections")):
                where = f"adapters.sections[{index}]"
                section = self._make(
                    where, "sections", spec, ReportSection, self._options(spec), self._context
                )
                name, version = section.name, section.version
                if not isinstance(name, Identifier):
                    raise self._fail(where, f"name {name!r} is not an Identifier")
                if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                    raise self._fail(where, f"version {version!r} is not a positive int")
                if name in names:
                    raise self._fail("adapters.sections", f"two sections are named {name!r}")
                names.add(name)
                built.append(section)
            return tuple(built)

        return self._cached("sections", build)

    def analysis_tools(self) -> AnalysisTools:
        return self._cached(
            "analysis_tools",
            lambda: AnalysisTools(
                self.mixer(), self.measurer(), self.visualizer(), self.sections()
            ),
        )

    def instrument_notes(self) -> tuple[InstrumentNote, ...]:
        """Each instrument's name, adapter, and note, building nothing."""
        return tuple(
            InstrumentNote(item.name, item.spec.use, item.guide)
            for item in self._config.instruments
        )

    def guide_parts(self) -> tuple[str, ...]:
        """Build every adapter and return each guided one's guide, once each, in guide order."""
        adapters: list[tuple[str, object]] = [
            (f"instruments.{name}", instrument) for name, instrument in self.instruments().items()
        ]
        adapters += [(f"effects.{name}", effect) for name, effect in self.effects().items()]
        adapters += [(f"adapters.song_formats[{i}]", f) for i, f in enumerate(self.song_formats())]
        adapters += [
            ("adapters.record_writer", self.record_writer()),
            ("adapters.audio_files", self.audio_files()),
            ("adapters.render_store", self.render_store()),
            ("adapters.mixer", self.mixer()),
            ("adapters.measurer", self.measurer()),
            ("adapters.visualizer", self.visualizer()),
        ]
        adapters += [(f"adapters.sections[{i}]", s) for i, s in enumerate(self.sections())]
        parts: list[str] = []
        for where, adapter in adapters:
            if not isinstance(adapter, Guided):
                continue
            guide = adapter.guide
            if not (isinstance(guide, str) and guide.strip().startswith("## ")):
                raise self._fail(where, "guide does not start with a '## ' heading")
            if guide not in parts:
                parts.append(guide)
        return tuple(parts)


def load(path: Path, sounds_dir: Path, renders_dir: Path) -> Composition:
    """Read a workspace config and return its composition, which builds adapters on first use.

    Factories receive `path`'s directory as `context.root` and the directories the running command
    uses. Raises ConfigError for a config it cannot use; imports no adapter.
    """
    config = read_config(path)
    context = AdapterContext(path.absolute().parent, sounds_dir, renders_dir)
    return Composition(config, context)
