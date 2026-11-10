"""Tests for a Pedalboard instrument's sound names: walking folders and scanning sources."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import fxp_bytes

from poser.adapters.pedalboard.config import PedalboardConfig
from poser.adapters.pedalboard.formats import (
    PRESET_FORMATS,
    FxpFormat,
    PresetError,
    PresetFormat,
    SavedFormat,
)
from poser.adapters.pedalboard.sources import (
    SourceScan,
    combine,
    describe,
    saved_dir,
    scan,
    walk,
)
from poser.adapters.pedalboard.state import encode_state
from poser.core.names import Identifier, Suffix

FXP = Suffix(".fxp")
FORMATS: dict[Suffix, PresetFormat] = {fmt.suffix: fmt for fmt in PRESET_FORMATS.values()}
SAVED_STATE = encode_state(b"saved-state")


def write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def write_saved(path: Path, description: str = "a saved sound") -> Path:
    return write(path, json.dumps({"description": description, "state": SAVED_STATE}).encode())


def config(tmp_path: Path, **sources: Path) -> PedalboardConfig:
    return PedalboardConfig(
        name=Identifier("serum"),
        path=tmp_path / "Serum.vst3",
        plugin_name=None,
        sources={Identifier(name): directory for name, directory in sources.items()},
        formats=tuple(PRESET_FORMATS),
    )


def case_sensitive(directory: Path) -> bool:
    probe = write(directory / "case-probe")
    try:
        return not (directory / "CASE-PROBE").exists()
    finally:
        probe.unlink()


# walk


def test_walk_finds_nested_files_and_skips_others(tmp_path: Path) -> None:
    root = tmp_path / "presets"
    deep = write(root / "Bass" / "Deep.fxp")
    top = write(root / "Top.fxp")
    write(root / "Bass" / ".Hidden.fxp")
    write(root / ".cache" / "Inside.fxp")
    notes = write(root / "Bass" / "Notes.txt")
    upper = write(root / "Pads" / "Wide.FXP")
    inner = write(root / "Dir.fxp" / "inner.fxp")

    assert walk(root) == {
        "Bass/Deep.fxp": deep,
        "Bass/Notes.txt": notes,
        "Top.fxp": top,
        "Pads/Wide.FXP": upper,
        "Dir.fxp/inner.fxp": inner,
    }


def test_walk_follows_symlinked_folders_without_looping(tmp_path: Path) -> None:
    pack = write(tmp_path / "pack" / "Lead.fxp").parent
    root = tmp_path / "presets"
    root.mkdir()
    (root / "Pack").symlink_to(pack)
    (pack / "loop").symlink_to(root)

    assert list(walk(root)) == ["Pack/Lead.fxp"]


def test_walk_raises_for_an_unreadable_folder(tmp_path: Path) -> None:
    locked = tmp_path / "presets" / "Locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        with pytest.raises(PresetError, match="cannot read presets folder"):
            walk(tmp_path / "presets")
    finally:
        locked.chmod(0o755)


# scan


def test_scan_names_files_by_source_and_path_in_exact_case(tmp_path: Path) -> None:
    root = tmp_path / "presets"
    deep = write(root / "Bass" / "Deep.fxp", fxp_bytes(b"x"))
    upper = write(root / "Pads" / "Wide.FXP", fxp_bytes(b"x"))
    found = scan(config(tmp_path, presets=root), tmp_path / "sounds", FORMATS)

    assert found.files == {
        "presets/Bass/Deep": (deep, FORMATS[FXP]),
        "presets/Pads/Wide": (upper, FORMATS[FXP]),
    }
    assert "presets/bass/Deep" not in found.files
    assert "presets/Bass/Deep.fxp" not in found.files
    assert found.problems == ()
    assert found.unavailable == frozenset()
    assert found.ignored == {}


def test_scan_source_notes_unclaimed_files(tmp_path: Path) -> None:
    root = tmp_path / "presets"
    write(root / "Deep.fxp", fxp_bytes(b"x"))
    write(root / "Notes.txt")
    write(root / "Sound.vital")
    found = scan(config(tmp_path, presets=root), tmp_path / "sounds", FORMATS)

    assert list(found.files) == ["presets/Deep"]
    assert found.ignored == {
        Identifier("presets"): "source 'presets' holds 1 .txt, 1 .vital poser cannot read"
        " (it reads: .fxp, .json)"
    }
    assert found.problems == ()


def test_scan_source_without_strays_has_no_notes(tmp_path: Path) -> None:
    root = tmp_path / "presets"
    write(root / "Bass" / "Deep.fxp", fxp_bytes(b"x"))
    write_saved(saved_dir(config(tmp_path), tmp_path / "sounds") / "pad.json")
    found = scan(config(tmp_path, presets=root), tmp_path / "sounds", FORMATS)

    assert found.files
    assert found.ignored == {}


def test_combine_merges_ignored() -> None:
    first = SourceScan({}, (), frozenset(), {}, {}, {Identifier("presets"): "presets hold strays"})
    second = SourceScan({}, (), frozenset(), {}, {}, {Identifier("bank"): "bank holds strays"})

    merged = combine(first, second)

    assert merged.ignored == {
        Identifier("presets"): "presets hold strays",
        Identifier("bank"): "bank holds strays",
    }
    assert combine(second, first).ignored == {
        Identifier("presets"): "presets hold strays",
        Identifier("bank"): "bank holds strays",
    }


def test_scan_reports_a_name_two_formats_share(tmp_path: Path) -> None:
    root = tmp_path / "presets"
    fxp = write(root / "Bass" / "X.fxp")
    saved = write_saved(root / "Bass" / "X.json")
    write(root / "Bass" / "Y.fxp")
    found = scan(config(tmp_path, presets=root), tmp_path / "sounds", FORMATS)

    assert list(found.files) == ["presets/Bass/Y"]
    [problem] = found.problems
    assert "presets/Bass/X is ambiguous" in problem
    assert str(fxp) in problem
    assert str(saved) in problem


def test_scan_reports_a_name_two_suffix_cases_share(tmp_path: Path) -> None:
    root = tmp_path / "presets"
    root.mkdir()
    if not case_sensitive(root):
        pytest.skip("the volume ignores case, so X.fxp and X.FXP cannot both exist")
    lower = write(root / "Bass" / "X.fxp")
    upper = write(root / "Bass" / "X.FXP")
    found = scan(config(tmp_path, presets=root), tmp_path / "sounds", FORMATS)

    assert found.files == {}
    [problem] = found.problems
    assert str(lower) in problem
    assert str(upper) in problem


def test_scan_maps_the_saved_source_and_ignores_other_files_there(tmp_path: Path) -> None:
    sounds = tmp_path / "sounds"
    cfg = config(tmp_path)
    pad = write_saved(saved_dir(cfg, sounds) / "pad.json")
    write(saved_dir(cfg, sounds) / "lead.fxp", fxp_bytes(b"x"))
    found = scan(cfg, sounds, FORMATS)

    assert saved_dir(cfg, sounds) == sounds / "serum"
    assert list(found.files) == ["saved/pad"]
    assert found.files["saved/pad"][0] == pad
    assert isinstance(found.files["saved/pad"][1], SavedFormat)


def test_the_saved_scan_ignores_subfolders(tmp_path: Path) -> None:
    sounds = tmp_path / "sounds"
    cfg = config(tmp_path)
    directory = saved_dir(cfg, sounds)
    write_saved(directory / "pad.json")
    write_saved(directory / "Sub" / "lead.json")
    locked = directory / "Locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        found = scan(cfg, sounds, FORMATS)
    finally:
        locked.chmod(0o755)
    assert list(found.files) == ["saved/pad"]
    assert found.problems == ()
    assert found.unavailable == frozenset()


def test_an_unreadable_saved_folder_is_a_saved_sound_problem(tmp_path: Path) -> None:
    sounds = tmp_path / "sounds"
    cfg = config(tmp_path)
    directory = saved_dir(cfg, sounds)
    write_saved(directory / "pad.json")
    directory.chmod(0)
    try:
        found = scan(cfg, sounds, FORMATS)
    finally:
        directory.chmod(0o755)
    assert found.files == {}
    assert found.unavailable == {"saved"}
    problem = f"cannot read saved sounds folder {directory}: Permission denied"
    assert found.problems == (problem,)
    assert found.source_problems == {"saved": problem}


def test_scan_without_a_saved_directory_has_no_problem(tmp_path: Path) -> None:
    found = scan(config(tmp_path), tmp_path / "sounds", FORMATS)
    assert found.files == {}
    assert found.problems == ()
    assert found.unavailable == frozenset()


def test_scan_reports_a_missing_configured_source(tmp_path: Path) -> None:
    present = write(tmp_path / "present" / "A.fxp").parent
    cfg = config(tmp_path, gone=tmp_path / "gone", present=present)
    found = scan(cfg, tmp_path / "sounds", FORMATS)

    assert list(found.files) == ["present/A"]
    assert found.unavailable == {"gone"}
    [problem] = found.problems
    assert "'gone'" in problem
    assert "does not exist" in problem


def test_scan_reports_a_file_as_a_configured_source(tmp_path: Path) -> None:
    found = scan(config(tmp_path, presets=write(tmp_path / "file")), tmp_path / "sounds", FORMATS)
    assert found.unavailable == {"presets"}
    assert [problem for problem in found.problems if "is not a directory" in problem]


def test_scan_reports_an_unreadable_configured_source(tmp_path: Path) -> None:
    locked = tmp_path / "presets" / "Locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        found = scan(config(tmp_path, presets=tmp_path / "presets"), tmp_path / "sounds", FORMATS)
    finally:
        locked.chmod(0o755)
    assert found.unavailable == {"presets"}
    assert [problem for problem in found.problems if "cannot read" in problem]


def test_saved_sounds_list_whatever_the_configured_formats(tmp_path: Path) -> None:
    root = tmp_path / "presets"
    write(root / "A.fxp")
    write_saved(root / "B.json")
    sounds = tmp_path / "sounds"
    cfg = config(tmp_path, presets=root)
    write_saved(saved_dir(cfg, sounds) / "pad.json")
    found = scan(cfg, sounds, {FXP: FxpFormat()})

    assert sorted(found.files) == ["presets/A", "saved/pad"]


# describe


@pytest.mark.parametrize(
    ("name", "description"),
    [
        ("presets/Bass (Hard)/X", "Bass (Hard) preset"),
        ("presets/A/B/X", "A/B preset"),
        ("presets/X", "preset"),
    ],
)
def test_describe_names_an_fxp_preset_by_its_folder(
    tmp_path: Path, name: str, description: str
) -> None:
    path = write(tmp_path / "X.fxp", fxp_bytes(b"x"))
    assert describe(name, path, FxpFormat()) == description


def test_describe_gives_a_saved_sound_its_own_description(tmp_path: Path) -> None:
    path = write_saved(tmp_path / "pad.json", "a warm pad")
    assert describe("saved/pad", path, SavedFormat()) == "a warm pad"


def test_describe_raises_for_an_unreadable_saved_sound(tmp_path: Path) -> None:
    path = write(tmp_path / "pad.json", b"{}")
    with pytest.raises(PresetError, match="exactly the keys"):
        describe("saved/pad", path, SavedFormat())


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [
        ("poser.adapters.pedalboard.sources", {"pedalboard", "mido"}),
        ("poser.adapters.pedalboard.formats", {"pedalboard", "mido"}),
        ("poser.adapters.pedalboard.state", {"pedalboard", "mido"}),
        ("poser.adapters.pedalboard.config", {"pedalboard", "mido"}),
        # The instrument names the worker's functions as targets, which imports mido, but no
        # plugin code loads until a worker runs.
        ("poser.adapters.pedalboard.instrument", {"pedalboard"}),
    ],
)
def test_importing_preset_helpers_loads_no_plugin_code(module: str, forbidden: set[str]) -> None:
    check = (
        f"import sys, {module}; "
        f"print(sorted(set({sorted(forbidden)!r}) & {{m.split('.')[0] for m in sys.modules}}))"
    )
    output = subprocess.run(
        [sys.executable, "-c", check], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "[]", output.stderr
