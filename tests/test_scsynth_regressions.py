"""Regressions for scsynth's render boundary, controller state, and pure construction."""

import base64
import json
import struct
from pathlib import Path
from typing import TYPE_CHECKING, Never

import pytest
from poser_scsynth import instrument as instrument_module
from poser_scsynth.instrument import create
from poser_scsynth.scores import build_score
from test_scsynth import (
    DEMO_SYNTHDEF,
    decode_score,
    make_instrument,
    make_source,
    make_stub,
    messages_of,
    performance_of,
    render_failure,
)

from poser.core.audio import SAMPLE_RATE
from poser.core.jsonvalue import Json
from poser.core.names import Identifier
from poser.core.performance import Control, NoteOff, NoteOn
from poser.core.ports import (
    AdapterContext,
    ConfigError,
    RenderFailure,
    RenderRequest,
    ResolvedSound,
    SoundFailure,
)
from poser.core.sounds import SoundRef
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus

if TYPE_CHECKING:
    from poser.core.jsonvalue import JsonValue


def _forbid_io(*args: object, **kwargs: object) -> Never:
    pytest.fail("unexpected filesystem access or subprocess spawn")


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(Json.of({"synthdef": "%%%", "meta": {}}), id="bad-base64-alphabet"),
        pytest.param(Json.of({"synthdef": "A", "meta": {}}), id="bad-base64-length"),
        *[
            pytest.param(
                Json.of({"synthdef": base64.b64encode(body).decode(), "meta": {}}),
                id=label,
            )
            for label, body in (
                ("empty-body", b""),
                ("garbage-body", b"not a synth definition"),
                ("header-only", b"SCgf" + struct.pack(">ih", 2, 1) + b"\x05voice"),
            )
        ],
        *[
            pytest.param(
                Json.of({"synthdef": base64.b64encode(DEMO_SYNTHDEF).decode(), "meta": meta}),
                id=label,
            )
            for label, meta in (
                ("meta-not-table", []),
                ("cc-not-table", {"cc": []}),
                ("cc-param-not-string", {"cc": {"1": 64}}),
                ("cc-param-not-utf8", {"cc": {"1": "\ud800"}}),
            )
        ],
    ],
)
def test_invalid_render_payload_fails_without_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: Json
) -> None:
    instrument = make_instrument(tmp_path, make_stub(tmp_path))
    sound = ResolvedSound(SoundRef(Identifier("sc"), None), "invalid-payload", payload)
    output = tmp_path / "stem.npy"
    request = RenderRequest(sound, performance_of(NoteOn(0, 60, 100)), output)
    monkeypatch.setattr("subprocess.run", _forbid_io)
    monkeypatch.setattr("subprocess.Popen", _forbid_io)

    failure = instrument.render(request, 1.0)

    assert isinstance(failure, RenderFailure)
    assert failure.status is RenderFailureStatus.LOAD_FAILED
    assert not output.exists()


def test_unencodable_parameter_name_is_rejected_when_resolving(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    meta_path = next(source.glob("*.json"))
    meta_path.write_text(json.dumps({"cc": {"1": "\ud800"}}), encoding="utf-8")
    instrument = make_instrument(tmp_path, None, {"demo": source})
    ref = next(
        entry.ref
        for entry in instrument.sounds().entries
        if entry.ref.name and entry.ref.name.startswith("demo/")
    )
    failure = instrument.resolve(ref)
    assert isinstance(failure, SoundFailure)
    assert failure.status is SoundFailureStatus.LOAD_FAILED


def test_every_truncated_synthdef_is_rejected_at_the_boundary() -> None:
    from poser_scsynth.formats import SoundRecipe

    # Exhaust all truncation points of the shipped artifact, not just its header. A recipe
    # cannot be constructed until the whole definition (including its final count) is present.
    for stop in range(len(DEMO_SYNTHDEF)):
        with pytest.raises(ValueError, match=r"synthdef|SCgf"):
            SoundRecipe(DEMO_SYNTHDEF[:stop], Json.of({}))


def test_output_wire_rate_cannot_be_supplied_independently() -> None:
    from poser_scsynth.synthdefs import CONTROL_RATE, UGen

    control = UGen("Control", CONTROL_RATE, (), channels=3)
    assert control.outputs == (CONTROL_RATE,) * 3
    assert "outputs" not in control.__dataclass_fields__


def test_nonexecutable_binary_is_load_failed(tmp_path: Path) -> None:
    stub = make_stub(tmp_path)
    stub.chmod(0o644)
    instrument = make_instrument(tmp_path, stub, {"demo": make_source(tmp_path)})

    failure = render_failure(instrument, tmp_path)

    assert isinstance(failure, RenderFailure)
    assert failure.status is RenderFailureStatus.LOAD_FAILED
    assert not (tmp_path / "stem.npy").exists()


def _controls(args: list[object], offset: int) -> dict[str, float]:
    pairs = args[offset:]
    assert len(pairs) % 2 == 0
    result: dict[str, float] = {}
    for index in range(0, len(pairs), 2):
        name, value = pairs[index : index + 2]
        assert isinstance(name, str)
        assert isinstance(value, float)
        result[name] = value
    return result


@pytest.mark.parametrize("value", [0, 64, 127])
def test_initial_controls_are_in_first_node_arguments(value: int) -> None:
    performance = performance_of(
        Control(0, 1, value), Control(0, 2, 127 - value), NoteOn(0, 60, 100)
    )
    score = decode_score(
        build_score(
            DEMO_SYNTHDEF, {"cc": {"1": "brightness", "2": "detune"}}, performance, SAMPLE_RATE
        )
    )

    (node,) = messages_of(score, "/s_new")
    controls = _controls(node, 4)
    assert controls["brightness"] == pytest.approx(value / 127)
    assert controls["detune"] == pytest.approx((127 - value) / 127)


def test_controllers_broadcast_and_latch_across_overlaps_and_silence() -> None:
    performance = performance_of(
        Control(0, 1, 16),
        NoteOn(0, 60, 100),
        NoteOn(10, 60, 90),
        NoteOff(15, 61),  # Unmatched: neither same-pitch voice may be removed.
        Control(20, 1, 64),
        NoteOff(30, 60),
        NoteOff(40, 60),
        Control(45, 1, 100),  # No held voices: still latch for the next note.
        Control(46, 7, 127),  # Unmapped CC must not replace the mapped state.
        NoteOn(50, 67, 80),
        Control(55, 1, 127),
        NoteOff(60, 67),
    )
    score = decode_score(
        build_score(DEMO_SYNTHDEF, {"cc": {"1": "brightness"}}, performance, SAMPLE_RATE)
    )
    nodes = messages_of(score, "/s_new")
    assert len(nodes) == 3
    assert [_controls(node, 4)["brightness"] for node in nodes] == pytest.approx(
        [16 / 127, 16 / 127, 100 / 127]
    )
    first, second, third = [node[1] for node in nodes]
    changes = messages_of(score, "/n_set")
    broadcasts = [args for args in changes if "brightness" in args[1::2]]
    # Target 0 is the parent group, not just the most recently opened synth. A group update
    # also reaches release tails, which are still audible after their NoteOff.
    assert [0, "brightness", pytest.approx(64 / 127)] in broadcasts
    assert [0, "brightness", 1.0] in broadcasts
    assert all(args[0] == 0 for args in broadcasts)
    assert [args for args in changes if "gate" in args[1::2]] == [
        [second, "gate", 0.0],
        [first, "gate", 0.0],
        [third, "gate", 0.0],
    ]
    timed_changes = [
        (time_s, args)
        for time_s, messages in score
        for address, args in messages
        if address == "/n_set" and "brightness" in args[1::2]
    ]
    for sample, value in ((20, 64), (55, 127)):
        assert any(
            time_s == pytest.approx(sample / SAMPLE_RATE, abs=0.5 / SAMPLE_RATE)
            and args == [0, "brightness", pytest.approx(value / 127)]
            for time_s, args in timed_changes
        )


@pytest.mark.parametrize("value", [None, "", "  ", False, 42, 1.5, [], {}])
def test_explicit_invalid_scsynth_option_is_rejected(tmp_path: Path, value: "JsonValue") -> None:
    context = AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
    with pytest.raises(ConfigError, match="scsynth"):
        create(Identifier("sc"), {"scsynth": value}, context)


@pytest.mark.parametrize("source", ["builtin", "saved"])
def test_reserved_source_names_are_rejected(tmp_path: Path, source: str) -> None:
    context = AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
    with pytest.raises(ConfigError, match="reserved"):
        create(Identifier("sc"), {"sources": {source: "missing-source"}}, context)


def test_build_and_guide_need_no_engine_or_workspace_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert instrument_module.__file__ is not None
    expected = Path(instrument_module.__file__).with_name("guide.md").read_text(encoding="utf-8")
    root = tmp_path / "missing-workspace"
    context = AdapterContext(root, root / "missing-sounds", root / "missing-renders")
    options: dict[str, JsonValue] = {
        "scsynth": "missing-engine/scsynth",
        "sources": {"demo": "missing-source"},
    }
    monkeypatch.setattr("subprocess.run", _forbid_io)
    monkeypatch.setattr("subprocess.Popen", _forbid_io)
    monkeypatch.setattr(instrument_module, "scan_source", _forbid_io)
    monkeypatch.setattr(instrument_module, "scan_saved", _forbid_io)
    for method in ("mkdir", "write_bytes", "write_text", "read_bytes"):
        monkeypatch.setattr(Path, method, _forbid_io)
    with monkeypatch.context() as build_guard:
        for method in ("read_text", "open", "exists", "is_file", "is_dir", "iterdir"):
            build_guard.setattr(Path, method, _forbid_io)
        instrument = create(Identifier("sc"), options, context)

    read_text = Path.read_text
    assert instrument_module.__file__ is not None
    package_guide = Path(instrument_module.__file__).with_name("guide.md")
    reads: list[Path] = []

    def read_package_guide(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        assert path == package_guide, "guide may read package data only"
        reads.append(path)
        return read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_package_guide)
    assert instrument.guide == expected
    assert reads == [package_guide]
    assert not root.exists()
