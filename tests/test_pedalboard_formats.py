"""Tests for Pedalboard preset formats: `.fxp` reading, `raw_state` framing, and saved sounds."""

import json
import random
import time
from pathlib import Path

import pytest
from contracts import check_preset_format_contract
from helpers import fxp_bytes

from poser.adapters.pedalboard.formats import (
    FxpFormat,
    PresetError,
    SavedFormat,
    SavedSound,
    read_chunk,
)
from poser.adapters.pedalboard.state import (
    build_state,
    component_state,
    encode_state,
    juce_base64_decode,
    juce_base64_encode,
)
from poser.core.names import Suffix

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
    with pytest.raises(ValueError, match=r"JUCE base64|cannot hold"):
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
    with pytest.raises(ValueError, match=r"^raw_state "):
        component_state(broken)


def write_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_read_chunk_returns_the_chunk(tmp_path: Path) -> None:
    path = write_file(tmp_path / "a.fxp", fxp_bytes(b"chunk-data"))
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
    path = write_file(tmp_path / "a.fxp", data)
    with pytest.raises(PresetError, match=problem):
        read_chunk(path)


def test_fxp_format_frames_the_chunk_and_describes_by_folder(tmp_path: Path) -> None:
    path = write_file(tmp_path / "a.fxp", fxp_bytes(b"chunk-data"))
    assert FxpFormat().read_state(path) == build_state(b"chunk-data")
    assert FxpFormat().describe(path) is None


# Saved sounds


def write_saved(path: Path, fields: object) -> Path:
    return write_file(path, json.dumps(fields).encode())


def test_saved_format_reads_a_description_and_a_state(tmp_path: Path) -> None:
    path = write_saved(tmp_path / "pad.json", {"description": "warm", "state": encode_state(b"s")})
    assert SavedFormat().read_sound(path) == SavedSound("warm", b"s")
    assert SavedFormat().read_state(path) == b"s"
    assert SavedFormat().describe(path) == "warm"


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"description": "warm", "state": "cw==", "plugin": "Serum"}, "exactly the keys"),
        ({"description": "warm"}, "exactly the keys"),
        (["warm", "cw=="], "exactly the keys"),
        ({"description": " ", "state": "cw=="}, "description must be"),
        ({"description": 1, "state": "cw=="}, "description must be"),
        ({"description": "warm", "state": "not base64!"}, "not valid base64"),
        ({"description": "warm", "state": ""}, "state is empty"),
        ({"description": "warm", "state": 1}, "not a string"),
    ],
    ids=["plugin key", "no state", "list", "blank", "number", "bad base64", "empty", "not text"],
)
def test_saved_format_refuses_bad_files(tmp_path: Path, fields: object, problem: str) -> None:
    path = write_saved(tmp_path / "pad.json", fields)
    with pytest.raises(PresetError, match=problem):
        SavedFormat().read_sound(path)
    with pytest.raises(PresetError, match=problem):
        SavedFormat().read_state(path)


def test_saved_format_refuses_unreadable_json(tmp_path: Path) -> None:
    path = write_file(tmp_path / "pad.json", b"{not json")
    with pytest.raises(PresetError, match=r"^cannot read pad\.json"):
        SavedFormat().read_sound(path)


def test_saved_format_round_trips_write_sound(tmp_path: Path) -> None:
    path = tmp_path / "pad.json"
    sound = SavedSound("warm pad", bytes(range(256)))
    SavedFormat().write_sound(path, sound)
    assert SavedFormat().read_sound(path) == sound
    assert set(json.loads(path.read_text())) == {"description", "state"}
    assert [entry.name for entry in tmp_path.iterdir()] == ["pad.json"]
    replacement = SavedSound("cold pad", b"other")
    SavedFormat().write_sound(path, replacement)
    assert SavedFormat().read_sound(path) == replacement
    assert [entry.name for entry in tmp_path.iterdir()] == ["pad.json"]


def test_saved_format_failed_write_leaves_no_temporary_file(tmp_path: Path) -> None:
    target = tmp_path / "pad.json"
    target.mkdir()  # Replacing a directory with a file fails after the temporary file exists.
    (target / "keep").write_text("")
    with pytest.raises(OSError, match=r"pad\.json"):
        SavedFormat().write_sound(target, SavedSound("warm", b"s"))
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["pad.json"]


def test_saved_sound_refuses_a_blank_description_or_empty_state() -> None:
    with pytest.raises(ValueError, match="description"):
        SavedSound(" ", b"s")
    with pytest.raises(ValueError, match="non-empty state"):
        SavedSound("warm", b"")


# The preset format contract


def test_fxp_format_meets_the_preset_format_contract(tmp_path: Path) -> None:
    sample = write_file(tmp_path / "sample" / "a.fxp", fxp_bytes(b"chunk-data"))
    check_preset_format_contract(FxpFormat(), sample, tmp_path)


def test_saved_format_meets_the_preset_format_contract(tmp_path: Path) -> None:
    sample = tmp_path / "sample" / "pad.json"
    sample.parent.mkdir()
    SavedFormat().write_sound(sample, SavedSound("warm", b"state"))
    check_preset_format_contract(SavedFormat(), sample, tmp_path)


class LooseFormat:
    """A format whose read_state lets a garbage file's ValueError escape."""

    @property
    def suffix(self) -> Suffix:
        return Suffix(".loose")

    def read_state(self, path: Path) -> bytes:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise PresetError(str(error)) from error
        if not data:
            raise PresetError("empty")
        if not data.startswith(b"LOOSE"):
            raise ValueError("not a loose preset")
        return data

    def describe(self, path: Path) -> str | None:
        return None


def test_preset_format_contract_can_fail(tmp_path: Path) -> None:
    sample = write_file(tmp_path / "sample" / "a.loose", b"LOOSE state")
    with pytest.raises(AssertionError, match=r"garbage\.loose raised ValueError"):
        check_preset_format_contract(LooseFormat(), sample, tmp_path)
