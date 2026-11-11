"""The scsynth adapter's machine-independent tests, proven against a stub scsynth binary.

The stub (`scsynth_stub.py`) validates what the real engine validates — the NRT record layout —
and answers every render, so this suite needs no SuperCollider. The score builder's records are
decoded here field by field, so the events a performance maps to are pinned exactly.
"""

import ast
import hashlib
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest
from contracts import check_instrument_contract
from poser_scsynth.instrument import ScsynthInstrument, create
from poser_scsynth.scores import build_score, hz

from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE, Audio, read_raw_audio
from poser.core.names import Identifier
from poser.core.performance import Control, NoteOff, NoteOn, Performance
from poser.core.ports import (
    AdapterContext,
    RenderFailure,
    RenderRequest,
    ResolvedSound,
    SoundFailure,
)
from poser.core.sounds import SoundRef
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus

if TYPE_CHECKING:
    from poser.core.jsonvalue import JsonValue

from poser_scsynth.formats import synthdef_name
from poser_scsynth.synthdefs import VOICE_BYTES

STUB_SOURCE = Path(__file__).parent / "scsynth_stub.py"
SYNTH_NAME = "additive-voice"

# The stub never parses the synthdef bytes, but the score builder reads their declared name, so
# the fixture is the real emitted demo voice: the same builder the real engine accepts.
DEMO_SYNTHDEF = VOICE_BYTES
assert synthdef_name(DEMO_SYNTHDEF) == SYNTH_NAME


def make_source(directory: Path) -> Path:
    """Write one demo sound into `directory` and return its folder."""
    source = directory / "demo"
    source.mkdir(parents=True)
    (source / f"{SYNTH_NAME}.scsyndef").write_bytes(DEMO_SYNTHDEF)
    (source / f"{SYNTH_NAME}.json").write_text('{"cc": {"1": "brightness"}}')
    return source


def make_stub(directory: Path) -> Path:
    """Write the stub binary beside the test and return its path."""
    stub = directory / "scsynth-stub"
    shutil.copy(STUB_SOURCE, stub)
    stub.chmod(0o755)
    return stub


def make_instrument(
    tmp_path: Path, scsynth: Path | None, sources: dict[str, Path] | None = None
) -> ScsynthInstrument:
    options: dict[str, JsonValue] = {}
    if scsynth is not None:
        options["scsynth"] = str(scsynth)
    if sources:
        options["sources"] = {name: str(path) for name, path in sources.items()}
    context = AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
    return create(Identifier("sc"), options, context)


@pytest.fixture
def instrument(tmp_path: Path) -> ScsynthInstrument:
    source = make_source(tmp_path)
    return make_instrument(tmp_path, make_stub(tmp_path), {"demo": source})


def performance_of(*events: Control | NoteOff | NoteOn, samples: int = SAMPLE_RATE) -> Performance:
    return Performance(samples, events)


def resolve(instrument: ScsynthInstrument) -> ResolvedSound:
    resolved = instrument.resolve(SoundRef(Identifier("sc"), f"demo/{SYNTH_NAME}"))
    assert isinstance(resolved, ResolvedSound), resolved
    return resolved


def render_failure(
    instrument: ScsynthInstrument, tmp_path: Path, timeout_s: float = 120.0
) -> object:
    """Render one two-note song on the demo sound and return the failure, if any."""
    resolved = resolve(instrument)
    performance = performance_of(NoteOn(0, 60, 100), NoteOff(SAMPLE_RATE // 4, 60))
    return instrument.render(RenderRequest(resolved, performance, tmp_path / "stem.npy"), timeout_s)


def render_ok(instrument: ScsynthInstrument, tmp_path: Path, path: str = "stem.npy") -> Audio:
    """Render the two-note song and return the exact stem."""
    resolved = resolve(instrument)
    performance = performance_of(NoteOn(0, 60, 100), NoteOff(SAMPLE_RATE // 4, 60))
    assert instrument.render(RenderRequest(resolved, performance, tmp_path / path), 120.0) is None
    audio = read_raw_audio(tmp_path / path, performance.samples)
    assert isinstance(audio, Audio)
    return audio


# The instrument contract through the stub


def test_instrument_contract(instrument: ScsynthInstrument, tmp_path: Path) -> None:
    check_instrument_contract(
        instrument, SoundRef(Identifier("sc"), f"demo/{SYNTH_NAME}"), tmp_path, timeout_s=120.0
    )


def test_default_sound_is_the_builtin_voice(instrument: ScsynthInstrument) -> None:
    first = instrument.resolve(SoundRef(Identifier("sc"), None))
    assert isinstance(first, ResolvedSound)
    again = instrument.resolve(SoundRef(Identifier("sc"), None))
    assert isinstance(again, ResolvedSound)
    assert first.identity == again.identity
    named = instrument.resolve(SoundRef(Identifier("sc"), "builtin/additive-voice"))
    assert isinstance(named, ResolvedSound)
    assert named.identity == first.identity


def test_sounds_list_default_builtin_sources_and_saved(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    saved = tmp_path / "sounds" / "sc"
    saved.mkdir(parents=True)
    (saved / "kept.scsyndef").write_bytes(DEMO_SYNTHDEF)
    (saved / "kept.json").write_text("{}")
    instrument = make_instrument(tmp_path, make_stub(tmp_path), {"demo": source})
    listing = instrument.sounds()
    assert [entry.ref.name for entry in listing.entries] == [
        None,
        "builtin/additive-voice",
        f"demo/{SYNTH_NAME}",
        "saved/kept",
    ]


def test_resolve_rejects_bad_sources(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    instrument = make_instrument(tmp_path, None, {"demo": source})
    missing = instrument.resolve(SoundRef(Identifier("sc"), "missing/voice"))
    assert isinstance(missing, SoundFailure)
    assert missing.status is SoundFailureStatus.UNKNOWN_SOUND
    gone = instrument.resolve(SoundRef(Identifier("sc"), "demo/gone"))
    assert isinstance(gone, SoundFailure)
    assert gone.status is SoundFailureStatus.UNKNOWN_SOUND
    (source / f"{SYNTH_NAME}.json").unlink()
    broken = instrument.resolve(SoundRef(Identifier("sc"), f"demo/{SYNTH_NAME}"))
    assert isinstance(broken, SoundFailure)
    assert broken.status is SoundFailureStatus.LOAD_FAILED


# The score builder


def score_events() -> tuple[Performance, dict[str, object]]:
    """Two same-pitch notes overlapping, a mapped CC, an unmapped CC, and a late CC."""
    return (
        performance_of(
            NoteOn(0, 60, 100),
            Control(10, 1, 64),
            NoteOn(20, 60, 90),
            Control(30, 7, 10),  # CC 7 has no entry in the demo meta.
            NoteOff(40, 60),
            NoteOff(50, 60),
            Control(60, 1, 100),  # No node is open any more.
        ),
        {"cc": {"1": "brightness"}},
    )


def decode_message(message: bytes) -> tuple[str, list[object]]:
    address, _, _ = message.partition(b"\0")
    cursor = len(address) + 1
    cursor += -cursor % 4
    tags_end = message.index(b"\0", cursor)
    tags = message[cursor:tags_end]
    base = tags_end + 1
    base += -base % 4
    args: list[object] = []
    index = base
    for tag in tags[1:].decode():
        if tag == "i":
            (value,) = struct.unpack(">i", message[index : index + 4])
            index += 4
            args.append(value)
        elif tag == "f":
            (value,) = struct.unpack(">f", message[index : index + 4])
            index += 4
            args.append(value)
        elif tag == "s":
            end = message.index(b"\0", index)
            args.append(message[index:end].decode())
            index = end + 1
            index += -index % 4
        elif tag == "b":
            (size,) = struct.unpack(">i", message[index : index + 4])
            index += 4
            args.append(message[index : index + size])
            index += size + -size % 4
        else:
            raise AssertionError(f"unhandled OSC tag {tag!r}")
    return address.decode(), args


def decode_score(score: bytes) -> list[tuple[float, list[tuple[str, list[object]]]]]:
    """Decode a whole command file into `(timetag seconds, messages)` records, in file order."""
    records = []
    position = 0
    while position < len(score):
        (length,) = struct.unpack(">i", score[position : position + 4])
        position += 4
        bundle = score[position : position + length]
        assert bundle[:8] == b"#bundle\0", bundle[:8]
        position += length
        seconds = struct.unpack(">II", bundle[8:16])
        time_s = (seconds[0] << 32 | seconds[1]) / 2**32
        messages: list[tuple[str, list[object]]] = []
        cursor = 16
        while cursor < length:
            (size,) = struct.unpack(">i", bundle[cursor : cursor + 4])
            cursor += 4
            messages.append(decode_message(bundle[cursor : cursor + size]))
            cursor += size
        records.append((time_s, messages))
    return records


def messages_of(
    score: list[tuple[float, list[tuple[str, list[object]]]]], address: str
) -> list[list[object]]:
    return [args for _, messages in score for found, args in messages if found == address]


def test_score_records() -> None:
    performance, meta = score_events()
    score = decode_score(build_score(DEMO_SYNTHDEF, meta, performance, SAMPLE_RATE))
    assert len(messages_of(score, "/d_recv")) == 1
    assert score[0][0] == 0.0
    recv_blob = messages_of(score, "/d_recv")[0][0]
    assert isinstance(recv_blob, bytes)
    assert recv_blob.startswith(b"SCgf")

    news = messages_of(score, "/s_new")
    assert len(news) == 2
    for position, args in enumerate(news):
        assert args[:4] == [SYNTH_NAME, 1000 + position, 0, 0]
        name, freq, _, amp, _, gate = args[4:10]
        assert (name, freq, amp, gate) == (
            "freq",
            pytest.approx(hz(60)),
            pytest.approx((100, 90)[position] / 127),
            1.0,
        )

    # Every mapped controller addresses the group, even between notes; only CC 7 drops.
    assert messages_of(score, "/n_set") == [
        [0, "brightness", pytest.approx(64 / 127)],
        [1001, "gate", 0.0],
        [1000, "gate", 0.0],
        [0, "brightness", pytest.approx(100 / 127)],
    ]
    assert news[1][-2:] == ["brightness", pytest.approx(64 / 127)]

    # Timestamps hold the sample each event falls on. The timetag quantizes to 32.32 fixed
    # point, about a 90,000th of one sample at this rate, so the comparison allows that much.
    times = [time_s for time_s, messages in score if all(a != "/d_recv" for a, _ in messages)]
    assert times == pytest.approx(
        [
            0.0,
            10 / SAMPLE_RATE,
            20 / SAMPLE_RATE,
            40 / SAMPLE_RATE,
            50 / SAMPLE_RATE,
            60 / SAMPLE_RATE,
            performance.samples / SAMPLE_RATE + 1,
        ],
        abs=0.5 / SAMPLE_RATE,
    )


def test_trailing_record_runs_past_the_end() -> None:
    performance = performance_of(NoteOn(0, 60, 100), samples=SAMPLE_RATE // 2)
    score = decode_score(build_score(DEMO_SYNTHDEF, {}, performance, SAMPLE_RATE))
    last_time, last_messages = score[-1]
    assert last_time == pytest.approx(performance.samples / SAMPLE_RATE + 1)
    assert last_messages == [], "the trailing record is an empty bundle"


def test_unmatched_note_off_drops() -> None:
    performance = performance_of(NoteOff(5, 60))
    score = decode_score(build_score(DEMO_SYNTHDEF, {}, performance, SAMPLE_RATE))
    assert messages_of(score, "/n_set") == []


def test_score_bytes_are_deterministic() -> None:
    performance, meta = score_events()
    assert build_score(DEMO_SYNTHDEF, meta, performance, SAMPLE_RATE) == build_score(
        DEMO_SYNTHDEF, meta, performance, SAMPLE_RATE
    )


# Identities


def test_identity_repeats_and_tracks_bytes(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    instrument = make_instrument(tmp_path, make_stub(tmp_path), {"demo": source})
    first = resolve(instrument).identity
    assert resolve(instrument).identity == first

    from dataclasses import replace

    from poser_scsynth.synthdefs import additive_voice, emit

    voice = additive_voice()
    changed = emit(replace(voice, params=(440.0, 0.25, 1.0)))
    (source / f"{SYNTH_NAME}.scsyndef").write_bytes(changed)
    assert resolve(instrument).identity != first

    (source / f"{SYNTH_NAME}.scsyndef").write_bytes(DEMO_SYNTHDEF)
    (source / f"{SYNTH_NAME}.json").write_text('{"cc": {"2": "brightness"}}')
    assert resolve(instrument).identity != first


def test_unrelated_source_change_leaves_identity(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    instrument = make_instrument(tmp_path, make_stub(tmp_path), {"demo": source, "more": other})
    first = resolve(instrument).identity
    (other / "late.scsyndef").write_bytes(DEMO_SYNTHDEF)
    (other / "late.json").write_text("{}")
    assert resolve(instrument).identity == first, "the scan happened twice"


# Render statuses through the stub


def test_crashed_names_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCSTUB_EXIT", "7")
    instrument = make_instrument(tmp_path, make_stub(tmp_path), {"demo": make_source(tmp_path)})
    failure = render_failure(instrument, tmp_path)
    assert isinstance(failure, RenderFailure)
    assert failure.status is RenderFailureStatus.CRASHED
    assert "stub was told to die this way" in failure.detail


def test_timed_out_reaps_the_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCSTUB_SLEEP", "5")
    stub = make_stub(tmp_path)
    instrument = make_instrument(tmp_path, stub, {"demo": make_source(tmp_path)})
    started = time.monotonic()
    failure = render_failure(instrument, tmp_path, timeout_s=1.0)
    elapsed = time.monotonic() - started
    assert isinstance(failure, RenderFailure)
    assert failure.status is RenderFailureStatus.TIMED_OUT
    assert elapsed < 4.0, "render returned before the stub's sleep ended: the child was killed"
    # The stub died before the failure returned, so nothing by its name is left running.
    assert subprocess.run(
        ["/usr/bin/pgrep", "-f", str(stub)], check=False, capture_output=True
    ).returncode


def test_missing_binary_fails_before_spawning(tmp_path: Path) -> None:
    instrument = make_instrument(
        tmp_path, tmp_path / "nowhere" / "scsynth", {"demo": make_source(tmp_path)}
    )
    failure = render_failure(instrument, tmp_path)
    assert isinstance(failure, RenderFailure)
    assert failure.status is RenderFailureStatus.LOAD_FAILED


def test_unset_binary_fails_before_spawning(tmp_path: Path) -> None:
    instrument = make_instrument(tmp_path, None, {"demo": make_source(tmp_path)})
    failure = render_failure(instrument, tmp_path)
    assert isinstance(failure, RenderFailure)
    assert failure.status is RenderFailureStatus.LOAD_FAILED


def test_short_output_is_padded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCSTUB_SHORT", "1")
    instrument = make_instrument(tmp_path, make_stub(tmp_path), {"demo": make_source(tmp_path)})
    audio = render_ok(instrument, tmp_path)
    assert audio.frames == SAMPLE_RATE


def test_non_finite_output_is_a_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCSTUB_NAN", "1")
    instrument = make_instrument(tmp_path, make_stub(tmp_path), {"demo": make_source(tmp_path)})
    failure = render_failure(instrument, tmp_path)
    assert isinstance(failure, RenderFailure)
    assert failure.status is RenderFailureStatus.CRASHED
    assert failure.detail == "non-finite audio"
    assert not (tmp_path / "stem.npy").exists()


def test_stems_are_exact_stereo_float32(tmp_path: Path) -> None:
    instrument = make_instrument(tmp_path, make_stub(tmp_path), {"demo": make_source(tmp_path)})
    audio = render_ok(instrument, tmp_path)
    assert audio.samples.shape == (NUM_CHANNELS, SAMPLE_RATE)


def test_two_renders_are_identical(tmp_path: Path) -> None:
    instrument = make_instrument(tmp_path, make_stub(tmp_path), {"demo": make_source(tmp_path)})
    first = render_ok(instrument, tmp_path, "first.npy")
    second = render_ok(instrument, tmp_path, "second.npy")
    assert np.array_equal(first.samples, second.samples)


# The boundary: what poser_scsynth may import


def _source_paths() -> list[Path]:
    root = (
        Path(__file__).resolve().parents[1] / "packages" / "poser-scsynth" / "src" / "poser_scsynth"
    )
    return sorted(root.glob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _boundary_violations(paths: list[Path]) -> list[str]:
    allowed_tops = set(sys.stdlib_module_names) | {"numpy", "scipy"}
    violations: list[str] = []
    for path in paths:
        for module in sorted(_imported_modules(path)):
            top = module.split(".", 1)[0]
            if (
                top in allowed_tops
                or module in {"poser.core", "poser_scsynth"}
                or module.startswith(("poser.core.", "poser_scsynth."))
            ):
                continue
            violations.append(f"{path.name}: {module}")
    return violations


def test_package_boundary() -> None:
    assert _boundary_violations(_source_paths()) == []


def test_boundary_catches_an_adapter_import(tmp_path: Path) -> None:
    """The adapter depends on poser core only: an adapter-library or poser.adapters import is a
    violation, whichever of the two it is."""
    smuggled = tmp_path / "smuggled.py"
    smuggled.write_text(
        "import pedalboard\n"
        "from poser.adapters import loading\n"
        "from poser.core.audio import SAMPLE_RATE\n"
    )
    assert sorted(_boundary_violations([smuggled])) == [
        f"{smuggled.name}: pedalboard",
        f"{smuggled.name}: poser.adapters",
        f"{smuggled.name}: poser.adapters.loading",
    ]


# The shipped voice's bytes are part of every identity it feeds, so they are pinned.


def test_voice_bytes_are_golden() -> None:
    assert hashlib.sha256(VOICE_BYTES).hexdigest() == (
        "80cb883ae429dd065630a70c9f6efa5725e818f259dc7ed5ceb4428614692263"
    )
    from poser_scsynth.synthdefs import additive_voice, emit

    voice = additive_voice()
    assert emit(voice) == VOICE_BYTES
    for ugen in voice.ugens:
        assert all(rate == ugen.rate for rate in ugen.outputs), ugen


def test_boundary_catches_a_pedalboard_import(tmp_path: Path) -> None:
    smuggled = tmp_path / "smuggled.py"
    smuggled.write_text("import pedalboard\nfrom poser.core.audio import SAMPLE_RATE\n")
    assert _boundary_violations([smuggled]) == [f"{smuggled.name}: pedalboard"]


# The guide composes into a workspace without the plugins, as the build law demands.


def test_guide_composes_into_a_scratch_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from poser.cli.guide import GUIDE_NAME
    from poser.cli.guide import main as guide_main
    from poser.cli.init_workspace import main as init_main

    monkeypatch.chdir(tmp_path)
    assert init_main([]) == 0
    config = tmp_path / "poser.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[instruments.sc]\n"
        'adapter = "poser_scsynth.instrument:create"\n'
        'guide = """\nThe built-in sound is `sc:builtin/additive-voice`."""\n'
    )
    assert guide_main([]) == 0
    guide = (tmp_path / GUIDE_NAME).read_text(encoding="utf-8")
    assert "## scsynth instruments" in guide
    assert "sc:builtin/additive-voice" in guide
    assert "`sc` (poser_scsynth.instrument:create)" in guide
