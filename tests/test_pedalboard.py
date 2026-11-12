"""Tests for the Pedalboard instrument, with test workers in place of the plugin workers."""

import dataclasses
import gc
import importlib
import io
import json
import os
import plistlib
import re
import subprocess
import sys
import weakref
from collections.abc import Callable, Iterator
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import NoReturn, cast

import numpy as np
import pytest
from contracts import check_instrument_contract, check_sound_capture_contract
from helpers import fxp_bytes
from test_performance import cc_track, performed

from poser.adapters.isolation import WorkerCrashedError, WorkerTimedOutError
from poser.adapters.pedalboard import instrument as instrument_module
from poser.adapters.pedalboard.config import (
    INITIALIZATION_TIMEOUT_S,
    EffectConfig,
    PedalboardConfig,
    effect_from_options,
    from_options,
)
from poser.adapters.pedalboard.formats import PRESET_FORMATS, PresetFormat
from poser.adapters.pedalboard.instrument import (
    MALFORMED_RESULT,
    PedalboardEffect,
    PedalboardInstrument,
    payload_state,
    plugin_version,
)
from poser.adapters.pedalboard.state import build_state, encode_state
from poser.adapters.pedalboard.worker import (
    BUFFER_SIZE,
    MalformedJobError,
    NotLoaded,
    capture_job,
    load_external,
    performance_messages,
    render_duration_s,
    render_effect,
    render_fresh,
    render_group_job,
    render_job,
)
from poser.core.audio import SAMPLE_RATE, Audio, read_raw_audio, write_raw_audio
from poser.core.graph import Wire
from poser.core.jsonvalue import Json, JsonValue
from poser.core.names import Identifier, NodeName, Suffix
from poser.core.performance import Control, NoteOff, NoteOn, Performance, performance_to_data
from poser.core.ports import (
    AdapterContext,
    Captured,
    CaptureFailure,
    CaptureRequest,
    ConfigError,
    EffectRenderRequest,
    GroupMember,
    GroupRenderRequest,
    RenderFailure,
    RenderRequest,
    ResolvedEffect,
    ResolvedSound,
    SoundFailure,
)
from poser.core.song import ControlChange, Note
from poser.core.sounds import SoundRef
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus

SERUM = Identifier("serum")
FORMATS: dict[Suffix, PresetFormat] = {fmt.suffix: fmt for fmt in PRESET_FORMATS.values()}
PERFORMANCE = Performance(100, (Control(0, 74, 33), NoteOn(0, 60, 37), NoteOff(50, 60)))

WORKERS = """
import base64
import json
import time
from pathlib import Path

import numpy as np

from poser.core.audio import write_raw_audio


def _keep(job, path):
    Path(path + ".job.json").write_text(json.dumps(job))


def render_ok(job):
    _keep(job, job["audio_path"])
    write_raw_audio(Path(job["audio_path"]), np.zeros((2, job["performance"]["samples"]), "f4"))
    return {"status": "ok"}


def render_load_failed(job):
    return {"status": "load_failed", "detail": "cannot load it"}


def render_empty_detail(job):
    return {"status": "load_failed", "detail": ""}


def render_malformed(job):
    return {"status": "fine"}


def render_crash(job):
    raise RuntimeError("the plugin exploded")


def render_hang(job):
    time.sleep(30)


def capture_ok(job):
    _keep(job, job["preview_path"])
    write_raw_audio(Path(job["preview_path"]), np.zeros((2, job["performance"]["samples"]), "f4"))
    return {"state": base64.b64encode(b"captured").decode()}


def capture_error(job):
    return {"error": "cannot load the plugin: nope"}


def capture_malformed(job):
    return {"state": 5}


def capture_bad_state(job):
    return {"state": "not base64!"}


def capture_crash(job):
    raise RuntimeError("the window crashed")
"""


@pytest.fixture
def workers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the test workers from a folder on both the test's and the child's path."""
    folder = tmp_path / "workers"
    folder.mkdir()
    (folder / "pedalboard_workers.py").write_text(WORKERS)
    monkeypatch.syspath_prepend(str(folder))
    monkeypatch.setenv("PYTHONPATH", str(folder))
    monkeypatch.delitem(sys.modules, "pedalboard_workers", raising=False)
    return importlib.import_module("pedalboard_workers")


def use_workers(
    monkeypatch: pytest.MonkeyPatch,
    workers: ModuleType,
    render: str = "render_ok",
    capture: str = "capture_ok",
) -> None:
    monkeypatch.setattr(instrument_module, "render_job", getattr(workers, render))
    monkeypatch.setattr(instrument_module, "capture_job", getattr(workers, capture))


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


SAVED_STATE = encode_state(b"saved-state")


def write_saved(path: Path, state: str = SAVED_STATE, description: str = "warm") -> Path:
    return write(path, json.dumps({"description": description, "state": state}).encode())


def make_config(tmp_path: Path, path: Path | None = None, **sources: Path) -> PedalboardConfig:
    return PedalboardConfig(
        name=SERUM,
        path=path or tmp_path / "Serum.vst3",
        plugin_name=None,
        sources={Identifier(name): directory for name, directory in sources.items()},
        formats=tuple(PRESET_FORMATS),
    )


def make_instrument(
    tmp_path: Path, path: Path | None = None, **sources: Path
) -> PedalboardInstrument:
    if not sources:
        sources = {"presets": tmp_path / "presets"}
    config = make_config(tmp_path, path, **sources)
    return PedalboardInstrument(config, tmp_path / "sounds", FORMATS)


def saved_dir(tmp_path: Path) -> Path:
    return tmp_path / "sounds" / "serum"


def resolved(instrument: PedalboardInstrument, name: str | None) -> ResolvedSound:
    result = instrument.resolve(SoundRef(SERUM, name))
    assert isinstance(result, ResolvedSound), result
    return result


def failed(instrument: PedalboardInstrument, name: str) -> SoundFailure:
    result = instrument.resolve(SoundRef(SERUM, name))
    assert isinstance(result, SoundFailure), result
    return result


def captured(state: bytes = b"test-state") -> Captured:
    return Captured(Json.of({"state": encode_state(state)}))


# Config


def config_fields(tmp_path: Path) -> dict[str, object]:
    return {
        "name": SERUM,
        "path": tmp_path / "Serum.vst3",
        "plugin_name": None,
        "sources": {Identifier("presets"): tmp_path / "presets"},
        "formats": ("fxp", "saved"),
    }


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("name", "serum", "must be an Identifier"),
        ("path", Path("Serum.vst3"), "path must be an absolute path"),
        ("plugin_name", " ", "plugin_name must be non-empty text"),
        ("sources", [], "sources must be a mapping"),
        ("sources", {"presets": Path("/p")}, "is not an Identifier"),
        ("sources", {Identifier("saved"): Path("/p")}, "'saved' is reserved"),
        ("sources", {Identifier("presets"): Path("p")}, "must be an absolute path"),
        ("formats", ["fxp"], "formats must be a tuple"),
        ("formats", ("fxp", ""), "formats must be a tuple"),
        ("formats", (), "at least one preset format"),
        ("formats", ("fxp", "fxp"), "duplicate"),
    ],
    ids=[
        "name",
        "relative path",
        "blank plugin name",
        "sources list",
        "source name",
        "saved source",
        "relative source",
        "formats list",
        "blank format",
        "no formats",
        "duplicate format",
    ],
)
def test_pedalboard_config_refuses(tmp_path: Path, field: str, value: object, problem: str) -> None:
    fields = config_fields(tmp_path) | {field: value}
    with pytest.raises(ConfigError, match=problem):
        PedalboardConfig(**fields)  # ty: ignore[invalid-argument-type]


def test_pedalboard_config_accepts_its_fields(tmp_path: Path) -> None:
    config = PedalboardConfig(**config_fields(tmp_path))  # ty: ignore[invalid-argument-type]
    assert config.sources == {"presets": tmp_path / "presets"}


def test_pedalboard_config_sources_are_a_read_only_copy(tmp_path: Path) -> None:
    sources = {Identifier("presets"): tmp_path / "presets"}
    config = PedalboardConfig(**config_fields(tmp_path) | {"sources": sources})  # ty: ignore[invalid-argument-type]
    sources[Identifier("saved")] = tmp_path / "late"
    assert config.sources == {"presets": tmp_path / "presets"}
    with pytest.raises(TypeError):
        config.sources[Identifier("other")] = tmp_path  # ty: ignore[invalid-assignment]


@pytest.mark.parametrize(
    ("name", "options", "problem"),
    [
        ("Ser:um", {"path": "/s.vst3"}, r"^instrument 'Ser:um': "),
        ("serum", {"path": "/s.vst3", "samples": "/p"}, "unknown option 'samples'"),
        ("serum", {}, "path must be non-empty text"),
        ("serum", {"path": " "}, "path must be non-empty text"),
        ("serum", {"path": "/s.vst3", "plugin_name": ""}, "plugin_name must be non-empty"),
        ("serum", {"path": "/s.vst3", "plugin_name": 5}, "plugin_name must be non-empty"),
        ("serum", {"path": "/s.vst3", "plugin_name": None}, "plugin_name must be non-empty"),
        ("serum", {"path": "/s.vst3", "sources": ["/p"]}, "sources must be a table of paths"),
        ("serum", {"path": "/s.vst3", "sources": {"my presets": "/p"}}, "does not match"),
        ("serum", {"path": "/s.vst3", "sources": {"saved": "/p"}}, "'saved' is reserved"),
        ("serum", {"path": "/s.vst3", "sources": {"presets": ""}}, "must be a non-empty path"),
        ("serum", {"path": "/s.vst3", "sources": {"presets": 1}}, "must be a non-empty path"),
        ("serum", {"path": "/s.vst3", "formats": "fxp"}, "formats must be a list"),
        ("serum", {"path": "/s.vst3", "formats": [1]}, "formats must be a list"),
        ("serum", {"path": "/s.vst3", "formats": []}, "at least one preset format"),
        ("serum", {"path": "/s.vst3", "formats": ["fxp", "fxp"]}, "duplicate"),
    ],
    ids=[
        "name",
        "unknown option",
        "no path",
        "blank path",
        "blank plugin name",
        "number plugin name",
        "null plugin name",
        "sources list",
        "source name",
        "saved source",
        "blank source",
        "number source",
        "formats text",
        "formats numbers",
        "no formats",
        "duplicate format",
    ],
)
def test_from_options_refuses(
    tmp_path: Path, name: str, options: dict[str, JsonValue], problem: str
) -> None:
    with pytest.raises(ConfigError, match=problem):
        from_options(name, options, tmp_path)


def test_from_options_resolves_relative_paths_against_base(tmp_path: Path) -> None:
    options: dict[str, JsonValue] = {
        "path": "plugins/Serum.vst3",
        "plugin_name": "Serum",
        "sources": {"presets": "presets", "factory": "/Library/Serum"},
    }
    config = from_options("serum", options, tmp_path)
    assert config == PedalboardConfig(
        name=SERUM,
        path=tmp_path / "plugins" / "Serum.vst3",
        plugin_name="Serum",
        sources={
            Identifier("presets"): tmp_path / "presets",
            Identifier("factory"): Path("/Library/Serum"),
        },
        formats=("fxp", "saved"),
    )
    absolute = from_options("serum", {"path": "/s.vst3", "formats": ["saved"]}, tmp_path)
    assert (absolute.path, absolute.formats) == (Path("/s.vst3"), ("saved",))


def test_instrument_refuses_formats_that_do_not_match_their_suffixes(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    with pytest.raises(ValueError, match="at least one preset format"):
        PedalboardInstrument(config, tmp_path / "sounds", {})
    with pytest.raises(ValueError, match="has the suffix"):
        PedalboardInstrument(
            config, tmp_path / "sounds", {Suffix(".json"): FORMATS[Suffix(".fxp")]}
        )


# Resolving


def test_resolve_gives_the_default_sound_no_state(tmp_path: Path) -> None:
    sound = resolved(make_instrument(tmp_path), None)
    assert sound.ref == SoundRef(SERUM)
    assert payload_state(sound.payload) is None


def test_resolve_frames_an_fxp_preset(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Bass" / "Deep.fxp", fxp_bytes(b"chunk"))
    sound = resolved(make_instrument(tmp_path), "presets/Bass/Deep")
    assert sound.ref == SoundRef(SERUM, "presets/Bass/Deep")
    assert payload_state(sound.payload) == encode_state(build_state(b"chunk"))


def test_resolve_reads_a_saved_sound(tmp_path: Path) -> None:
    write_saved(saved_dir(tmp_path) / "pad.json")
    sound = resolved(make_instrument(tmp_path), "saved/pad")
    assert payload_state(sound.payload) == encode_state(b"saved-state")


def test_resolve_reports_an_unknown_name(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Bass" / "Deep.fxp", fxp_bytes(b"chunk"))
    failure = failed(make_instrument(tmp_path), "presets/bass/Deep")
    assert failure.status is SoundFailureStatus.UNKNOWN_SOUND
    assert failure.detail == "'serum' has no sound 'presets/bass/Deep'"


def test_resolve_appends_the_ignored_note(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Deep.fxp", fxp_bytes(b"chunk"))
    write(tmp_path / "presets" / "Other.vital", b"x")
    failure = failed(make_instrument(tmp_path), "presets/Missing")
    assert failure.status is SoundFailureStatus.UNKNOWN_SOUND
    assert failure.detail == (
        "'serum' has no sound 'presets/Missing'; "
        "source 'presets' holds 1 .vital poser cannot read (it reads: .fxp, .json)"
    )


def test_resolve_prefers_the_ambiguity_over_the_note(tmp_path: Path) -> None:
    fxp = write(tmp_path / "presets" / "X.fxp", fxp_bytes(b"chunk"))
    saved = write_saved(tmp_path / "presets" / "X.json")
    write(tmp_path / "presets" / "Other.vital", b"x")
    failure = failed(make_instrument(tmp_path), "presets/X")
    assert failure.status is SoundFailureStatus.UNKNOWN_SOUND
    assert failure.detail == (
        f"'serum' has no sound 'presets/X'; presets/X is ambiguous: {fxp} and {saved}"
    )


def test_resolve_unknown_with_odd_source_segment_stays_unknown(tmp_path: Path) -> None:
    (tmp_path / "presets").mkdir()
    failure = failed(make_instrument(tmp_path), "Unknown Bank/Patch")
    assert failure.status is SoundFailureStatus.UNKNOWN_SOUND
    assert failure.detail == "'serum' has no sound 'Unknown Bank/Patch'"
    write(tmp_path / "presets" / "Other.vital", b"x")
    failure = failed(make_instrument(tmp_path), "Unknown Bank/Patch")
    assert failure.status is SoundFailureStatus.UNKNOWN_SOUND
    assert failure.detail == "'serum' has no sound 'Unknown Bank/Patch'"


def test_resolve_reports_an_ambiguous_name(tmp_path: Path) -> None:
    fxp = write(tmp_path / "presets" / "X.fxp", fxp_bytes(b"chunk"))
    saved = write_saved(tmp_path / "presets" / "X.json")
    failure = failed(make_instrument(tmp_path), "presets/X")
    assert failure.status is SoundFailureStatus.UNKNOWN_SOUND
    assert failure.detail == (
        f"'serum' has no sound 'presets/X'; presets/X is ambiguous: {fxp} and {saved}"
    )


def test_resolve_reports_a_name_in_an_unavailable_source(tmp_path: Path) -> None:
    failure = failed(make_instrument(tmp_path, gone=tmp_path / "gone"), "gone/X")
    assert failure.status is SoundFailureStatus.LOAD_FAILED
    assert failure.detail == (
        f"preset serum:gone/X: source 'gone' directory {tmp_path / 'gone'} does not exist"
    )


def test_resolve_reports_a_broken_fxp(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Broken.fxp", fxp_bytes(b"chunk")[:40])
    failure = failed(make_instrument(tmp_path), "presets/Broken")
    assert failure.status is SoundFailureStatus.LOAD_FAILED
    assert failure.detail == (
        "preset serum:presets/Broken: Broken.fxp is too short for an .fxp header"
    )


def test_resolve_reports_a_broken_saved_sound(tmp_path: Path) -> None:
    write_saved(saved_dir(tmp_path) / "bad.json", state="not base64!")
    failure = failed(make_instrument(tmp_path), "saved/bad")
    assert failure.status is SoundFailureStatus.LOAD_FAILED
    assert failure.detail.startswith("saved sound serum:saved/bad: state is not valid base64")


def write_bundle(path: Path, **info: str) -> Path:
    (path / "Contents").mkdir(parents=True, exist_ok=True)
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return path


@pytest.mark.parametrize(
    ("info", "version"),
    [
        ({"CFBundleShortVersionString": "1.3", "CFBundleVersion": "1368"}, "1.3 (1368)"),
        ({"CFBundleShortVersionString": "1.3"}, "1.3"),
        ({"CFBundleVersion": "1368"}, "1368"),
        ({}, None),
    ],
)
def test_plugin_version_reads_the_bundle(
    tmp_path: Path, info: dict[str, str], version: str | None
) -> None:
    assert plugin_version(write_bundle(tmp_path / "Serum.vst3", **info)) == version


def test_plugin_version_is_none_without_a_readable_plist(tmp_path: Path) -> None:
    assert plugin_version(tmp_path / "Missing.vst3") is None
    write(tmp_path / "Broken.vst3" / "Contents" / "Info.plist", b"not a plist")
    assert plugin_version(tmp_path / "Broken.vst3") is None
    # A folder where the file should be cannot be read as one.
    (tmp_path / "Folder.vst3" / "Contents" / "Info.plist").mkdir(parents=True)
    assert plugin_version(tmp_path / "Folder.vst3") is None


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY], ids=["xml", "binary"])
def test_plugin_version_is_none_for_a_truncated_plist(
    tmp_path: Path, fmt: plistlib.PlistFormat
) -> None:
    info = {"CFBundleShortVersionString": "1.3", "CFBundleVersion": "1368"}
    data = plistlib.dumps(info, fmt=fmt)
    write(tmp_path / "Cut.vst3" / "Contents" / "Info.plist", data[: len(data) // 2])
    assert plugin_version(tmp_path / "Cut.vst3") is None
    assert resolved(make_instrument(tmp_path, tmp_path / "Cut.vst3"), None).identity


def identity(tmp_path: Path, plugin: Path, name: str | None = None) -> str:
    return resolved(make_instrument(tmp_path, plugin), name).identity


def test_identity_repeats_for_one_recipe(tmp_path: Path) -> None:
    plugin = write_bundle(tmp_path / "Serum.vst3", CFBundleShortVersionString="1.3")
    assert identity(tmp_path, plugin) == identity(tmp_path, plugin)


def test_identity_changes_with_the_state(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "A.fxp", fxp_bytes(b"a"))
    write(tmp_path / "presets" / "B.fxp", fxp_bytes(b"b"))
    plugin = tmp_path / "Serum.vst3"
    identities = {identity(tmp_path, plugin, name) for name in (None, "presets/A", "presets/B")}
    assert len(identities) == 3


def test_identity_changes_with_the_plugin_path(tmp_path: Path) -> None:
    assert identity(tmp_path, tmp_path / "A.vst3") != identity(tmp_path, tmp_path / "B.vst3")


def test_identity_changes_with_the_plugin_version(tmp_path: Path) -> None:
    plugin = tmp_path / "Serum.vst3"
    write_bundle(plugin, CFBundleShortVersionString="1.3", CFBundleVersion="1368")
    first = identity(tmp_path, plugin)
    write_bundle(plugin, CFBundleShortVersionString="1.4", CFBundleVersion="1368")
    second = identity(tmp_path, plugin)
    write_bundle(plugin, CFBundleShortVersionString="1.4", CFBundleVersion="1369")
    third = identity(tmp_path, plugin)
    assert len({first, second, third}) == 3


def test_identity_changes_with_the_pedalboard_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "Serum.vst3"
    first = identity(tmp_path, plugin)
    version = metadata.version
    monkeypatch.setattr(
        metadata, "version", lambda name: "0.0.1" if name == "pedalboard" else version(name)
    )
    assert identity(tmp_path, plugin) != first


# Listing and scanning


def test_sounds_lists_the_default_then_every_name_in_order(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Top.fxp", fxp_bytes(b"x"))
    write(tmp_path / "presets" / "Bass" / "Deep.fxp", fxp_bytes(b"x"))
    write_saved(saved_dir(tmp_path) / "pad.json", description="a warm pad")
    listing = make_instrument(tmp_path).sounds()
    assert [(str(entry.ref), entry.description) for entry in listing.entries] == [
        ("serum", "default patch"),
        ("serum:presets/Bass/Deep", "Bass preset"),
        ("serum:presets/Top", "preset"),
        ("serum:saved/pad", "a warm pad"),
    ]
    assert listing.problems == ()


def test_sounds_reports_an_unreadable_saved_sound_and_lists_the_rest(tmp_path: Path) -> None:
    write_saved(saved_dir(tmp_path) / "pad.json")
    write(saved_dir(tmp_path) / "bad.json", b'{"description": "x"}')
    (tmp_path / "presets").mkdir()
    listing = make_instrument(tmp_path).sounds()
    assert [str(entry.ref) for entry in listing.entries] == ["serum", "serum:saved/pad"]
    assert listing.problems == ("saved/bad: must hold exactly the keys ['description', 'state']",)


def test_sounds_reports_a_missing_source(tmp_path: Path) -> None:
    listing = make_instrument(tmp_path).sounds()
    assert [str(entry.ref) for entry in listing.entries] == ["serum"]
    assert listing.problems == (
        f"source 'presets' directory {tmp_path / 'presets'} does not exist",
    )


def test_sounds_notes_unclaimed_files_without_failing(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Top.fxp", fxp_bytes(b"x"))
    write(tmp_path / "presets" / "Notes.txt", b"notes")
    listing = make_instrument(tmp_path).sounds()
    assert [str(entry.ref) for entry in listing.entries] == ["serum", "serum:presets/Top"]
    assert listing.notes == (
        "source 'presets' holds 1 .txt poser cannot read (it reads: .fxp, .json)",
    )
    assert listing.problems == ()


def test_sounds_of_clean_sources_carries_no_notes(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Top.fxp", fxp_bytes(b"x"))
    write_saved(saved_dir(tmp_path) / "pad.json")
    listing = make_instrument(tmp_path).sounds()
    assert listing.notes == ()


def test_sounds_keeps_a_bad_saved_sound_in_problems_not_notes(tmp_path: Path) -> None:
    (tmp_path / "presets").mkdir()
    write(saved_dir(tmp_path) / "bad.json", b'{"description": "x"}')
    listing = make_instrument(tmp_path).sounds()
    assert listing.notes == ()
    assert listing.problems == ("saved/bad: must hold exactly the keys ['description', 'state']",)


def test_sounds_carries_a_note_and_a_problem_together(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Notes.txt", b"notes")
    write_saved(saved_dir(tmp_path) / "pad.json")
    write(saved_dir(tmp_path) / "bad.json", b'{"description": "x"}')
    listing = make_instrument(tmp_path).sounds()
    assert listing.notes == (
        "source 'presets' holds 1 .txt poser cannot read (it reads: .fxp, .json)",
    )
    assert listing.problems == ("saved/bad: must hold exactly the keys ['description', 'state']",)


def counting(monkeypatch: pytest.MonkeyPatch, name: str) -> list[int]:
    calls: list[int] = []
    original = getattr(instrument_module, name)

    def count(*args: object) -> object:
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(instrument_module, name, count)
    return calls


def test_one_instrument_scans_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(tmp_path / "presets" / "Top.fxp", fxp_bytes(b"x"))
    configured, saved = (
        counting(monkeypatch, "scan_configured"),
        counting(monkeypatch, "scan_saved"),
    )
    versions = counting(monkeypatch, "plugin_version")
    instrument = make_instrument(tmp_path)
    instrument.sounds()
    for name in ("presets/Top", "presets/Top", "presets/Nope", None):
        instrument.resolve(SoundRef(SERUM, name))
    instrument.sounds()
    assert (len(configured), len(saved), len(versions)) == (1, 1, 1)


def test_a_stored_sound_resolves_after_listing(tmp_path: Path) -> None:
    instrument = make_instrument(tmp_path)
    instrument.sounds()
    ref = instrument.store(Identifier("pad"), "a pad", captured(), replace=False)
    assert ref == SoundRef(SERUM, "saved/pad")
    sound = instrument.resolve(ref)
    assert isinstance(sound, ResolvedSound)
    assert payload_state(sound.payload) == encode_state(b"test-state")
    assert ref in [entry.ref for entry in instrument.sounds().entries]


def test_a_stored_sound_resolves_after_resolving_a_saved_one(tmp_path: Path) -> None:
    write_saved(saved_dir(tmp_path) / "pad.json")
    instrument = make_instrument(tmp_path)
    resolved(instrument, "saved/pad")  # As save-sound --from does.
    ref = instrument.store(Identifier("pad2"), "another pad", captured(), replace=False)
    assert isinstance(ref, SoundRef)
    assert isinstance(instrument.resolve(ref), ResolvedSound)


def test_a_replaced_sound_resolves_to_its_new_state(tmp_path: Path) -> None:
    write_saved(saved_dir(tmp_path) / "pad.json")
    instrument = make_instrument(tmp_path)
    before = resolved(instrument, "saved/pad")
    ref = instrument.store(Identifier("pad"), "a new pad", captured(b"new"), replace=True)
    assert ref == SoundRef(SERUM, "saved/pad")
    after = resolved(instrument, "saved/pad")
    assert payload_state(after.payload) == encode_state(b"new")
    assert after.identity != before.identity
    descriptions = {str(entry.ref): entry.description for entry in instrument.sounds().entries}
    assert descriptions["serum:saved/pad"] == "a new pad"


def test_a_configured_source_is_scanned_once(tmp_path: Path) -> None:
    write(tmp_path / "presets" / "Top.fxp", fxp_bytes(b"x"))
    instrument = make_instrument(tmp_path)
    instrument.sounds()
    write(tmp_path / "presets" / "New.fxp", fxp_bytes(b"x"))
    assert isinstance(instrument.store(Identifier("pad"), "p", captured(), replace=False), SoundRef)
    assert failed(instrument, "presets/New").status is SoundFailureStatus.UNKNOWN_SOUND
    assert isinstance(
        make_instrument(tmp_path).resolve(SoundRef(SERUM, "presets/New")), ResolvedSound
    )


def refuse(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError(f"constructing an instrument did I/O: {args} {kwargs}")


def test_constructing_an_instrument_reads_nothing_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, tmp_path / "Missing.vst3", presets=tmp_path / "presets")
    with monkeypatch.context() as patch:
        for module, name in [
            (io, "open"),
            (os, "open"),
            (os, "stat"),
            (os, "scandir"),
            (os, "listdir"),
            (os, "walk"),
            (subprocess, "Popen"),
            (instrument_module, "run_isolated"),
        ]:
            patch.setattr(module, name, refuse)
        PedalboardInstrument(config, tmp_path / "sounds", FORMATS)
    assert sorted(path.name for path in tmp_path.iterdir()) == []


def test_guide_is_a_section_naming_saved_sounds(tmp_path: Path) -> None:
    guide = make_instrument(tmp_path).guide
    assert guide.startswith("## ")
    assert "saved/" in guide


class EffectPlugin:
    """A loaded plugin that is an effect, not an instrument."""

    is_instrument = False

    def show_editor(self) -> None:
        raise AssertionError("an effect's window opened")


def patch_plugin_loading(monkeypatch: pytest.MonkeyPatch, fake: object) -> None:
    """Replace every way the worker loads a plugin, by format or by trying each, with `fake`."""
    for name in ("load_plugin", "VST3Plugin", "AudioUnitPlugin"):
        monkeypatch.setattr(f"pedalboard.{name}", fake)


@pytest.mark.parametrize(
    ("name", "loader"),
    [
        ("Synth.vst3", "VST3Plugin"),
        ("Synth.VST3", "VST3Plugin"),
        ("Synth.component", "AudioUnitPlugin"),
        ("Synth.dll", "load_plugin"),
    ],
)
def test_load_external_loads_with_the_class_the_suffix_names(
    monkeypatch: pytest.MonkeyPatch, name: str, loader: str
) -> None:
    calls: list[str] = []
    for each in ("load_plugin", "VST3Plugin", "AudioUnitPlugin"):
        monkeypatch.setattr(
            f"pedalboard.{each}",
            lambda *args, each=each, **kwargs: calls.append(each) or FakePlugin(),
        )

    load_external(f"/{name}", None)

    assert calls == [loader]


def test_a_vst3_that_fails_to_scan_reports_only_the_vst3_error(tmp_path: Path) -> None:
    bundle = tmp_path / "Broken.vst3"
    bundle.mkdir()

    with pytest.raises(ImportError) as raised:
        load_external(str(bundle), None)

    assert "AudioUnit" not in str(raised.value)
    assert "Components" not in str(raised.value)


def test_the_jobs_refuse_a_plugin_that_is_not_an_instrument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_plugin_loading(monkeypatch, lambda *args, **kwargs: EffectPlugin())
    job: dict[str, JsonValue] = {
        "path": "/Effect.vst3",
        "plugin_name": None,
        "state": None,
        "performance": performance_to_data(PERFORMANCE),
    }
    assert render_job(job | {"audio_path": str(tmp_path / "a.npy")}) == {
        "status": "load_failed",
        "detail": "/Effect.vst3 is not an instrument",
    }
    assert capture_job(job | {"preview_path": str(tmp_path / "p.npy")}) == {
        "error": "/Effect.vst3 is not an instrument"
    }
    assert list(tmp_path.iterdir()) == []


# Rendering and capturing through test workers


def render(instrument: PedalboardInstrument, path: Path, timeout_s: float = 30) -> object:
    sound = resolved(instrument, None)
    return instrument.render(RenderRequest(sound, PERFORMANCE, path), timeout_s)


def test_render_runs_the_worker_on_the_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: ModuleType
) -> None:
    use_workers(monkeypatch, workers)
    write(tmp_path / "presets" / "Top.fxp", fxp_bytes(b"chunk"))
    instrument = make_instrument(tmp_path)
    path = tmp_path / "out.npy"
    sound = resolved(instrument, "presets/Top")
    assert instrument.render(RenderRequest(sound, PERFORMANCE, path), 30) is None
    assert isinstance(read_raw_audio(path, PERFORMANCE.samples), Audio)
    job = json.loads((tmp_path / "out.npy.job.json").read_text())
    assert job["state"] == encode_state(build_state(b"chunk"))
    assert (job["path"], job["plugin_name"]) == (str(tmp_path / "Serum.vst3"), None)
    assert job["performance"]["samples"] == PERFORMANCE.samples


@pytest.mark.parametrize(
    ("worker", "status", "detail"),
    [
        ("render_load_failed", RenderFailureStatus.LOAD_FAILED, "^cannot load it$"),
        ("render_empty_detail", RenderFailureStatus.CRASHED, f"^{MALFORMED_RESULT}$"),
        ("render_malformed", RenderFailureStatus.CRASHED, f"^{MALFORMED_RESULT}$"),
        ("render_crash", RenderFailureStatus.CRASHED, "^exit code 1: .*the plugin exploded$"),
        ("render_hang", RenderFailureStatus.TIMED_OUT, r"^worker exceeded 0\.5 seconds$"),
    ],
)
def test_render_maps_each_worker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workers: ModuleType,
    worker: str,
    status: RenderFailureStatus,
    detail: str,
) -> None:
    use_workers(monkeypatch, workers, render=worker)
    failure = render(make_instrument(tmp_path), tmp_path / "out.npy", timeout_s=0.5)
    assert isinstance(failure, RenderFailure), failure
    assert failure.status is status
    assert re.search(detail, failure.detail), failure.detail


def capture(
    instrument: PedalboardInstrument, path: Path, start: ResolvedSound | None = None
) -> Captured | CaptureFailure:
    return instrument.capture(CaptureRequest(Identifier("pad"), start, PERFORMANCE, path))


def test_capture_runs_the_worker_from_the_start_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: ModuleType
) -> None:
    use_workers(monkeypatch, workers)
    write_saved(saved_dir(tmp_path) / "pad.json")
    instrument = make_instrument(tmp_path)
    path = tmp_path / "preview.npy"
    result = capture(instrument, path, resolved(instrument, "saved/pad"))
    assert result == Captured(Json.of({"state": encode_state(b"captured")}))
    assert isinstance(read_raw_audio(path, PERFORMANCE.samples), Audio)
    job = json.loads((tmp_path / "preview.npy.job.json").read_text())
    assert job["state"] == encode_state(b"saved-state")
    assert capture(instrument, tmp_path / "default.npy") == result
    assert json.loads((tmp_path / "default.npy.job.json").read_text())["state"] is None


@pytest.mark.parametrize(
    ("worker", "detail"),
    [
        ("capture_error", "^cannot load the plugin: nope$"),
        ("capture_malformed", f"^{MALFORMED_RESULT}$"),
        ("capture_bad_state", "^worker returned an unusable state: state is not valid base64"),
        ("capture_crash", "^exit code 1: .*the window crashed$"),
    ],
)
def test_capture_maps_each_worker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workers: ModuleType,
    worker: str,
    detail: str,
) -> None:
    use_workers(monkeypatch, workers, capture=worker)
    failure = capture(make_instrument(tmp_path), tmp_path / "preview.npy")
    assert isinstance(failure, CaptureFailure), failure
    assert re.search(detail, failure.detail), failure.detail


def test_capture_maps_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def time_out(target: object, job: object, timeout_s: float | None) -> NoReturn:
        assert timeout_s is None
        raise WorkerTimedOutError("worker exceeded 5 seconds")

    monkeypatch.setattr(instrument_module, "run_isolated", time_out)
    assert capture(make_instrument(tmp_path), tmp_path / "p.npy") == CaptureFailure(
        "worker exceeded 5 seconds"
    )


# Saving


def test_check_name_without_saved_sounds_accepts_any_name(tmp_path: Path) -> None:
    assert make_instrument(tmp_path).check_name(Identifier("pad"), replace=False) is None


def test_check_name_refuses_an_existing_name_without_replace(tmp_path: Path) -> None:
    write_saved(saved_dir(tmp_path) / "pad.json")
    instrument = make_instrument(tmp_path)
    assert instrument.check_name(Identifier("pad"), replace=False) == (
        "saved sound 'pad' exists; pass --replace"
    )
    assert instrument.check_name(Identifier("pad"), replace=True) is None
    assert instrument.check_name(Identifier("lead"), replace=False) is None


def test_check_name_refuses_a_name_differing_only_in_case(tmp_path: Path) -> None:
    write_saved(saved_dir(tmp_path) / "Pad.json")
    instrument = make_instrument(tmp_path)
    for replace in (False, True):
        assert instrument.check_name(Identifier("pad"), replace=replace) == (
            "saved sound 'Pad' differs only in case"
        )


def test_store_writes_the_saved_format(tmp_path: Path) -> None:
    instrument = make_instrument(tmp_path)
    instrument.store(Identifier("pad"), "a warm pad", captured(), replace=False)
    data = json.loads((saved_dir(tmp_path) / "pad.json").read_text())
    assert data == {"description": "a warm pad", "state": encode_state(b"test-state")}


def test_resolve_reports_an_unreadable_saved_folder_as_a_saved_sound(tmp_path: Path) -> None:
    directory = saved_dir(tmp_path)
    write_saved(directory / "pad.json")
    directory.chmod(0)
    try:
        failure = failed(make_instrument(tmp_path), "saved/pad")
    finally:
        directory.chmod(0o755)
    assert failure.status is SoundFailureStatus.LOAD_FAILED
    assert failure.detail == (
        f"saved sound serum:saved/pad: cannot read saved sounds folder {directory}: "
        "Permission denied"
    )


def test_an_unreadable_subfolder_blocks_no_store(tmp_path: Path) -> None:
    locked = saved_dir(tmp_path) / "Locked"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        instrument = make_instrument(tmp_path)
        ref = instrument.store(Identifier("pad"), "a pad", captured(), replace=False)
        assert ref == SoundRef(SERUM, "saved/pad")
        assert payload_state(resolved(instrument, "saved/pad").payload) == encode_state(
            b"test-state"
        )
    finally:
        locked.chmod(0o755)


def test_replacing_writes_over_a_file_whose_suffix_differs_in_case(tmp_path: Path) -> None:
    write_saved(saved_dir(tmp_path) / "Pad.JSON")
    instrument = make_instrument(tmp_path)
    ref = instrument.store(Identifier("Pad"), "a new pad", captured(b"new"), replace=True)
    assert ref == SoundRef(SERUM, "saved/Pad")
    names = sorted(path.name for path in saved_dir(tmp_path).iterdir() if path.name != ".lock")
    assert names == ["Pad.JSON"]
    assert payload_state(resolved(instrument, "saved/Pad").payload) == encode_state(b"new")


def test_store_refuses_a_name_two_files_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A case-insensitive volume cannot hold both files, so the listing is faked.
    directory = saved_dir(tmp_path)
    write_saved(directory / "Pad.json")
    both = {"Pad.json": directory / "Pad.json", "Pad.JSON": directory / "Pad.JSON"}
    monkeypatch.setattr(instrument_module, "list_saved", lambda folder: both)
    instrument = make_instrument(tmp_path)
    problem = (
        f"saved sound 'Pad' is ambiguous: {directory / 'Pad.json'} and {directory / 'Pad.JSON'}"
    )
    assert instrument.check_name(Identifier("Pad"), replace=True) == problem
    assert instrument.store(Identifier("Pad"), "a pad", captured(), replace=True) == problem


STORER = """
import sys, time
from pathlib import Path
from poser.adapters.pedalboard.config import PedalboardConfig
from poser.adapters.pedalboard.formats import PRESET_FORMATS
from poser.adapters.pedalboard.instrument import PedalboardInstrument
from poser.adapters.pedalboard.state import encode_state
from poser.core.jsonvalue import Json
from poser.core.names import Identifier
from poser.core.ports import Captured
from poser.core.sounds import SoundRef

root, name, go = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
config = PedalboardConfig(Identifier("serum"), root / "Serum.vst3", None, {}, tuple(PRESET_FORMATS))
formats = {fmt.suffix: fmt for fmt in PRESET_FORMATS.values()}
instrument = PedalboardInstrument(config, root / "sounds", formats)
captured = Captured(Json.of({"state": encode_state(name.encode())}))
print("ready", flush=True)
while not go.exists():
    time.sleep(0.001)
result = instrument.store(Identifier(name), "racing", captured, replace=False)
print("stored" if isinstance(result, SoundRef) else "refused", flush=True)
"""


@pytest.mark.parametrize("names", [("pad", "pad"), ("pad", "Pad")], ids=["same", "case"])
def test_two_processes_storing_one_name_save_one_sound(
    tmp_path: Path, names: tuple[str, str]
) -> None:
    go = tmp_path / "go"
    storers = [
        subprocess.Popen(
            [sys.executable, "-c", STORER, str(tmp_path), name, str(go)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for name in names
    ]
    try:
        for storer in storers:
            assert storer.stdout is not None
            assert storer.stdout.readline() == "ready\n"
        go.touch()
        outputs = sorted(storer.communicate(timeout=60)[0].strip() for storer in storers)
    finally:
        for storer in storers:
            storer.kill()
            storer.wait()
    assert outputs == ["refused", "stored"]
    left = sorted(path.name for path in saved_dir(tmp_path).iterdir() if path.name != ".lock")
    assert len(left) == 1, left
    assert left[0] in {f"{name}.json" for name in names}


# Contract suites


def test_pedalboard_instrument_meets_the_instrument_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: ModuleType
) -> None:
    use_workers(monkeypatch, workers)
    write(tmp_path / "presets" / "Bass" / "Deep.fxp", fxp_bytes(b"chunk"))
    write_saved(saved_dir(tmp_path) / "pad.json")
    instrument = make_instrument(tmp_path)
    check_instrument_contract(instrument, SoundRef(SERUM, "presets/Bass/Deep"), tmp_path)
    check_instrument_contract(instrument, SoundRef(SERUM, "saved/pad"), tmp_path)
    check_instrument_contract(instrument, SoundRef(SERUM), tmp_path)


def test_pedalboard_instrument_meets_the_sound_capture_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: ModuleType
) -> None:
    use_workers(monkeypatch, workers)

    def make(directory: Path) -> PedalboardInstrument:
        config = make_config(tmp_path, presets=tmp_path / "presets")
        return PedalboardInstrument(config, directory, FORMATS)

    def make_captured(instrument: object) -> Captured:
        assert isinstance(instrument, PedalboardInstrument)
        return Captured(Json.of({"state": encode_state(b"test-state")}))

    check_sound_capture_contract(make, make_captured, tmp_path / "capture")


# Messages for Pedalboard


def pedalboard_sample(time: float) -> int:
    """Return the sample Pedalboard plays a message at: float32 time times the rate, truncated."""
    return int(np.float32(time) * np.float32(SAMPLE_RATE))


def test_every_message_time_truncates_to_its_sample() -> None:
    # One beat at 120 bpm is 24000 samples, so beat k / 24000 lands on sample k.
    beats = [k / 24000 for k in (27, 49, 54, 61, 98, 12345)]
    notes = tuple(Note(beat, 60, 1e-9, 100) for beat in beats)
    changes = (ControlChange(beat, 1, i + 1) for i, beat in enumerate(beats))
    track = cc_track(ControlChange(0, 1, 0), *changes, notes=notes)

    messages = performance_messages(performed(track))
    samples = {(m.dict()["type"], pedalboard_sample(m.dict()["time"])) for m in messages}

    assert {("note_on", k) for k in (27, 49, 54, 61, 98, 12345)} <= samples
    assert {("control_change", k) for k in (27, 49, 54, 61, 98, 12345)} <= samples


def test_messages_carry_every_event_field_in_order() -> None:
    # mido sets message fields dynamically, so they are read through `dict()`.
    fields = [m.dict() for m in performance_messages(PERFORMANCE)]

    assert [(f["type"], f["channel"]) for f in fields] == [
        ("control_change", 0),
        ("note_on", 0),
        ("note_off", 0),
    ]
    assert (fields[0]["control"], fields[0]["value"]) == (74, 33)
    assert (fields[1]["note"], fields[1]["velocity"]) == (60, 37)
    assert fields[2]["note"] == 60
    assert [pedalboard_sample(f["time"]) for f in fields] == [0, 0, 50]


@pytest.mark.parametrize("samples", [1, 240000, 485925, 3600 * SAMPLE_RATE])
def test_render_duration_gives_exactly_the_samples_after_truncation(samples: int) -> None:
    assert int(render_duration_s(samples) * SAMPLE_RATE) == samples


# The factory


TEXT_FORMATS = """
from pathlib import Path

from poser.core.names import Suffix


class TextFormat:
    suffix = Suffix(".txt")

    def read_state(self, path):
        return path.read_bytes()

    def describe(self, path):
        return path.read_text().strip()


def text_format():
    return TextFormat()


def fxp_too():
    fmt = TextFormat()
    fmt.suffix = Suffix(".fxp")
    return fmt


def not_a_format():
    return object()


def plain_suffix():
    fmt = TextFormat()
    fmt.suffix = ".txt"
    return fmt
"""


def format_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    name = f"preset_formats_{tmp_path.name.replace('-', '_')}"
    (tmp_path / f"{name}.py").write_text(TEXT_FORMATS)
    monkeypatch.syspath_prepend(str(tmp_path))
    return name


def create_serum(tmp_path: Path, formats: list[JsonValue]) -> PedalboardInstrument:
    options: dict[str, JsonValue] = {
        "path": "Serum.vst3",
        "sources": {"presets": "presets"},
        "formats": formats,
    }
    context = AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
    return instrument_module.create(SERUM, options, context)


def test_create_scans_the_suffix_of_a_format_named_by_import_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = format_module(tmp_path, monkeypatch)
    (tmp_path / "presets").mkdir()
    (tmp_path / "presets" / "Warm.txt").write_text("a warm pad\n")
    (tmp_path / "presets" / "Bright.fxp").write_bytes(fxp_bytes(b"state"))

    made = create_serum(tmp_path, [f"{module}:text_format"])

    assert isinstance(made, PedalboardInstrument)
    entries = made.sounds().entries
    assert [(str(entry.ref), entry.description) for entry in entries[1:]] == [
        ("serum:presets/Warm", "a warm pad")
    ]
    assert not (tmp_path / "sounds").exists()
    assert not (tmp_path / "renders").exists()


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("vital", "unknown preset format 'vital'; use one of "),
        ("{module}:not_a_format", "made a object, which is not a PresetFormat"),
        ("{module}:plain_suffix", "has suffix '.txt', not a Suffix"),
    ],
)
def test_create_refuses_a_format_it_cannot_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, message: str
) -> None:
    module = format_module(tmp_path, monkeypatch)

    with pytest.raises(ConfigError, match=re.escape(message)) as error:
        create_serum(tmp_path, [entry.format(module=module)])

    assert str(error.value).startswith("instrument 'serum': ")


def test_create_refuses_two_formats_with_one_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = format_module(tmp_path, monkeypatch)

    with pytest.raises(ConfigError) as error:
        create_serum(tmp_path, ["fxp", f"{module}:fxp_too"])

    assert str(error.value) == "instrument 'serum': two preset formats read '.fxp' files"


# The worker's jobs on fake plugins

EDITED = b"edited"
type Call = tuple[list[object], dict[str, object], bytes | None]


class FakePlugin:
    """A loaded instrument that records each call and renders an array of its own."""

    is_instrument = True

    def __init__(self, fill: float = 1.0, calls: list[Call] | None = None) -> None:
        self.fill = fill
        self.calls: list[Call] = [] if calls is None else calls
        self.rendered: list[np.ndarray] = []
        self.state: bytes | None = None
        self.state_error: Exception | None = None
        self.call_error: Exception | None = None
        self.on_call: Callable[[], None] | None = None

    @property
    def raw_state(self) -> bytes | None:
        return self.state

    @raw_state.setter
    def raw_state(self, value: bytes) -> None:
        if self.state_error is not None:
            raise self.state_error
        self.state = value

    def show_editor(self) -> None:
        self.raw_state = EDITED

    def __call__(self, messages: Iterator[object], **kwargs: object) -> np.ndarray:
        self.calls.append((list(messages), kwargs, self.state))
        if self.on_call is not None:
            self.on_call()
        if self.call_error is not None:
            raise self.call_error
        duration = kwargs["duration"]
        assert isinstance(duration, float)
        self.rendered.append(np.full((2, int(duration * SAMPLE_RATE)), self.fill, np.float32))
        return self.rendered[-1]


class FakeLoader:
    """Stands in for pedalboard's plugin loading, handing out preset or new plugins."""

    def __init__(self) -> None:
        self.loads: list[tuple[str, str | None, float]] = []
        self.queue: list[FakePlugin | FakeEffectPlugin] = []
        self.made: list[weakref.ref[FakePlugin | FakeEffectPlugin]] = []
        self.error: Exception | None = None

    def __call__(
        self, path: str, plugin_name: str | None = None, initialization_timeout: float = 0
    ) -> "FakePlugin | FakeEffectPlugin":
        self.loads.append((path, plugin_name, initialization_timeout))
        if self.error is not None:
            raise self.error
        plugin: FakePlugin | FakeEffectPlugin = (
            self.queue.pop(0) if self.queue else FakePlugin(len(self.made) + 1)
        )
        self.made.append(weakref.ref(plugin))
        return plugin


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch) -> FakeLoader:
    fake = FakeLoader()
    patch_plugin_loading(monkeypatch, fake)
    return fake


def message_values(messages: list[object]) -> list[tuple[object, ...]]:
    return [(m.type, m.dict()) for m in messages]  # ty: ignore[unresolved-attribute]


def test_render_fresh_renders_the_new_instance_without_a_reset(
    loader: FakeLoader,
) -> None:
    plugin = FakePlugin()
    loader.queue.append(plugin)

    audio = render_fresh("/S.vst3", "Synth", b"state", PERFORMANCE)

    assert loader.loads == [("/S.vst3", "Synth", INITIALIZATION_TIMEOUT_S)]
    [(messages, kwargs, state)] = plugin.calls
    assert state == b"state"
    assert message_values(messages) == message_values(list(performance_messages(PERFORMANCE)))
    assert kwargs == {
        "duration": render_duration_s(PERFORMANCE.samples),
        "sample_rate": SAMPLE_RATE,
        "num_channels": 2,
        "buffer_size": 512,
        "reset": False,
    }
    assert audio is plugin.rendered[0]


def test_render_fresh_with_no_state_leaves_raw_state_unset(loader: FakeLoader) -> None:
    plugin = FakePlugin()
    plugin.state_error = AssertionError("raw_state was set")
    loader.queue.append(plugin)

    render_fresh("/S.vst3", None, None, PERFORMANCE)

    [(_, _, state)] = plugin.calls
    assert state is None


def test_render_fresh_loads_a_new_instance_per_call(loader: FakeLoader) -> None:
    first, second = FakePlugin(), FakePlugin()
    loader.queue.extend([first, second])

    render_fresh("/S.vst3", None, None, PERFORMANCE)
    render_fresh("/S.vst3", None, None, PERFORMANCE)

    assert len(loader.loads) == 2
    assert (len(first.calls), len(second.calls)) == (1, 1)


@pytest.mark.parametrize(
    ("error", "detail"), [(RuntimeError("bad bundle"), "bad bundle"), (RuntimeError(), "")]
)
def test_render_fresh_reports_a_load_exception(
    loader: FakeLoader, error: Exception, detail: str
) -> None:
    loader.error = error

    assert render_fresh("/S.vst3", None, None, PERFORMANCE) == NotLoaded(detail)


def test_render_fresh_reports_a_rejected_state(loader: FakeLoader) -> None:
    plugin = FakePlugin()
    plugin.state_error = ValueError("bad state")
    loader.queue.append(plugin)

    assert render_fresh("/S.vst3", None, b"state", PERFORMANCE) == NotLoaded("bad state")
    assert plugin.calls == []


def test_render_fresh_reports_a_non_instrument(loader: FakeLoader) -> None:
    plugin = FakePlugin()
    plugin.is_instrument = False
    loader.queue.append(plugin)

    result = render_fresh("/S.vst3", None, None, PERFORMANCE)

    assert result == NotLoaded("/S.vst3 is not an instrument")
    assert plugin.calls == []


def test_render_fresh_lets_a_processing_exception_escape(loader: FakeLoader) -> None:
    plugin = FakePlugin()
    plugin.call_error = RuntimeError("boom")
    loader.queue.append(plugin)

    with pytest.raises(RuntimeError, match="boom"):
        render_fresh("/S.vst3", None, None, PERFORMANCE)


def fake_job(output: str, path: Path, state: bytes | None = None) -> dict[str, JsonValue]:
    return {
        "path": "/S.vst3",
        "plugin_name": None,
        "state": None if state is None else encode_state(state),
        "performance": performance_to_data(PERFORMANCE),
        output: str(path),
    }


def test_render_job_writes_what_the_plugin_rendered(loader: FakeLoader, tmp_path: Path) -> None:
    loader.queue.append(FakePlugin(0.25))
    audio_path = tmp_path / "a.npy"

    assert render_job(fake_job("audio_path", audio_path)) == {"status": "ok"}

    audio = read_raw_audio(audio_path, PERFORMANCE.samples)
    assert isinstance(audio, Audio)
    assert np.array_equal(audio.samples, np.full((2, PERFORMANCE.samples), 0.25, np.float32))


def test_render_job_reports_a_load_exception(loader: FakeLoader, tmp_path: Path) -> None:
    loader.error = RuntimeError("bad bundle")
    audio_path = tmp_path / "a.npy"

    assert render_job(fake_job("audio_path", audio_path)) == {
        "status": "load_failed",
        "detail": "bad bundle",
    }
    assert not audio_path.exists()


def test_capture_job_previews_the_captured_state_on_a_new_instance(
    loader: FakeLoader, tmp_path: Path
) -> None:
    editor_calls: list[Call] = []
    preview = FakePlugin(0.5)
    loader.queue.extend([FakePlugin(0.25, editor_calls), preview])
    editor_alive: list[bool] = []

    def check_editor() -> None:
        gc.collect()
        editor_alive.append(loader.made[0]() is not None)

    preview.on_call = check_editor
    preview_path = tmp_path / "p.npy"

    result = capture_job(fake_job("preview_path", preview_path, b"start"))

    assert result == {"state": encode_state(EDITED)}
    [(messages, kwargs, _)] = editor_calls
    assert messages == []
    assert kwargs["reset"] is False
    [(messages, _, state)] = preview.calls
    assert message_values(messages) == message_values(list(performance_messages(PERFORMANCE)))
    assert state == EDITED
    assert editor_alive == [False]
    audio = read_raw_audio(preview_path, PERFORMANCE.samples)
    assert isinstance(audio, Audio)
    assert np.array_equal(audio.samples, np.full((2, PERFORMANCE.samples), 0.5, np.float32))


def test_capture_job_reports_a_failed_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def load_once(*args: object, **kwargs: object) -> FakePlugin:
        patch_plugin_loading(monkeypatch, failing)
        return FakePlugin()

    def failing(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("gone")

    patch_plugin_loading(monkeypatch, load_once)
    preview_path = tmp_path / "p.npy"

    assert capture_job(fake_job("preview_path", preview_path)) == {
        "error": "cannot restore the captured state: gone"
    }
    assert not preview_path.exists()


def test_capture_job_refuses_a_preview_plugin_that_is_not_an_instrument(
    loader: FakeLoader, tmp_path: Path
) -> None:
    preview = FakePlugin()
    preview.is_instrument = False
    loader.queue.extend([FakePlugin(), preview])
    preview_path = tmp_path / "p.npy"

    assert capture_job(fake_job("preview_path", preview_path)) == {
        "error": "cannot restore the captured state: /S.vst3 is not an instrument"
    }
    assert preview.calls == []
    assert not preview_path.exists()


# Effect recipes: config, worker verb, and the adapter, all headless.


def effect_config_fields(tmp_path: Path) -> dict[str, object]:
    return {
        "name": Identifier("drive"),
        "path": None,
        "builtin": "Gain",
        "parameters": {"gain_db": -6},
    }


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("name", "drive", "must be an Identifier"),
        ("path", Path("Drive.vst3"), "path must be an absolute path"),
        ("builtin", " ", "builtin must be non-empty text"),
        ("parameters", [], "parameters must be a table"),
    ],
    ids=["name", "relative path", "blank builtin", "parameters list"],
)
def test_effect_config_refuses(tmp_path: Path, field: str, value: object, problem: str) -> None:
    fields = dict(effect_config_fields(tmp_path))
    if field == "path" and value is not None:
        fields = fields | {"path": value, "builtin": None}
    else:
        fields = fields | {field: value}
    with pytest.raises(ConfigError, match=problem):
        EffectConfig(**fields)  # ty: ignore[invalid-argument-type]


def test_effect_config_accepts_a_path_recipe(tmp_path: Path) -> None:
    config = EffectConfig(Identifier("crush"), tmp_path / "Crush.vst3", None, {})
    assert config.path == tmp_path / "Crush.vst3"
    assert config.parameters == {}


@pytest.mark.parametrize(
    ("name", "options", "problem"),
    [
        ("dr:ive", {"builtin": "Gain"}, r"^effect 'dr:ive': "),
        ("drive", {"builtin": "Gain", "loud": True}, "unknown option 'loud'"),
        ("drive", {}, "path must be non-empty text"),
        ("drive", {"path": "/a.vst3", "builtin": "Gain"}, "name exactly one of"),
        ("drive", {"path": " "}, "path must be non-empty text"),
        ("drive", {"builtin": 3}, "builtin must be non-empty text"),
        ("drive", {"builtin": "pedalboard_native.Gain"}, "must name a pedalboard-native class"),
        ("drive", {"path": "/a.vst3", "parameters": {"x": 1}}, "parameters belong to a builtin"),
        ("drive", {"builtin": "Gain", "parameters": [1]}, "parameters must be a table"),
    ],
    ids=[
        "name",
        "unknown option",
        "neither",
        "both",
        "blank path",
        "number builtin",
        "dotted builtin",
        "parameters with path",
        "parameters list",
    ],
)
def test_effect_from_options_refuses(
    tmp_path: Path, name: str, options: dict[str, JsonValue], problem: str
) -> None:
    with pytest.raises(ConfigError, match=problem):
        effect_from_options(name, options, tmp_path)


def test_effect_from_options_resolves_the_path_against_base(tmp_path: Path) -> None:
    config = effect_from_options("drive", {"path": "plugins/Drive.vst3"}, tmp_path)
    assert config.path == tmp_path / "plugins" / "Drive.vst3"
    builtin = effect_from_options(
        "drive", {"builtin": "Gain", "parameters": {"gain_db": 6}}, tmp_path
    )
    assert (builtin.builtin, builtin.parameters) == ("Gain", {"gain_db": 6})


def write_effect_input(tmp_path: Path, frames: int = 480, fill: float = 0.5) -> Path:
    path = tmp_path / "input.npy"
    write_raw_audio(path, np.full((2, frames), fill, np.float32))
    return path


def effect_job(tmp_path: Path, **fields: JsonValue) -> dict[str, JsonValue]:
    job: dict[str, JsonValue] = {
        "path": None,
        "builtin": "Gain",
        "parameters": {"gain_db": -6},
        "frames": 480,
        "audio_path": str(write_effect_input(tmp_path)),
        "control_path": None,
        "output_path": str(tmp_path / "out.npy"),
    }
    job.update(fields)
    return job


def test_render_effect_processes_audio_through_a_native_builtin(tmp_path: Path) -> None:
    output = tmp_path / "out.npy"

    assert render_effect(effect_job(tmp_path)) == {"status": "ok"}

    audio = read_raw_audio(output, 480)
    assert isinstance(audio, Audio)
    assert np.allclose(audio.samples, 0.5 * 10 ** (-6 / 20))


def write_effect_control(tmp_path: Path, frames: int = 480, fill: float = 0.5) -> Path:
    path = tmp_path / "control.npy"
    write_raw_audio(path, np.full((2, frames), fill, np.float32))
    return path


def test_render_effect_sums_as_a_passthrough(tmp_path: Path) -> None:
    output = tmp_path / "out.npy"

    assert render_effect(effect_job(tmp_path, builtin="sum", parameters={}, control_path=None)) == {
        "status": "ok"
    }

    audio = read_raw_audio(output, 480)
    assert isinstance(audio, Audio)
    assert np.allclose(audio.samples, 0.5)


def test_render_effect_ducks_while_the_control_is_loud(tmp_path: Path) -> None:
    """The duck's own law: a full-scale control pulls the gain down by exactly amount_db, in
    proportion below it, so a control at half scale pulls a 0.5 signal down by 3 of 6 dB."""
    output = tmp_path / "out.npy"

    assert render_effect(
        effect_job(
            tmp_path,
            builtin="duck",
            parameters={"attack_ms": 1, "release_ms": 100, "amount_db": 6},
            control_path=str(write_effect_control(tmp_path, fill=0.5)),
        )
    ) == {"status": "ok"}

    audio = read_raw_audio(output, 480)
    assert isinstance(audio, Audio)
    assert np.allclose(audio.samples, 0.5 * 10 ** (-3 / 20))


@pytest.mark.parametrize("control_fill", [0.0, None])
def test_render_effect_duck_without_a_control_passes_its_audio_through(
    tmp_path: Path, control_fill: float | None
) -> None:
    output = tmp_path / "out.npy"
    job = effect_job(tmp_path, builtin="duck", parameters={})
    job["control_path"] = (
        None if control_fill is None else str(write_effect_control(tmp_path, fill=control_fill))
    )

    assert render_effect(job) == {"status": "ok"}

    audio = read_raw_audio(output, 480)
    assert isinstance(audio, Audio)
    assert np.allclose(audio.samples, 0.5)


def test_render_effect_duck_is_deterministic(tmp_path: Path) -> None:
    control = str(write_effect_control(tmp_path))

    def render(name: str) -> bytes:
        output = tmp_path / name
        assert render_effect(
            effect_job(
                tmp_path,
                builtin="duck",
                parameters={"attack_ms": 5, "release_ms": 200, "amount_db": 9},
                control_path=control,
                output_path=str(output),
            )
        ) == {"status": "ok"}
        return output.read_bytes()

    assert render("a.npy") == render("b.npy")


@pytest.mark.parametrize(
    ("parameters", "problem"),
    [
        ({"flange": 3}, "duck takes only the parameters"),
        ({"attack_ms": "fast"}, "duck's attack_ms must be a number"),
        ({"attack_ms": 0}, "duck's attack_ms must be milliseconds above zero"),
        ({"release_ms": -1}, "duck's release_ms must be milliseconds above zero"),
        ({"amount_db": -3}, "duck's amount_db must be non-negative decibels"),
    ],
    ids=["unknown", "not a number", "zero attack", "negative release", "negative amount"],
)
def test_render_effect_duck_refuses_parameters_it_cannot_use(
    tmp_path: Path, parameters: dict[str, JsonValue], problem: str
) -> None:
    result = render_effect(
        effect_job(
            tmp_path,
            builtin="duck",
            parameters=parameters,
            control_path=str(write_effect_control(tmp_path)),
        )
    )

    assert isinstance(result, dict)
    assert result.get("status") == "load_failed"
    detail = result.get("detail")
    assert isinstance(detail, str)
    assert problem in detail


def test_render_effect_refuses_a_control_wire_for_any_other_recipe(tmp_path: Path) -> None:
    result = render_effect(effect_job(tmp_path, control_path=str(write_effect_control(tmp_path))))

    assert result == {
        "status": "load_failed",
        "detail": "Gain takes no control wire: it has no sidechain input",
    }


def test_render_effect_reports_an_unusable_control_wire(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="the control wire's audio is unusable"):
        render_effect(effect_job(tmp_path, control_path=str(tmp_path / "absent.npy")))


def test_render_effect_refuses_a_job_that_is_not_exactly_its_keys(tmp_path: Path) -> None:
    with pytest.raises(MalformedJobError):
        render_effect({"path": None, "builtin": "Gain"})
    with pytest.raises(MalformedJobError):
        render_effect(effect_job(tmp_path, extra=1))
    with pytest.raises(MalformedJobError):
        render_effect(effect_job(tmp_path, path=None, builtin=None))
    with pytest.raises(MalformedJobError):
        render_effect(effect_job(tmp_path, frames=0))
    with pytest.raises(MalformedJobError):
        render_effect(effect_job(tmp_path, parameters=["gain_db"]))


def test_render_effect_reports_an_unusable_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="the effect's input is unusable"):
        render_effect(effect_job(tmp_path, audio_path=str(tmp_path / "absent.npy")))


def test_render_effect_reports_an_unknown_builtin_class(tmp_path: Path) -> None:
    result = render_effect(effect_job(tmp_path, builtin="NoSuchEffect"))

    assert result == {
        "status": "load_failed",
        "detail": "module 'pedalboard_native' has no attribute 'NoSuchEffect'",
    }


class FakeEffectPlugin:
    """A loaded effect that records its call and returns a constant."""

    is_instrument = False

    def __init__(self, fill: float = 0.25, frames_off: int = 0) -> None:
        self.fill = fill
        self.frames_off = frames_off
        self.calls: list[tuple[tuple[int, int], int, dict[str, object]]] = []

    def __call__(self, audio: np.ndarray, sample_rate: int, **kwargs: object) -> np.ndarray:
        self.calls.append((audio.shape, sample_rate, kwargs))
        return np.full((2, audio.shape[1] - self.frames_off), self.fill, np.float32)


def test_render_effect_loads_a_path_plugin(loader: FakeLoader, tmp_path: Path) -> None:
    plugin = FakeEffectPlugin(0.25)
    loader.queue.append(plugin)

    assert render_effect(effect_job(tmp_path, path="/Drive.vst3", builtin=None)) == {"status": "ok"}

    assert loader.loads == [("/Drive.vst3", None, INITIALIZATION_TIMEOUT_S)]
    [(shape, rate, kwargs)] = plugin.calls
    assert shape == (2, 480)
    assert (rate, kwargs) == (SAMPLE_RATE, {"buffer_size": BUFFER_SIZE})


def test_render_effect_refuses_an_instrument_plugin(loader: FakeLoader, tmp_path: Path) -> None:
    plugin = FakePlugin()
    loader.queue.append(plugin)

    assert render_effect(effect_job(tmp_path, path="/Serum.vst3", builtin=None)) == {
        "status": "load_failed",
        "detail": "/Serum.vst3 is not an effect",
    }
    assert plugin.calls == []


def test_render_effect_reports_a_load_exception(loader: FakeLoader, tmp_path: Path) -> None:
    loader.error = RuntimeError("bad bundle")

    assert render_effect(effect_job(tmp_path, path="/Drive.vst3", builtin=None)) == {
        "status": "load_failed",
        "detail": "bad bundle",
    }


def test_render_effect_lets_a_frame_mismatch_raise(loader: FakeLoader, tmp_path: Path) -> None:
    loader.queue.append(FakeEffectPlugin(frames_off=1))

    with pytest.raises(ValueError, match="the effect returned"):
        render_effect(effect_job(tmp_path, path="/Drive.vst3", builtin=None))


def make_effect(tmp_path: Path, options: dict[str, JsonValue] | None = None) -> PedalboardEffect:
    context = AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
    return instrument_module.create_effect(
        Identifier("drive"), options or {"builtin": "Gain", "parameters": {"gain_db": -6}}, context
    )


def test_the_effect_adapter_renders_headlessly_through_the_worker(tmp_path: Path) -> None:
    effect = make_effect(tmp_path)
    audio = Audio(np.full((2, 2400), 0.5, dtype=np.float32))
    out = tmp_path / "out.npy"

    assert effect.render(EffectRenderRequest(audio, resolved_recipe(effect), out), 60) is None

    rendered = read_raw_audio(out, 2400)
    assert isinstance(rendered, Audio)
    assert np.allclose(rendered.samples, 0.5 * 10 ** (-6 / 20))
    again = tmp_path / "again.npy"
    assert effect.render(EffectRenderRequest(audio, resolved_recipe(effect), again), 60) is None
    assert out.read_bytes() == again.read_bytes()


def test_the_duck_adapter_renders_a_control_wire_headlessly(tmp_path: Path) -> None:
    """C13's shipped consumer, through the real worker: no plugin, and byte-identical repeats."""
    effect = make_effect(tmp_path, {"builtin": "duck", "parameters": {"amount_db": 6}})
    audio = Audio(np.full((2, 2400), 0.5, dtype=np.float32))
    control = Audio(np.full((2, 2400), 0.5, dtype=np.float32))
    out = tmp_path / "out.npy"
    dry = tmp_path / "dry.npy"
    resolved = resolved_recipe(effect)

    assert effect.render(EffectRenderRequest(audio, resolved, out, control=control), 60) is None
    assert effect.render(EffectRenderRequest(audio, resolved, dry), 60) is None

    ducked = read_raw_audio(out, 2400)
    passthrough = read_raw_audio(dry, 2400)
    assert isinstance(ducked, Audio)
    assert isinstance(passthrough, Audio)
    assert np.allclose(ducked.samples, 0.5 * 10 ** (-3 / 20))
    assert np.allclose(passthrough.samples, 0.5)
    repeat = tmp_path / "repeat.npy"
    assert effect.render(EffectRenderRequest(audio, resolved, repeat, control=control), 60) is None
    assert out.read_bytes() == repeat.read_bytes()


def test_the_effect_adapter_refuses_a_control_wire_for_anything_but_duck(
    tmp_path: Path,
) -> None:
    """C13 at the adapter: the refusal names the recipe and the missing sidechain input."""
    effect = make_effect(tmp_path)
    request = EffectRenderRequest(
        Audio(np.zeros((2, 2400), dtype=np.float32)),
        resolved_recipe(effect),
        tmp_path / "out.npy",
        control=Audio(np.zeros((2, 2400), dtype=np.float32)),
    )

    assert effect.render(request, 60) == RenderFailure(
        RenderFailureStatus.LOAD_FAILED,
        "effect 'drive' has no sidechain input, so it takes no control wire",
    )


def test_the_effect_adapter_maps_worker_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    effect = make_effect(tmp_path)
    request = EffectRenderRequest(
        Audio(np.zeros((2, 2400), dtype=np.float32)), resolved_recipe(effect), tmp_path / "out.npy"
    )

    def time_out(target: object, job: object, timeout_s: float | None) -> NoReturn:
        raise WorkerTimedOutError("worker exceeded 3 seconds")

    monkeypatch.setattr(instrument_module, "run_isolated", time_out)
    assert effect.render(request, 3) == RenderFailure(
        RenderFailureStatus.TIMED_OUT, "worker exceeded 3 seconds"
    )

    def crash(target: object, job: object, timeout_s: float | None) -> NoReturn:
        raise WorkerCrashedError("exit code 1: the plugin exploded")

    monkeypatch.setattr(instrument_module, "run_isolated", crash)
    assert effect.render(request, 3) == RenderFailure(
        RenderFailureStatus.CRASHED, "exit code 1: the plugin exploded"
    )

    monkeypatch.setattr(
        instrument_module, "run_isolated", lambda target, job, timeout_s: {"status": "fine"}
    )
    assert effect.render(request, 3) == RenderFailure(RenderFailureStatus.CRASHED, MALFORMED_RESULT)


def test_the_effect_adapter_refuses_a_payload_that_is_not_its_own(tmp_path: Path) -> None:
    effect = make_effect(tmp_path)
    stranger = ResolvedEffect("drive", "identity", Json.of({"state": "x"}))

    with pytest.raises(ValueError, match="not a Pedalboard effect payload"):
        effect.render(
            EffectRenderRequest(
                Audio(np.zeros((2, 10), dtype=np.float32)), stranger, tmp_path / "o"
            ),
            60,
        )


def resolved_recipe(effect: PedalboardEffect) -> ResolvedEffect:
    resolved = effect.resolve(Identifier("drive"))
    assert isinstance(resolved, ResolvedEffect)
    return resolved


# The cycle group's block pass (C22): one job, fixed blocks, previous-line delay reads.


def group_job(
    tmp_path: Path,
    frames: int = 1500,
    **fields: JsonValue,
) -> dict[str, JsonValue]:
    """A loop of sum, delay(512), and Gain(-6), with the track into the sum and the Gain's return
    at -12 dB, written as files, so the reference below can be computed per sample."""
    members: list[JsonValue] = [
        {
            "name": "late",
            "recipe": None,
            "delay_samples": BUFFER_SIZE,
            "input_path": str(tmp_path / "late.in.npy"),
            "control_path": None,
            "output_path": str(tmp_path / "late.out.npy"),
        },
        {
            "name": "loop",
            "recipe": {"path": None, "builtin": "sum", "parameters": {}},
            "delay_samples": None,
            "input_path": str(tmp_path / "loop.in.npy"),
            "control_path": None,
            "output_path": str(tmp_path / "loop.out.npy"),
        },
        {
            "name": "loop.drive",
            "recipe": {"path": None, "builtin": "Gain", "parameters": {"gain_db": -6}},
            "delay_samples": None,
            "input_path": str(tmp_path / "drive.in.npy"),
            "control_path": None,
            "output_path": str(tmp_path / "drive.out.npy"),
        },
    ]
    for name, fill in (("loop.in", 0.5), ("late.in", 0.0), ("drive.in", 0.0)):
        write_raw_audio(tmp_path / f"{name}.npy", np.full((2, frames), fill, np.float32))
    wires: list[JsonValue] = [
        ["loop", "late", "audio", 0.0],
        ["late", "loop.drive", "audio", 0.0],
        ["loop.drive", "loop", "audio", -12.0],
    ]
    job: dict[str, JsonValue] = {
        "frames": frames,
        "members": members,
        "wires": wires,
    }
    job.update(fields)
    return job


def reference_loop(
    frames: int, amplitude: float, drive: float, return_gain: float
) -> dict[str, np.ndarray]:
    summed = np.zeros(frames, np.float32)
    delayed = np.zeros(frames, np.float32)
    driven = np.zeros(frames, np.float32)
    for t in range(frames):
        held = summed[t - BUFFER_SIZE] if t >= BUFFER_SIZE else np.float32(0.0)
        delayed[t] = held
        driven[t] = np.float32(np.float64(held) * drive)
        summed[t] = np.float32(np.float64(amplitude) + np.float64(driven[t]) * return_gain)
    return {"loop.out.npy": summed, "late.out.npy": delayed, "drive.out.npy": driven}


def test_render_group_pass_writes_every_member_whole(tmp_path: Path) -> None:
    output = render_group_job(group_job(tmp_path))

    assert output == {"status": "ok"}
    expected = reference_loop(1500, 0.5, 10 ** (-6 / 20), 10 ** (-12 / 20))
    for name, reference in expected.items():
        audio = read_raw_audio(tmp_path / name, 1500)
        assert isinstance(audio, Audio)
        assert np.array_equal(audio.samples[0], reference), name


def test_render_group_visits_exactly_the_blocks_of_the_horizon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = FakeEffectPlugin(1.0)
    patch_plugin_loading(monkeypatch, lambda *args, **kwargs: plugin)
    job = group_job(tmp_path)
    members = cast("list[dict[str, JsonValue]]", job["members"])
    job["members"] = [
        members[0],
        dict(members[1], recipe={"path": "/Drive.vst3", "builtin": None, "parameters": {}}),
        dict(members[2], recipe={"path": "/Drive.vst3", "builtin": None, "parameters": {}}),
    ]

    assert render_group_job(job) == {"status": "ok"}

    # The sum and the drive run per block, and nothing runs after the horizon ends.
    blocks = [(shape[1], rate, kwargs) for shape, rate, kwargs in plugin.calls]
    # Two plugin members, each visited once per block, in the instantaneous order.
    assert [count for count, _, _ in blocks] == [512, 512, 512, 512, 476, 476]
    assert all(kwargs == {"buffer_size": BUFFER_SIZE} for _, _, kwargs in blocks)


def test_render_group_reports_the_member_that_cannot_load(tmp_path: Path) -> None:
    job = group_job(tmp_path)
    members = cast("list[dict[str, JsonValue]]", job["members"])
    recipe = cast("dict[str, JsonValue]", members[2]["recipe"])
    recipe["builtin"] = "NoSuchEffect"

    assert render_group_job(job) == {
        "status": "load_failed",
        "detail": (
            "member 'loop.drive': module 'pedalboard_native' has no attribute 'NoSuchEffect'"
        ),
    }


def test_render_group_refuses_a_control_edge_for_any_recipe_but_the_duck(
    tmp_path: Path,
) -> None:
    control = write_effect_control(tmp_path, frames=1500)
    job = group_job(tmp_path)
    members = cast("list[dict[str, JsonValue]]", job["members"])
    members[2]["control_path"] = str(control)

    result = render_group_job(job)

    assert isinstance(result, dict)
    fields = result
    assert fields["status"] == "load_failed"
    detail = str(fields["detail"])
    assert detail.startswith("member 'loop.drive':")
    assert "takes no control wire" in detail


@pytest.mark.parametrize(
    ("job", "problem"),
    [
        ({"frames": 0, "members": [], "wires": []}, "frames"),
        ({"frames": 10, "members": [], "wires": []}, "members"),
        (
            {
                "frames": 10,
                "members": [
                    {
                        "name": "loop",
                        "recipe": None,
                        "delay_samples": 1,
                        "input_path": "",
                        "control_path": None,
                        "output_path": "",
                    }
                ],
                "wires": [["ghost", "loop", "audio", 0.0]],
            },
            "ghost",
        ),
    ],
)
def test_render_group_refuses_a_job_that_is_not_exactly_its_laws(
    tmp_path: Path, job: dict[str, JsonValue], problem: str
) -> None:
    with pytest.raises(MalformedJobError, match=problem):
        render_group_job(job)


def test_render_group_reports_an_unusable_input(tmp_path: Path) -> None:
    job = group_job(tmp_path)
    members = cast("list[dict[str, JsonValue]]", job["members"])
    members[1]["input_path"] = str(tmp_path / "absent.npy")

    with pytest.raises(ValueError, match="the group member's input is unusable"):
        render_group_job(job)


def group_request(tmp_path: Path, frames: int = 1200) -> GroupRenderRequest:
    """A loop of the adapter's drive, a sum builtin, and a delay, as the core would ask."""
    silent = Audio(np.zeros((2, frames), dtype=np.float32))
    track = Audio(np.full((2, frames), 0.5, dtype=np.float32))
    return GroupRenderRequest(
        frames=frames,
        members=(
            GroupMember(NodeName("late"), None, BUFFER_SIZE, silent, None),
            GroupMember(
                NodeName("loop"),
                ResolvedEffect(
                    "sum", "sum", Json.of({"path": None, "builtin": "sum", "parameters": {}})
                ),
                None,
                track,
                None,
            ),
            GroupMember(
                NodeName("loop.drive"), resolved_recipe(make_effect(tmp_path)), None, silent, None
            ),
        ),
        wires=(
            Wire("loop", "late", "audio"),
            Wire("late", "loop.drive", "audio"),
            Wire("loop.drive", "loop", "audio", gain_db=-12),
        ),
        outputs=(
            (NodeName("late"), tmp_path / "late.npy"),
            (NodeName("loop"), tmp_path / "loop.npy"),
            (NodeName("loop.drive"), tmp_path / "drive.npy"),
        ),
    )


def test_the_effect_adapter_renders_a_cycle_group_through_the_worker(tmp_path: Path) -> None:
    request = group_request(tmp_path)

    assert make_effect(tmp_path).render_group(request, 60) is None

    expected = reference_loop(request.frames, 0.5, 10 ** (-6 / 20), 10 ** (-12 / 20))
    for name, reference in (
        ("late.npy", "late.out.npy"),
        ("loop.npy", "loop.out.npy"),
        ("drive.npy", "drive.out.npy"),
    ):
        audio = read_raw_audio(tmp_path / name, request.frames)
        assert isinstance(audio, Audio)
        assert np.array_equal(audio.samples[0], expected[reference]), name


def test_the_group_job_the_adapter_sends_carries_every_member_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, JsonValue] = {}

    def keep(target: object, job: dict[str, JsonValue], timeout_s: float) -> dict[str, str]:
        seen.update(job)
        members = cast("list[dict[str, JsonValue]]", job["members"])
        for member in members:
            write_raw_audio(
                Path(cast("str", member["output_path"])),
                np.zeros((2, cast("int", job["frames"])), "f4"),
            )
        return {"status": "ok"}

    monkeypatch.setattr(instrument_module, "run_isolated", keep)
    request = group_request(tmp_path)

    assert make_effect(tmp_path).render_group(request, 60) is None

    assert seen["frames"] == request.frames
    assert seen["wires"] == [
        ["loop", "late", "audio", 0.0],
        ["late", "loop.drive", "audio", 0.0],
        ["loop.drive", "loop", "audio", -12.0],
    ]
    members = cast("list[dict[str, JsonValue]]", seen["members"])
    by_name = {cast("str", member["name"]): member for member in members}
    late = by_name["late"]
    loop = by_name["loop"]
    drive = by_name["loop.drive"]
    assert late["recipe"] is None
    assert late["delay_samples"] == BUFFER_SIZE
    assert cast("dict[str, JsonValue]", loop["recipe"])["builtin"] == "sum"
    assert cast("dict[str, JsonValue]", drive["recipe"])["builtin"] == "Gain"
    assert all(member["output_path"] for member in members)
    assert all(member["input_path"] for member in members)


@pytest.mark.parametrize(
    ("result", "failure"),
    [
        (
            {"status": "load_failed", "detail": "cannot load it"},
            RenderFailure(RenderFailureStatus.LOAD_FAILED, "cannot load it"),
        ),
        ({"status": "fine"}, RenderFailure(RenderFailureStatus.CRASHED, MALFORMED_RESULT)),
    ],
)
def test_the_group_adapter_maps_each_worker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, JsonValue],
    failure: RenderFailure,
) -> None:
    monkeypatch.setattr(instrument_module, "run_isolated", lambda *args: result)

    assert make_effect(tmp_path).render_group(group_request(tmp_path), 60) == failure


def test_the_group_adapter_refuses_a_control_edge_for_any_recipe_but_the_duck(
    tmp_path: Path,
) -> None:
    request = group_request(tmp_path)
    steered = dataclasses.replace(
        request,
        members=(
            *request.members[:2],
            dataclasses.replace(request.members[2], control=request.members[1].audio),
        ),
    )

    failure = make_effect(tmp_path).render_group(steered, 60)

    assert failure == RenderFailure(
        RenderFailureStatus.LOAD_FAILED,
        "effect 'drive' has no sidechain input, so it takes no control wire",
    )


def test_the_group_adapter_refuses_a_payload_that_is_not_its_own(tmp_path: Path) -> None:
    request = group_request(tmp_path)
    stranger = dataclasses.replace(
        request,
        members=(
            GroupMember(
                NodeName("late"),
                ResolvedEffect("late", "x", Json.of({"state": "x"})),
                None,
                request.members[0].audio,
                None,
            ),
            *request.members[1:],
        ),
    )

    with pytest.raises(ValueError, match="not a Pedalboard effect payload"):
        make_effect(tmp_path).render_group(stranger, 60)
