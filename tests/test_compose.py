"""Tests for `poser.compose`: reading `poser.toml` and building the adapters it selects."""

import argparse
import importlib
import re
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, NoReturn

import pytest
from helpers import SURGE_DIR

from poser.adapters.chords import GUIDE as CHORDS_GUIDE
from poser.adapters.chords import ChordsVocabulary
from poser.adapters.filestore import FileRenderStore
from poser.adapters.measuring import StandardMeasurer
from poser.adapters.mixing import GainMixer
from poser.adapters.pedalboard.instrument import PedalboardInstrument
from poser.adapters.song_formats import ScriptSongFormat, YamlSongFormat
from poser.adapters.spectrogram import SpectrogramVisualizer
from poser.adapters.tonal import GUIDE as TONAL_GUIDE
from poser.adapters.tonal import KEY_GUIDE, KeyVocabulary, TonalFramework
from poser.adapters.wav import WavAudioFiles
from poser.cli.common import load_composition
from poser.compose import (
    PRESETS,
    UPGRADING_URL,
    AdapterSpec,
    Composition,
    load,
    parse_config,
    read_config,
)
from poser.core.names import Identifier
from poser.core.ports import AdapterContext, ConfigError

if TYPE_CHECKING:
    from poser.core.jsonvalue import JsonValue

# Test adapters, selected by import path from a module written into `tmp_path`.
ADAPTERS = """
from fakes import (
    FakeInstrument,
    FakeMixer,
    FakeSection,
    FakeSongFormat,
    FakeSound,
    FakeVisualizer,
)

from poser.core.names import Identifier, Suffix

SEEN = []  # (factory, args) for every call.
NOT_A_FACTORY = 3


class MetaInstrument(FakeInstrument):
    def __init__(self, name, adapter_id, adapter_version):
        super().__init__(name, {None: FakeSound()})
        self._meta = (adapter_id, adapter_version)

    @property
    def adapter_id(self):
        return self._meta[0]

    @property
    def adapter_version(self):
        return self._meta[1]


def instrument(name, options, context):
    SEEN.append(("instrument", (name, options, context)))
    adapter_id = options.get("adapter_id")
    return MetaInstrument(
        name,
        Identifier("fake") if adapter_id is None else adapter_id,
        options.get("adapter_version", "1"),
    )


class PitchlessInstrument:
    # An instrument that does not declare `pitches`.

    @property
    def adapter_id(self):
        return Identifier("fake")

    @property
    def adapter_version(self):
        return "1"

    def sounds(self):
        raise AssertionError("never listed")

    def resolve(self, ref):
        raise AssertionError("never resolved")

    def render(self, request, timeout_s):
        raise AssertionError("never rendered")


def pitchless_instrument(name, options, context):
    return PitchlessInstrument()


class MetaEffect:
    def __init__(self, name, adapter_id, adapter_version):
        self._id = adapter_id
        self._version = adapter_version

    @property
    def adapter_id(self):
        return self._id

    @property
    def adapter_version(self):
        return self._version

    def resolve(self, recipe):
        from poser.core.jsonvalue import Json
        from poser.core.ports import ResolvedEffect

        return ResolvedEffect(recipe, "fake effect", Json.of({}))

    def render(self, request, timeout_s):
        return None


def effect(name, options, context):
    SEEN.append(("effect", (name, options, context)))
    return MetaEffect(name, options.get("adapter_id"), options.get("adapter_version"))


def mixer(options, context):
    SEEN.append(("mixer", (options, context)))
    return FakeMixer()


class PlainSuffixVisualizer(FakeVisualizer):
    @property
    def suffix(self):
        return "png"


def visualizer(options, context):
    return PlainSuffixVisualizer()


class YamlFormat(FakeSongFormat):
    @property
    def suffixes(self):
        return (Suffix(".yaml"),)


class PlainSuffixFormat(FakeSongFormat):
    @property
    def suffixes(self):
        return (".json",)


def song_format(options, context):
    return FakeSongFormat()


def yaml_format(options, context):
    return YamlFormat()


def plain_suffix_format(options, context):
    return PlainSuffixFormat()


class PlainNameSection(FakeSection):
    @property
    def name(self):
        return str(self._name)


def section(options, context):
    return FakeSection(options.get("name", "probe"), version=options.get("version", 1))


def plain_name_section(options, context):
    return PlainNameSection("probe")


def not_a_port(options, context):
    return object()


def not_an_effect(name, options, context):
    return object()


def raises(options, context):
    raise RuntimeError("the adapter has a bug")


class Vocabulary:
    def __init__(self, name):
        self._name = name

    @property
    def name(self):
        return self._name

    @property
    def guide(self):
        return f"## {self._name} labels\\n"

    def parse(self, text):
        from poser.core.reading import Rejected

        return Rejected("reads nothing")


def vocabulary(options, context):
    SEEN.append(("vocabulary", (options, context)))
    name = options.get("name", "chords")
    return Vocabulary(name if options.get("plain") else Identifier(name))
"""

SERUM = """
[instruments.serum]
adapter = "pedalboard"
path = "Missing.vst3"
"""


def write_config(tmp_path: Path, text: str, module: str = "") -> Path:
    path = tmp_path / "poser.toml"
    path.write_text(textwrap.dedent(text).format(module=module))
    return path


def adapters_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    name = f"compose_adapters_{uuid.uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(ADAPTERS)
    monkeypatch.syspath_prepend(str(tmp_path))
    return importlib.import_module(name)


def compose(tmp_path: Path, text: str, module: str = "") -> Composition:
    path = write_config(tmp_path, text, module)
    return load(path, tmp_path / "sounds", tmp_path / "renders")


def files_under(root: Path) -> set[Path]:
    return {path for path in root.rglob("*") if "__pycache__" not in path.parts}


# Reading


def test_the_reference_config_loads_one_pedalboard_instrument() -> None:
    config = read_config(SURGE_DIR / "poser.toml")

    assert [(item.name, item.spec.use) for item in config.instruments] == [("surge", "pedalboard")]
    assert config.instruments[0].guide is not None
    assert "guide" not in config.instruments[0].spec.options


def test_text_parser_matches_path_reader(tmp_path: Path) -> None:
    path = tmp_path / "poser.toml"
    path.write_text(SERUM)
    assert parse_config(SERUM, path) == read_config(path)


@pytest.mark.parametrize("text", ["[instruments]\n", "instruments = ["])
def test_text_parser_matches_path_reader_errors(tmp_path: Path, text: str) -> None:
    path = tmp_path / "poser.toml"
    path.write_text(text)
    with pytest.raises(ConfigError) as parse_error:
        parse_config(text, path)
    with pytest.raises(ConfigError) as read_error:
        read_config(path)
    assert str(read_error.value) == str(parse_error.value)


def test_text_config_anchors_relative_paths_to_future_workspace(tmp_path: Path) -> None:
    root = tmp_path / "future"
    path = root / "poser.toml"
    config = parse_config(SERUM, path)
    context = AdapterContext(root, root / "sounds", root / "renders")
    instrument = Composition(config, context).instruments()[Identifier("serum")]
    assert isinstance(instrument, PedalboardInstrument)
    assert instrument._config.path == root / "Missing.vst3"
    with pytest.raises(ConfigError, match=re.escape(str(path))):
        parse_config("[instruments]", path)
    assert not root.exists()


def test_a_config_without_adapters_builds_the_built_in_adapters(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM)

    assert [type(fmt) for fmt in composition.song_formats()] == [YamlSongFormat, ScriptSongFormat]
    assert isinstance(composition.record_writer(), YamlSongFormat)
    assert isinstance(composition.audio_files(), WavAudioFiles)
    assert isinstance(composition.render_store(), FileRenderStore)
    assert isinstance(composition.mixer(), GainMixer)
    assert isinstance(composition.measurer(), StandardMeasurer)
    assert isinstance(composition.visualizer(), SpectrogramVisualizer)
    assert composition.sections() == ()
    assert [type(framework) for framework in composition.frameworks()] == [TonalFramework]
    assert isinstance(composition.instruments()[Identifier("serum")], PedalboardInstrument)


CONFIG_ERRORS = {
    "not-toml": ("instruments = [", "", "cannot read: "),
    "unknown-top-level-key": ("plugins = 1\n" + SERUM, "plugins", "unknown key"),
    "no-instruments": ("[adapters]\n", "instruments", "missing"),
    "instruments-not-a-table": ("instruments = 3\n", "instruments", "must be a table"),
    "no-instrument": ("[instruments]\n", "instruments", "must name at least one"),
    "bad-name": (
        '[instruments."bad name"]\nadapter = "pedalboard"\n',
        "instruments.bad name",
        "does not match",
    ),
    "name-differs-in-case": (
        SERUM + '[instruments.Serum]\nadapter = "pedalboard"\n',
        "instruments.Serum",
        "differs only in case from 'serum'",
    ),
    "instrument-not-a-table": ("[instruments]\nserum = 3\n", "instruments.serum", "a table"),
    "no-adapter": ("[instruments.serum]\npath = 'x'\n", "instruments.serum.adapter", "string"),
    "adapter-not-a-string": (
        "[instruments.serum]\nadapter = 3\n",
        "instruments.serum.adapter",
        "must be a string",
    ),
    "unknown-adapter": (
        "[instruments.serum]\nadapter = 'vst'\n",
        "instruments.serum.adapter",
        "'vst' is not a built-in ('pedalboard') or an import path module:attribute",
    ),
    "blank-guide": (SERUM + "guide = ' '\n", "instruments.serum.guide", "non-blank text"),
    "guide-not-a-string": (SERUM + "guide = 3\n", "instruments.serum.guide", "non-blank text"),
    "option-not-json": (SERUM + "when = 1979-05-27\n", "instruments.serum.when", "not a JSON"),
    "adapters-not-a-table": (SERUM.replace("[", "adapters = 1\n[", 1), "adapters", "a table"),
    "unknown-adapters-key": (SERUM + "[adapters]\nstore = 'files'\n", "adapters.store", "unknown"),
    "table-without-use": (
        SERUM + "[adapters]\nmixer = {{ gain = 1 }}\n",
        "adapters.mixer",
        "a table with a string `use`",
    ),
    "list-key-not-a-list": (
        SERUM + "[adapters]\nsong_formats = 'yaml'\n",
        "adapters.song_formats",
        "must be a list",
    ),
    "no-song-format": (
        SERUM + "[adapters]\nsong_formats = []\n",
        "adapters.song_formats",
        "must name at least one adapter",
    ),
    "unknown-built-in": (
        SERUM + "[adapters]\nmixer = 'loud'\n",
        "adapters.mixer",
        "'loud' is not a built-in ('gain') or an import path",
    ),
    "section-not-an-import-path": (
        SERUM + "[adapters]\nsections = ['probe']\n",
        "adapters.sections[0]",
        "'probe' is not an import path module:attribute",
    ),
    "effects-not-a-table": ("effects = 3\n" + SERUM, "effects", "must be a table of effects"),
    "effect-bad-name": (
        SERUM + '[effects."bad name"]\nadapter = "pedalboard"\nbuiltin = "Gain"\n',
        "effects.bad name",
        "does not match",
    ),
    "effect-name-differs-in-case": (
        SERUM
        + '[effects.Drive]\nadapter = "pedalboard"\nbuiltin = "Gain"\n'
        + '[effects.drive]\nadapter = "pedalboard"\nbuiltin = "Gain"\n',
        "effects.drive",
        "differs only in case from 'Drive'",
    ),
    "effect-not-a-table": (SERUM + "[effects]\ndrive = 3\n", "effects.drive", "a table"),
    "no-effect-adapter": (
        SERUM + "[effects.drive]\nbuiltin = 'Gain'\n",
        "effects.drive.adapter",
        "string",
    ),
    "unknown-effect-adapter": (
        SERUM + '[effects.drive]\nadapter = "loudness"\n',
        "effects.drive.adapter",
        "'loudness' is not a built-in ('pedalboard') or an import path",
    ),
    "effect-option-not-json": (
        SERUM + '[effects.drive]\nadapter = "pedalboard"\nbuiltin = "Gain"\nwhen = 1979-05-27\n',
        "effects.drive.when",
        "not a JSON",
    ),
}


@pytest.mark.parametrize(("text", "where", "problem"), CONFIG_ERRORS.values(), ids=CONFIG_ERRORS)
def test_load_refuses_a_config_naming_the_file_and_key(
    tmp_path: Path, text: str, where: str, problem: str
) -> None:
    path = write_config(tmp_path, text)

    with pytest.raises(ConfigError) as error:
        load(path, tmp_path / "sounds", tmp_path / "renders")

    assert str(error.value).startswith(f"{path}: {where}")
    assert problem in str(error.value)


def test_load_refuses_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"poser\.toml: cannot read: "):
        load(tmp_path / "poser.toml", tmp_path / "sounds", tmp_path / "renders")


# Building


def refuse(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError(f"building an adapter started a process: {args} {kwargs}")


def test_building_every_built_in_adapter_starts_no_process_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = SERUM + "[instruments.serum.sources]\npresets = 'presets'\n"
    composition = compose(tmp_path, text)
    before = files_under(tmp_path)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)

    composition.instruments()
    composition.effects()
    composition.song_formats()
    composition.record_writer()
    composition.render_store()
    composition.analysis_tools()
    assert composition.guide_parts()

    assert files_under(tmp_path) == before


def context_seen(module: ModuleType) -> AdapterContext:
    ((_, (_, _, context)),) = [call for call in module.SEEN if call[0] == "instrument"]
    return context


def test_the_context_carries_the_command_s_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    text = '[instruments.x]\nadapter = "{module}:instrument"\n'
    path = write_config(tmp_path, text, module.__name__)
    path = path.relative_to(tmp_path)
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(config=path, sounds_dir=Path("s"), output_dir=Path("/r"))

    load_composition(args).instruments()

    assert context_seen(module) == AdapterContext(tmp_path, Path("s"), Path("/r"))


def test_the_context_defaults_to_the_root_s_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    text = '[instruments.x]\nadapter = "{module}:instrument"\n'
    path = write_config(tmp_path, text, module.__name__)

    load_composition(argparse.Namespace(config=path)).instruments()

    assert context_seen(module) == AdapterContext(
        tmp_path, tmp_path / "sounds", tmp_path / "renders"
    )


def test_import_paths_build_test_adapters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    composition = compose(
        tmp_path,
        """
        [instruments.synth]
        adapter = "{module}:instrument"

        [adapters]
        mixer = "{module}:mixer"
        song_formats = ["yaml", "{module}:song_format"]
        sections = ["{module}:section"]
        """,
        module.__name__,
    )

    assert type(composition.instruments()[Identifier("synth")]) is module.MetaInstrument
    assert type(composition.mixer()).__name__ == "FakeMixer"
    assert [type(fmt).__name__ for fmt in composition.song_formats()] == [
        "YamlSongFormat",
        "FakeSongFormat",
    ]
    assert [section.name for section in composition.sections()] == ["probe"]


def one_mixer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use: str) -> Composition:
    module = adapters_module(tmp_path, monkeypatch)
    text = SERUM + f'[adapters]\nmixer = "{use}"\n'
    return compose(tmp_path, text, module.__name__)


@pytest.mark.parametrize(
    ("use", "problem"),
    [
        ("{module}:not_a_port", "'{module}:not_a_port' made a object, which is not a Mixer"),
        ("{module}:NOT_A_FACTORY", "'{module}:NOT_A_FACTORY' is not a factory"),
        ("{module}:missing", "cannot import '{module}:missing'"),
        ("no_such_module_for_poser:mixer", "cannot import 'no_such_module_for_poser:mixer'"),
    ],
)
def test_a_factory_that_cannot_build_the_port_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use: str, problem: str
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    composition = compose(tmp_path, SERUM + f'[adapters]\nmixer = "{use}"\n', module.__name__)

    with pytest.raises(ConfigError) as error:
        composition.mixer()

    expected = problem.format(module=module.__name__)
    assert str(error.value).startswith(f"{tmp_path / 'poser.toml'}: adapters.mixer: {expected}")


def test_a_factory_raising_another_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    composition = one_mixer(tmp_path, monkeypatch, "{module}:raises")

    with pytest.raises(RuntimeError, match="the adapter has a bug"):
        composition.mixer()


INSTRUMENT_METADATA = {
    "plain-str-adapter-id": ('adapter_id = "fake"', "adapter_id 'fake' is not an Identifier"),
    "empty-adapter-version": ('adapter_version = ""', "adapter_version '' is not a non-empty str"),
}


@pytest.mark.parametrize(
    ("option", "problem"), INSTRUMENT_METADATA.values(), ids=INSTRUMENT_METADATA
)
def test_instrument_metadata_is_checked_when_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str, problem: str
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    text = f'[instruments.synth]\nadapter = "{{module}}:instrument"\n{option}\n'
    composition = compose(tmp_path, text, module.__name__)

    with pytest.raises(ConfigError) as error:
        composition.instruments()

    assert str(error.value) == f"{tmp_path / 'poser.toml'}: instruments.synth: {problem}"


ADAPTER_METADATA = {
    "visualizer-suffix": (
        'visualizer = "{module}:visualizer"',
        "visualizer",
        "adapters.visualizer: suffix 'png' is not a Suffix",
    ),
    "song-format-suffix": (
        'song_formats = ["{module}:plain_suffix_format"]',
        "song_formats",
        "adapters.song_formats[0]: suffixes entry '.json' is not a Suffix",
    ),
    "section-version-true": (
        'sections = [{{ use = "{module}:section", version = true }}]',
        "sections",
        "adapters.sections[0]: version True is not a positive int",
    ),
    "section-version-zero": (
        'sections = [{{ use = "{module}:section", version = 0 }}]',
        "sections",
        "adapters.sections[0]: version 0 is not a positive int",
    ),
    "section-plain-name": (
        'sections = ["{module}:plain_name_section"]',
        "sections",
        "adapters.sections[0]: name 'probe' is not an Identifier",
    ),
    "two-yaml-formats": (
        'song_formats = ["yaml", "{module}:yaml_format"]',
        "song_formats",
        "adapters.song_formats: '.yaml' is read by both 'yaml' and '{module}:yaml_format'",
    ),
    "two-sections-alike": (
        'sections = ["{module}:section", "{module}:section"]',
        "sections",
        "adapters.sections: two sections are named 'probe'",
    ),
}


@pytest.mark.parametrize(
    ("line", "method", "problem"), ADAPTER_METADATA.values(), ids=ADAPTER_METADATA
)
def test_adapter_metadata_is_checked_when_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, method: str, problem: str
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    composition = compose(tmp_path, SERUM + f"[adapters]\n{line}\n", module.__name__)

    with pytest.raises(ConfigError) as error:
        getattr(composition, method)()

    expected = problem.format(module=module.__name__)
    assert str(error.value) == f"{tmp_path / 'poser.toml'}: {expected}"


def test_options_reach_the_factory_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    text = """
    [instruments.synth]
    adapter = "{module}:instrument"
    guide = "A note for the guide."
    level = 3
    layers = {{ names = ["a", "b"] }}
    """
    composition = compose(tmp_path, text, module.__name__)

    composition.instruments()

    ((_, (name, options, _)),) = module.SEEN
    assert name == "synth"
    assert dict(options) == {"level": 3, "layers": {"names": ["a", "b"]}}
    with pytest.raises(TypeError):
        options["level"] = 4
    options["layers"]["names"].append("c")
    assert composition.config.instruments[0].spec.options["layers"] == {"names": ["a", "b"]}


def test_a_built_in_adapter_refuses_an_unknown_option(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM + '[adapters]\nmixer = {{ use = "gain", loud = true }}\n')

    with pytest.raises(ConfigError) as error:
        composition.mixer()

    assert str(error.value) == (
        f"{tmp_path / 'poser.toml'}: adapters.mixer: "
        "unknown option 'loud'; this adapter takes no options"
    )


def test_a_pedalboard_option_error_names_the_key(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM + "loud = true\n")

    with pytest.raises(ConfigError) as error:
        composition.instruments()

    assert str(error.value) == (
        f"{tmp_path / 'poser.toml'}: instruments.serum: unknown option 'loud'"
    )


def test_an_empty_effects_table_is_valid_and_builds_nothing(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM + "[effects]\n")

    assert composition.config.effects == ()
    assert composition.effects() == {}


def test_an_effect_factory_error_names_the_file_and_the_effect(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM + '[effects.drive]\nadapter = "pedalboard"\n')

    with pytest.raises(ConfigError) as error:
        composition.effects()

    assert str(error.value) == (
        f"{tmp_path / 'poser.toml'}: effects.drive: path must be non-empty text"
    )


def test_an_effect_factory_that_cannot_build_the_port_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    composition = compose(
        tmp_path,
        SERUM + '[effects.drive]\nadapter = "{module}:not_an_effect"\n',
        module.__name__,
    )

    with pytest.raises(ConfigError) as error:
        composition.effects()

    assert str(error.value) == (
        f"{tmp_path / 'poser.toml'}: effects.drive: "
        f"'{module.__name__}:not_an_effect' made a object, which is not an Effect: it lacks "
        f"adapter_id, adapter_version, render, resolve; if it was written for an earlier poser, "
        f"see {UPGRADING_URL}"
    )


def test_an_instrument_without_pitches_is_a_config_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    composition = compose(
        tmp_path,
        '[instruments.old]\nadapter = "{module}:pitchless_instrument"\n',
        module.__name__,
    )

    with pytest.raises(ConfigError) as error:
        composition.instruments()

    assert str(error.value) == (
        f"{tmp_path / 'poser.toml'}: instruments.old: "
        f"'{module.__name__}:pitchless_instrument' made a PitchlessInstrument, which is not an "
        f"Instrument: it lacks pitches; if it was written for an earlier poser, see {UPGRADING_URL}"
    )


def test_effects_build_with_their_options_and_metadata_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    composition = compose(
        tmp_path,
        SERUM + '[effects.drive]\nadapter = "{module}:effect"\nadapter_id = "fake"\n',
        module.__name__,
    )

    with pytest.raises(ConfigError) as error:
        composition.effects()

    assert str(error.value) == (
        f"{tmp_path / 'poser.toml'}: effects.drive: adapter_id 'fake' is not an Identifier"
    )


def test_load_imports_no_adapter_module(tmp_path: Path) -> None:
    path = write_config(tmp_path, SERUM)
    script = (
        "import sys\nfrom pathlib import Path\nfrom poser.compose import load\n"
        f"load(Path({str(path)!r}), Path('s'), Path('r'))\n"
        "print(sorted(m for m in sys.modules if m.startswith('poser.adapters')))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )

    assert result.stdout.strip() == "['poser.adapters', 'poser.adapters.loading']"


def test_each_method_builds_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    text = """
    [instruments.synth]
    adapter = "{module}:instrument"

    [adapters]
    mixer = "{module}:mixer"
    """
    composition = compose(tmp_path, text, module.__name__)

    assert composition.instruments() is composition.instruments()
    assert composition.effects() is composition.effects()
    assert composition.mixer() is composition.mixer()
    composition.analysis_tools()
    composition.guide_parts()

    assert [factory for factory, _ in module.SEEN] == ["instrument", "mixer"]


def test_guide_parts_hold_each_guide_once_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    text = """
    [instruments.serum]
    adapter = "pedalboard"
    path = "Missing.vst3"
    guide = "Serum's note."

    [instruments.keys]
    adapter = "pedalboard"
    path = "Missing Too.vst3"

    [adapters]
    sections = ["{module}:section"]
    """
    composition = compose(tmp_path, text, module.__name__)

    parts = composition.guide_parts()

    instruments = composition.instruments()
    assert parts == (
        instruments[Identifier("serum")].guide,  # ty: ignore[unresolved-attribute]
        composition.render_store().guide,  # ty: ignore[unresolved-attribute]
        composition.visualizer().guide,  # ty: ignore[unresolved-attribute]
        "## probe\n\nA fake section.\n",
        TONAL_GUIDE,
        CHORDS_GUIDE,
        KEY_GUIDE,
    )
    for item in composition.config.instruments:
        assert "guide" not in item.spec.options


def test_adapter_spec_options_are_a_read_only_copy() -> None:
    names: list[JsonValue] = ["a"]
    spec = AdapterSpec("gain", {"names": names})
    names.append("b")

    assert spec.options == {"names": ["a"]}
    with pytest.raises(TypeError):
        spec.options["more"] = 1  # ty: ignore[invalid-assignment]


# Frameworks


def test_no_frameworks_key_selects_the_western_preset(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM)

    (framework,) = composition.frameworks()
    assert isinstance(framework, TonalFramework)
    assert framework.name == "tonal"
    assert framework.window_beats == 16
    assert composition.config.preset == "western"
    assert PRESETS["western"]["frameworks"] == ("tonal",)


def test_the_neutral_preset_selects_no_framework(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM + '[adapters]\nframeworks = "neutral"\n')

    assert composition.frameworks() == ()
    assert composition.config.preset == "neutral"
    assert TONAL_GUIDE not in composition.guide_parts()


def test_a_list_pins_frameworks_and_records_no_preset(tmp_path: Path) -> None:
    text = SERUM + '[adapters]\nframeworks = [{{ use = "tonal", window_beats = 8 }}]\n'
    composition = compose(tmp_path, text)

    (framework,) = composition.frameworks()
    assert isinstance(framework, TonalFramework)
    assert framework.window_beats == 8
    assert composition.config.preset is None
    assert compose(tmp_path, SERUM + '[adapters]\nframeworks = ["tonal"]\n').config.preset is None


def test_a_framework_name_in_place_of_a_preset_suggests_the_list(tmp_path: Path) -> None:
    path = write_config(tmp_path, SERUM + '[adapters]\nframeworks = "tonal"\n')

    with pytest.raises(ConfigError) as error:
        read_config(path)

    assert str(error.value) == (
        f"{path}: adapters.frameworks: unknown preset 'tonal'; presets are neutral, western; "
        'to select the tonal framework alone, write ["tonal"]'
    )


@pytest.mark.parametrize(
    ("value", "where", "problem"),
    [
        (
            '"modern"',
            "adapters.frameworks",
            "unknown preset 'modern'; presets are neutral, western",
        ),
        ("3", "adapters.frameworks", "must be a preset name or a list"),
        ('["modal"]', "adapters.frameworks[0]", "'modal' is not a built-in ('tonal')"),
    ],
)
def test_an_unusable_frameworks_value_is_refused(
    tmp_path: Path, value: str, where: str, problem: str
) -> None:
    path = write_config(tmp_path, SERUM + f"[adapters]\nframeworks = {value}\n")

    with pytest.raises(ConfigError, match=re.escape(f"{path}: {where}: {problem}")):
        read_config(path)


@pytest.mark.parametrize(
    ("value", "where", "problem"),
    [
        ('["tonal", "tonal"]', "adapters.frameworks", "two frameworks are named 'tonal'"),
        (
            '[{{ use = "tonal", window_beats = 0.5 }}]',
            "adapters.frameworks[0]",
            "window_beats: must be a finite number of at least 1, got 0.5",
        ),
        (
            '[{{ use = "tonal", window = 8 }}]',
            "adapters.frameworks[0]",
            "unknown option 'window'",
        ),
    ],
    ids=["duplicate-name", "short-window", "unknown-option"],
)
def test_frameworks_are_checked_when_built(
    tmp_path: Path, value: str, where: str, problem: str
) -> None:
    composition = compose(tmp_path, SERUM + f"[adapters]\nframeworks = {value}\n")

    with pytest.raises(
        ConfigError, match=re.escape(f"{tmp_path / 'poser.toml'}: {where}: {problem}")
    ):
        composition.frameworks()


# Vocabularies


def test_no_vocabularies_key_selects_the_western_preset(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM)

    chords, key = composition.vocabularies()
    assert isinstance(chords, ChordsVocabulary)
    assert isinstance(key, KeyVocabulary)
    assert (chords.name, key.name) == ("chords", "key")
    assert composition.config.vocabulary_preset == "western"
    assert PRESETS["western"]["vocabularies"] == ("chords", "key")
    assert CHORDS_GUIDE in composition.guide_parts()
    assert KEY_GUIDE in composition.guide_parts()


def test_the_neutral_preset_selects_no_vocabulary(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM + '[adapters]\nvocabularies = "neutral"\n')

    assert composition.vocabularies() == ()
    assert composition.config.vocabulary_preset == "neutral"
    assert composition.config.preset == "western"
    assert len(composition.frameworks()) == 1
    assert CHORDS_GUIDE not in composition.guide_parts()
    assert KEY_GUIDE not in composition.guide_parts()


def test_a_list_pins_vocabularies_and_records_no_preset(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM + '[adapters]\nvocabularies = ["chords"]\n')

    (vocabulary,) = composition.vocabularies()
    assert isinstance(vocabulary, ChordsVocabulary)
    assert composition.config.vocabulary_preset is None
    assert composition.config.preset == "western"


def test_a_list_of_key_selects_only_the_key_vocabulary(tmp_path: Path) -> None:
    composition = compose(tmp_path, SERUM + '[adapters]\nvocabularies = ["key"]\n')

    (vocabulary,) = composition.vocabularies()
    assert isinstance(vocabulary, KeyVocabulary)
    assert KEY_GUIDE in composition.guide_parts()
    assert CHORDS_GUIDE not in composition.guide_parts()


def test_a_vocabulary_name_in_place_of_a_preset_suggests_the_list(tmp_path: Path) -> None:
    path = write_config(tmp_path, SERUM + '[adapters]\nvocabularies = "chords"\n')

    with pytest.raises(ConfigError) as error:
        read_config(path)

    assert str(error.value) == (
        f"{path}: adapters.vocabularies: unknown preset 'chords'; presets are neutral, western; "
        'to select the chords vocabulary alone, write ["chords"]'
    )


@pytest.mark.parametrize(
    ("value", "where", "problem"),
    [
        (
            '"modern"',
            "adapters.vocabularies",
            "unknown preset 'modern'; presets are neutral, western",
        ),
        ("3", "adapters.vocabularies", "must be a preset name or a list"),
        ('["roman"]', "adapters.vocabularies[0]", "'roman' is not a built-in ('chords', 'key')"),
    ],
)
def test_an_unusable_vocabularies_value_is_refused(
    tmp_path: Path, value: str, where: str, problem: str
) -> None:
    path = write_config(tmp_path, SERUM + f"[adapters]\nvocabularies = {value}\n")

    with pytest.raises(ConfigError, match=re.escape(f"{path}: {where}: {problem}")):
        read_config(path)


def test_an_import_path_supplies_a_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    text = SERUM + '[adapters]\nvocabularies = [{{ use = "{module}:vocabulary" }}]\n'
    composition = compose(tmp_path, text, module.__name__)

    (vocabulary,) = composition.vocabularies()
    assert type(vocabulary) is module.Vocabulary
    assert vocabulary.name == "chords"
    assert "## chords labels\n" in composition.guide_parts()


@pytest.mark.parametrize(
    ("value", "where", "problem"),
    [
        (
            '["chords", {{ use = "{module}:vocabulary" }}]',
            "adapters.vocabularies",
            "two vocabularies are named 'chords'",
        ),
        (
            '[{{ use = "{module}:vocabulary", name = "Chords" }}]',
            "adapters.vocabularies[0]",
            "name 'Chords' is not a layer name",
        ),
        (
            '[{{ use = "chords", strict = true }}]',
            "adapters.vocabularies[0]",
            "unknown option 'strict'",
        ),
        (
            '[{{ use = "{module}:vocabulary", plain = true }}]',
            "adapters.vocabularies[0]",
            "name 'chords' is not an Identifier",
        ),
    ],
    ids=["duplicate-name", "uppercase-name", "unknown-option", "plain-str-name"],
)
def test_vocabularies_are_checked_when_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, where: str, problem: str
) -> None:
    module = adapters_module(tmp_path, monkeypatch)
    composition = compose(
        tmp_path, SERUM + f"[adapters]\nvocabularies = {value}\n", module.__name__
    )

    with pytest.raises(
        ConfigError, match=re.escape(f"{tmp_path / 'poser.toml'}: {where}: {problem}")
    ):
        composition.vocabularies()
