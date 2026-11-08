"""Tests for installed presets: `.fxp` reading, `raw_state` framing, and listing."""

import ast
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest
from helpers import fxp_bytes

from poser import presets
from poser.plugin_check import ConfigError, PluginSpec, load_config
from poser.presets import (
    PresetError,
    build_state,
    component_state,
    juce_base64_decode,
    juce_base64_encode,
    preset_description,
    preset_files,
    read_chunk,
)

VECTORS = [(b"", "0."), (b"\x00", "1..."), (b"\xff", "1.+C"), (b"abc", "3.gI1X")]


@pytest.mark.parametrize(("data", "text"), VECTORS)
def test_juce_base64_matches_known_vectors(data: bytes, text: str) -> None:
    assert juce_base64_encode(data) == text
    assert juce_base64_decode(text) == data


def test_juce_base64_round_trips_random_bytes() -> None:
    rng = random.Random(0)
    for length in range(65):
        data = bytes(rng.randrange(256) for _ in range(length))
        assert juce_base64_decode(juce_base64_encode(data)) == data


def test_juce_base64_handles_a_two_megabyte_preset_quickly() -> None:
    data = random.Random(1).randbytes(2_000_000)
    start = time.perf_counter()
    assert juce_base64_decode(juce_base64_encode(data)) == data
    assert time.perf_counter() - start < 10


@pytest.mark.parametrize("text", ["abc", "x.AB", "2.A", "1.!!"])
def test_juce_base64_decode_rejects_bad_text(text: str) -> None:
    with pytest.raises(ValueError):
        juce_base64_decode(text)


def test_build_state_frames_the_component_as_juce_binary_xml() -> None:
    xml = (
        b'<?xml version="1.0" encoding="UTF-8"?> <VST3PluginState><IComponent>'
        b"3.gI1X</IComponent></VST3PluginState>"
    )
    state = build_state(b"abc")
    assert state == b"VC2!" + len(xml).to_bytes(4, "little") + xml + b"\x00"
    assert component_state(state) == b"abc"


@pytest.mark.parametrize(
    "broken",
    [
        b"VC3!" + build_state(b"abc")[4:],
        build_state(b"abc")[:-2] + b"\x00",
        build_state(b"abc")[:-1] + b"x",
    ],
    ids=["magic", "length", "zero byte"],
)
def test_component_state_rejects_broken_framing(broken: bytes) -> None:
    with pytest.raises(ValueError):
        component_state(broken)


def write_fxp(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_read_chunk_returns_the_chunk(tmp_path: Path) -> None:
    path = write_fxp(tmp_path / "a.fxp", fxp_bytes(b"chunk-data"))
    assert read_chunk(path) == b"chunk-data"


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (fxp_bytes(b"chunk")[:40], "too short"),
        (b"XXXX" + fxp_bytes(b"chunk")[4:], "not an .fxp"),
        (fxp_bytes(b"chunk", kind=b"FxCk"), "parameter"),
        (fxp_bytes(b"chunk")[:-1], "declares"),
        (fxp_bytes(b"chunk") + b"x", "declares"),
        (fxp_bytes(b""), "empty"),
    ],
    ids=["short", "magic", "params", "past end", "short of end", "empty"],
)
def test_read_chunk_rejects_unusable_files(tmp_path: Path, data: bytes, problem: str) -> None:
    path = write_fxp(tmp_path / "a.fxp", data)
    with pytest.raises(PresetError, match=problem):
        read_chunk(path)


def test_preset_files_names_nested_presets_and_skips_others(tmp_path: Path) -> None:
    root = tmp_path / "presets"
    deep = write_fxp(root / "Bass" / "Deep.fxp", fxp_bytes(b"x"))
    top = write_fxp(root / "Top.fxp", fxp_bytes(b"x"))
    write_fxp(root / "Bass" / ".Hidden.fxp", fxp_bytes(b"x"))
    write_fxp(root / ".cache" / "Inside.fxp", fxp_bytes(b"x"))
    write_fxp(root / "Bass" / "Notes.txt", b"x")
    upper = write_fxp(root / "Pads" / "Wide.FXP", fxp_bytes(b"x"))
    inner = write_fxp(root / "Dir.fxp" / "inner.fxp", fxp_bytes(b"x"))
    spec = PluginSpec("Serum", "/serum.vst3", None, str(root))

    assert preset_files(spec) == {
        "Serum:Bass/Deep": deep,
        "Serum:Top": top,
        "Serum:Pads/Wide": upper,
        "Serum:Dir.fxp/inner": inner,
    }


def test_preset_files_follows_symlinked_folders_without_looping(tmp_path: Path) -> None:
    pack = write_fxp(tmp_path / "pack" / "Lead.fxp", fxp_bytes(b"x")).parent
    root = tmp_path / "presets"
    root.mkdir()
    (root / "Pack").symlink_to(pack)
    (pack / "loop").symlink_to(root)
    spec = PluginSpec("Serum", "/serum.vst3", None, str(root))

    assert list(preset_files(spec)) == ["Serum:Pack/Lead"]


def test_preset_files_raises_for_an_unreadable_folder(tmp_path: Path) -> None:
    locked = tmp_path / "presets" / "Locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        spec = PluginSpec("Serum", "/serum.vst3", None, str(tmp_path / "presets"))
        with pytest.raises(PresetError, match="cannot read"):
            preset_files(spec)
    finally:
        locked.chmod(0o755)


def test_preset_files_raises_for_a_file_as_directory(tmp_path: Path) -> None:
    (tmp_path / "presets").write_text("")
    spec = PluginSpec("Serum", "/serum.vst3", None, str(tmp_path / "presets"))
    with pytest.raises(PresetError, match="not a directory"):
        preset_files(spec)


@pytest.mark.parametrize(
    ("name", "description"),
    [
        ("Serum:Bass (Hard)/X", "Bass (Hard) preset"),
        ("Serum:A/B/X", "A/B preset"),
        ("Serum:X", "preset"),
    ],
)
def test_preset_description_is_the_folder(name: str, description: str) -> None:
    assert preset_description(name) == description


def test_preset_files_is_empty_without_presets() -> None:
    assert preset_files(PluginSpec("Serum", "/serum.vst3")) == {}


def test_preset_files_raises_for_a_missing_directory(tmp_path: Path) -> None:
    spec = PluginSpec("Serum", "/serum.vst3", None, str(tmp_path / "missing"))
    with pytest.raises(PresetError, match="does not exist"):
        preset_files(spec)


def write_config(tmp_path: Path, entry: str) -> Path:
    path = tmp_path / "plugins.toml"
    path.write_text(f"[[plugin]]\n{entry}\n")
    return path


def test_load_config_reads_presets(tmp_path: Path) -> None:
    path = write_config(tmp_path, 'name = "Serum"\npath = "/s.vst3"\npresets = "/p"')
    assert load_config(path) == [PluginSpec("Serum", "/s.vst3", None, "/p")]


@pytest.mark.parametrize(
    "entry",
    [
        'name = "Serum"\npath = "/s.vst3"\nsamples = "/p"',
        'name = "Ser:um"\npath = "/s.vst3"',
        'name = "Serum"\npath = "/s.vst3"\npresets = ""',
    ],
    ids=["unknown key", "colon in name", "empty presets"],
)
def test_load_config_rejects_bad_entries(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, entry))


def test_presets_module_imports_no_plugin_code() -> None:
    tree = ast.parse(Path(presets.__file__).read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert not modules & {"pedalboard", "mido"}


def test_importing_presets_loads_no_plugin_code() -> None:
    check = (
        "import sys, poser.presets; "
        "print(sorted({'pedalboard', 'mido'} & {m.split('.')[0] for m in sys.modules}))"
    )
    output = subprocess.run(
        [sys.executable, "-c", check], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "[]", output.stderr
