"""The composition root: builds a workspace's adapters from its `poser.toml`.

This is the only module that knows which adapters exist. It names poser's own adapters by import
path, so importing it loads no adapter library, and building imports only the adapters a command
uses.

A workspace configures what it can vary: its instruments and effects, and the `[adapters]` lists
`song_formats`, `frameworks`, and `vocabularies`. Each names a built-in in `BUILTINS` or any
other adapter by import path, `module:attribute`, naming a factory:

- an instrument's or effect's factory takes `(name, options, context)`;
- a song format's, framework's, or vocabulary's takes `(options, context)`.

The record writer, audio files, render store, mixer, measurer, visualizer, and piano-roll drawer
have one implementation each. They are fixed parts, built from the import paths in `FIXED`.

A factory raises ConfigError for options it cannot use; any other exception is a bug in the
adapter and propagates. Building an adapter loads no plugin, starts no process, writes no file,
and reads nothing but its arguments and imported modules, so a guide can be composed on a
machine without the plugins.

Deferred until adapters are published by other people: discovering adapters through entry points
(`importlib.metadata.entry_points(group="poser.adapters")`) and a versioned adapter contract.
"""

import copy
import inspect
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast, get_protocol_members

from poser.adapters.loading import load_object
from poser.core.analysis import AnalysisTools
from poser.core.guide import FrameworkListing, GuideBlock, InstrumentNote
from poser.core.jsonvalue import JsonValue, is_json_value
from poser.core.marks import LAYER_PATTERN
from poser.core.names import Identifier, Suffix
from poser.core.ports import (
    AdapterContext,
    AdviceProvider,
    AudioFiles,
    ConfigError,
    DeliveryWriter,
    Effect,
    Framework,
    Guided,
    Instrument,
    Measurer,
    Mixer,
    PianoRollDrawer,
    RecordWriter,
    RenderStore,
    SongFormat,
    Visualizer,
    Vocabulary,
    WorkspaceInitializer,
)


def workspace_initializer(directory: Path) -> WorkspaceInitializer:
    """Build the filesystem transaction owner without performing I/O."""
    factory = cast(
        "Callable[[Path], WorkspaceInitializer]",
        load_object("poser.adapters.workspace_init:create"),
    )
    return factory(directory)


INSTRUMENT = "instrument"
EFFECT = "effect"
# Marks a member an adapter does not have, which no real member value can be.
_ABSENT = object()
_NO_BUILTINS: Mapping[str, str] = MappingProxyType[str, str]({})
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
        "frameworks": MappingProxyType({"tonal": "poser.adapters.tonal:create_tonal"}),
        "vocabularies": MappingProxyType(
            {
                "chords": "poser.adapters.chords:create_chords",
                "key": "poser.adapters.tonal:create_key",
            }
        ),
    }
)
# The parts with one implementation each, by import path; no `poser.toml` key selects them. The
# render store's factory takes `(options, context, stem_suffix)`, every other one
# `(options, context)`.
FIXED: Mapping[str, str] = MappingProxyType(
    {
        "record_writer": "poser.adapters.song_formats:create_yaml",
        "audio_files": "poser.adapters.wav:create",
        "render_store": "poser.adapters.filestore:create",
        "mixer": "poser.adapters.mixing:create",
        "measurer": "poser.adapters.measuring:create",
        "visualizer": "poser.adapters.spectrogram:create",
        "piano_roll_drawer": "poser.adapters.piano_roll:build_piano_roll_drawer",
    }
)
SONG_FORMATS = "song_formats"
DEFAULT_SONG_FORMATS = ("yaml", "script")
FRAMEWORKS = "frameworks"
VOCABULARIES = "vocabularies"
# The list keys a preset fills, each with the singular its messages use.
PRESET_KEYS: Mapping[str, str] = MappingProxyType(
    {FRAMEWORKS: "framework", VOCABULARIES: "vocabulary"}
)
# Named sets of frameworks and vocabularies, by list key. A preset is poser's current
# recommendation and may gain members in later versions; readings and parsed marks are never
# stored, so a changed preset stales no data. A studio pins its frameworks or vocabularies by
# listing them.
PRESETS: Mapping[str, Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "western": MappingProxyType({FRAMEWORKS: ("tonal",), VOCABULARIES: ("chords", "key")}),
        "neutral": MappingProxyType({FRAMEWORKS: (), VOCABULARIES: ()}),
    }
)
DEFAULT_PRESET = "western"
TOP_LEVEL_KEYS = frozenset({"instruments", "effects", "adapters", "guidance"})
ADAPTER_KEY = "adapter"  # An instrument table's spelling of `use`.
UPGRADING_URL = "https://github.com/isnbh0/poser/blob/main/docs/upgrading.md"
GUIDE_KEY = "guide"  # An instrument table's note for the guide; compose's, not the adapter's.
USE_KEY = "use"
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
IMPORT_PATH = re.compile(rf"{_NAME}(\.{_NAME})*:{_NAME}(\.{_NAME})*")


class _Recipe(Protocol):
    """What the instrument and effect builders check on what they build."""

    @property
    def adapter_id(self) -> Identifier: ...

    @property
    def adapter_version(self) -> str: ...


class _Named(Protocol):
    """What the framework and vocabulary builders check on what they build."""

    @property
    def name(self) -> Identifier: ...


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
class GuidanceConfig:
    advice: bool = True


@dataclass(frozen=True)
class WorkspaceConfig:
    path: Path
    instruments: tuple[InstrumentSpec, ...]
    effects: tuple[EffectSpec, ...]
    # Every `[adapters]` list, with defaults filled in.
    adapters: Mapping[str, tuple[AdapterSpec, ...]]
    guidance: GuidanceConfig = field(default_factory=GuidanceConfig)
    # The preset `[adapters] frameworks` names, or None for an explicit list.
    preset: str | None = None
    # The preset `[adapters] vocabularies` names, or None for an explicit list.
    vocabulary_preset: str | None = None

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
        if guides and "advice" in body:
            raise ConfigError(
                f"{path}: {owner}.advice: reserved; optional advice belongs in adapter package data"
            )
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


def _preset_list(path: Path, key: str, raw: object) -> tuple[tuple[AdapterSpec, ...], str | None]:
    """Read `[adapters] frameworks` or `vocabularies`: a preset name, or a list of specs like
    `song_formats`."""
    where = f"adapters.{key}"
    if isinstance(raw, str):
        if raw not in PRESETS:
            presets = ", ".join(sorted(PRESETS))
            problem = f"unknown preset {raw!r}; presets are {presets}"
            if raw in BUILTINS[key]:
                problem += f'; to select the {raw} {PRESET_KEYS[key]} alone, write ["{raw}"]'
            raise ConfigError(f"{path}: {where}: {problem}")
        return tuple(AdapterSpec(use) for use in PRESETS[raw][key]), raw
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: {where}: must be a preset name or a list")
    items = cast("list[object]", raw)
    specs = tuple(_spec(path, f"{where}[{index}]", key, item) for index, item in enumerate(items))
    return specs, None


def _adapters(
    path: Path, value: object
) -> tuple[dict[str, tuple[AdapterSpec, ...]], dict[str, str | None]]:
    """Read `[adapters]`: every list's specs, and the preset each preset key names, if any."""
    table = _table(value)
    if table is None:
        raise ConfigError(f"{path}: adapters: must be a table")
    for key in table:
        if key != SONG_FORMATS and key not in PRESET_KEYS:
            raise ConfigError(f"{path}: adapters.{key}: unknown key")
    where = f"adapters.{SONG_FORMATS}"
    raw = table.get(SONG_FORMATS, list(DEFAULT_SONG_FORMATS))
    if not isinstance(raw, list):
        raise ConfigError(f"{path}: {where}: must be a list")
    items = cast("list[object]", raw)
    # A workspace with no song format can load no song.
    if not items:
        raise ConfigError(f"{path}: {where}: must name at least one adapter")
    adapters = {
        SONG_FORMATS: tuple(
            _spec(path, f"{where}[{index}]", SONG_FORMATS, item) for index, item in enumerate(items)
        )
    }
    presets: dict[str, str | None] = {}
    for key in PRESET_KEYS:
        adapters[key], presets[key] = _preset_list(path, key, table.get(key, DEFAULT_PRESET))
    return adapters, presets


def _guidance(path: Path, value: object) -> GuidanceConfig:
    table = _table(value)
    if table is None:
        raise ConfigError(f"{path}: guidance: must be a table")
    for key in table:
        if key != "advice":
            raise ConfigError(f"{path}: guidance.{key}: unknown key")
    advice = table.get("advice", True)
    if not isinstance(advice, bool):
        raise ConfigError(f"{path}: guidance.advice: must be a boolean")
    return GuidanceConfig(advice)


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
                f"{path}: {key}: unknown key; "
                "use `instruments`, `effects`, `adapters`, and `guidance`"
            )
    if "instruments" not in data:
        raise ConfigError(f"{path}: instruments: missing; name at least one instrument")
    instruments = _instruments(path, data["instruments"])
    effects = _effects(path, data.get("effects", {}))
    adapters, presets = _adapters(path, data.get("adapters", {}))
    return WorkspaceConfig(
        path,
        instruments,
        effects,
        adapters,
        _guidance(path, data.get("guidance", {})),
        preset=presets[FRAMEWORKS],
        vocabulary_preset=presets[VOCABULARIES],
    )


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
        use: str,
        port: type[T],
        *args: object,
        builtins: Mapping[str, str] = _NO_BUILTINS,
        owner: str | None = None,
    ) -> T:
        """Build one adapter from `use`, a name in `builtins` or an import path, naming `where` in
        any ConfigError.

        A factory's error that starts with `owner`, such as `instrument 'serum': `, drops it, so
        the message names the instrument once.
        """
        target = builtins.get(use, use)
        try:
            factory = load_object(target)
        except ConfigError as error:
            raise self._fail(where, str(error)) from error
        if not callable(factory):
            raise self._fail(where, f"{use!r} is not a factory")
        try:
            made = factory(*args)
        except ConfigError as error:
            problem = str(error)
            if owner is not None:
                problem = problem.removeprefix(f"{owner}: ")
            raise self._fail(where, problem) from error
        if not isinstance(made, port):
            # `isinstance` looks members up statically, so this names exactly what it missed.
            lacking = sorted(
                member
                for member in get_protocol_members(port)
                if inspect.getattr_static(made, member, _ABSENT) is _ABSENT
            )
            reason = (
                f": it lacks {', '.join(lacking)}; if it was written for an earlier poser, see "
                f"{UPGRADING_URL}"
                if lacking
                else ""
            )
            article = "an" if port.__name__[0] in "AEIOU" else "a"
            raise self._fail(
                where,
                f"{use!r} made a {type(made).__name__}, which is not {article} "
                f"{port.__name__}{reason}",
            )
        return made

    def _configured[T](self, where: str, key: str, spec: AdapterSpec, port: type[T]) -> T:
        """Build a configured list entry, by built-in name or import path."""
        return self._make(
            where, spec.use, port, _read_only(spec.options), self._context, builtins=BUILTINS[key]
        )

    def _fixed[T](self, key: str, port: type[T], *extra: object) -> T:
        """Build a fixed part from its `FIXED` import path, with empty options, once."""
        return self._cached(
            key, lambda: self._make(key, FIXED[key], port, _read_only({}), self._context, *extra)
        )

    def _suffix(self, where: str, name: str, value: object) -> None:
        if not isinstance(value, Suffix):
            raise self._fail(where, f"{name} {value!r} is not a Suffix")

    def _cached[T](self, key: str, build: Callable[[], T]) -> T:
        if key not in self._built:
            self._built[key] = build()
        return cast("T", self._built[key])

    def _recipes[T: _Recipe](
        self, kind: str, items: Sequence[InstrumentSpec | EffectSpec], port: type[T]
    ) -> Mapping[Identifier, T]:
        """Build every instrument or effect, by name, in config order."""
        built: dict[Identifier, T] = {}
        for item in items:
            where = f"{kind}s.{item.name}"
            made = self._make(
                where,
                item.spec.use,
                port,
                item.name,
                _read_only(item.spec.options),
                self._context,
                builtins=BUILTINS[kind],
                owner=f"{kind} {item.name!r}",
            )
            if not isinstance(made.adapter_id, Identifier):
                raise self._fail(where, f"adapter_id {made.adapter_id!r} is not an Identifier")
            version = made.adapter_version
            if not (isinstance(version, str) and version):
                raise self._fail(where, f"adapter_version {version!r} is not a non-empty str")
            built[item.name] = made
        return MappingProxyType(built)

    def instruments(self) -> Mapping[Identifier, Instrument]:
        """Every instrument, by name, in config order."""
        return self._cached(
            "instruments", lambda: self._recipes(INSTRUMENT, self._config.instruments, Instrument)
        )

    def effects(self) -> Mapping[Identifier, Effect]:
        """Every effect recipe, by name, in config order."""
        return self._cached("effects", lambda: self._recipes(EFFECT, self._config.effects, Effect))

    def song_formats(self) -> tuple[SongFormat, ...]:
        def build() -> tuple[SongFormat, ...]:
            built: list[SongFormat] = []
            claimed: dict[Suffix, str] = {}
            for index, spec in enumerate(self._config.adapters[SONG_FORMATS]):
                where = f"adapters.{SONG_FORMATS}[{index}]"
                fmt = self._configured(where, SONG_FORMATS, spec, SongFormat)
                suffixes = fmt.suffixes
                if not isinstance(suffixes, Sequence) or isinstance(suffixes, str):
                    raise self._fail(where, f"suffixes {suffixes!r} is not a tuple of Suffix")
                for suffix in cast("Sequence[object]", suffixes):
                    self._suffix(where, "suffixes entry", suffix)
                    owner = claimed.get(cast("Suffix", suffix))
                    if owner is not None:
                        raise self._fail(
                            f"adapters.{SONG_FORMATS}",
                            f"{suffix!r} is read by both {owner!r} and {spec.use!r}",
                        )
                    claimed[cast("Suffix", suffix)] = spec.use
                built.append(fmt)
            return tuple(built)

        return self._cached(SONG_FORMATS, build)

    def _named[T: _Named](
        self, key: str, noun: str, port: type[T], check: Callable[[str, Identifier], None]
    ) -> tuple[T, ...]:
        """Build a framework or vocabulary list in configured order, with unique names that pass
        `check(where, name)`."""
        built: list[T] = []
        names: set[Identifier] = set()
        for index, spec in enumerate(self._config.adapters[key]):
            where = f"adapters.{key}[{index}]"
            made = self._configured(where, key, spec, port)
            name = made.name
            if not isinstance(name, Identifier):
                raise self._fail(where, f"name {name!r} is not an Identifier")
            check(where, name)
            if name in names:
                raise self._fail(f"adapters.{key}", f"two {noun} are named {name!r}")
            names.add(name)
            built.append(made)
        return tuple(built)

    def _layer_name(self, where: str, name: Identifier) -> None:
        if not LAYER_PATTERN.fullmatch(name):
            raise self._fail(
                where,
                f"name {name!r} is not a layer name: lowercase letters, digits, _ and -, "
                "starting with a letter or digit",
            )

    def frameworks(self) -> tuple[Framework, ...]:
        """Every configured framework, in configured order."""
        return self._cached(
            FRAMEWORKS,
            lambda: self._named(FRAMEWORKS, "frameworks", Framework, lambda _where, _name: None),
        )

    def vocabularies(self) -> tuple[Vocabulary, ...]:
        """Every configured vocabulary, in configured order."""
        return self._cached(
            VOCABULARIES,
            lambda: self._named(VOCABULARIES, "vocabularies", Vocabulary, self._layer_name),
        )

    def piano_roll_drawer(self) -> PianoRollDrawer:
        return self._fixed("piano_roll_drawer", PianoRollDrawer)

    def record_writer(self) -> RecordWriter:
        return self._fixed("record_writer", RecordWriter)

    def audio_files(self) -> AudioFiles:
        return self._fixed("audio_files", AudioFiles)

    def render_store(self) -> RenderStore:
        return self._fixed("render_store", RenderStore, self.audio_files().suffix)

    def mixer(self) -> Mixer:
        return self._fixed("mixer", Mixer)

    def measurer(self) -> Measurer:
        return self._fixed("measurer", Measurer)

    def visualizer(self) -> Visualizer:
        return self._fixed("visualizer", Visualizer)

    def framework_listing(self) -> FrameworkListing:
        """The presets and the configured frameworks' and vocabularies' names, for the generated
        guide."""
        preset = self._config.preset
        names = tuple(framework.name for framework in self.frameworks())
        vocabulary_preset = self._config.vocabulary_preset
        return FrameworkListing(
            preset,
            names,
            default=preset == DEFAULT_PRESET,
            vocabulary_preset=vocabulary_preset,
            vocabularies=tuple(vocabulary.name for vocabulary in self.vocabularies()),
            vocabulary_default=vocabulary_preset == DEFAULT_PRESET,
        )

    def analysis_tools(self) -> AnalysisTools:
        return self._cached(
            "analysis_tools",
            lambda: AnalysisTools(self.mixer(), self.measurer(), self.visualizer()),
        )

    def instrument_notes(self) -> tuple[InstrumentNote, ...]:
        """Each instrument's name, adapter, and note, building nothing."""
        return tuple(
            InstrumentNote(item.name, item.spec.use, item.guide)
            for item in self._config.instruments
        )

    def guide_blocks(self) -> tuple[GuideBlock, ...]:
        """Collect explicitly classified package-only adapter content in guide order."""
        adapters: list[tuple[str, object]] = [
            (f"instruments.{name}", instrument) for name, instrument in self.instruments().items()
        ]
        adapters += [(f"effects.{name}", effect) for name, effect in self.effects().items()]
        adapters += [(f"adapters.song_formats[{i}]", f) for i, f in enumerate(self.song_formats())]
        adapters += [
            ("record_writer", self.record_writer()),
            ("audio_files", self.audio_files()),
            ("render_store", self.render_store()),
            ("mixer", self.mixer()),
            ("measurer", self.measurer()),
            ("visualizer", self.visualizer()),
        ]
        adapters += [(f"adapters.{FRAMEWORKS}[{i}]", f) for i, f in enumerate(self.frameworks())]
        adapters += [
            (f"adapters.{VOCABULARIES}[{i}]", v) for i, v in enumerate(self.vocabularies())
        ]
        parts: list[GuideBlock] = []
        for where, adapter in adapters:
            for attribute, supported in (
                ("guide", isinstance(adapter, Guided)),
                ("advice", isinstance(adapter, AdviceProvider)),
            ):
                if not supported:
                    continue
                text = getattr(adapter, attribute)
                if not (isinstance(text, str) and text.strip().startswith("## ")):
                    raise self._fail(where, f"{attribute} does not start with a '## ' heading")
                block = GuideBlock(text, advice=attribute == "advice")
                if block not in parts:
                    parts.append(block)
        return tuple(parts)


def delivery_writer() -> DeliveryWriter:
    """Build the default delivery publisher without performing filesystem work."""
    factory = load_object("poser.adapters.delivery:create")
    if not callable(factory):
        raise ConfigError("delivery writer factory is not callable")
    writer = factory()
    if not isinstance(writer, DeliveryWriter):
        raise ConfigError("delivery writer does not implement DeliveryWriter")
    return writer


def load(path: Path, sounds_dir: Path, renders_dir: Path) -> Composition:
    """Read a workspace config and return its composition, which builds adapters on first use.

    Factories receive `path`'s directory as `context.root` and the directories the running command
    uses. Raises ConfigError for a config it cannot use; imports no adapter.
    """
    config = read_config(path)
    context = AdapterContext(path.absolute().parent, sounds_dir, renders_dir)
    return Composition(config, context)
