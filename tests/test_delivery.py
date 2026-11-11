"""Delivery counterexamples: no plugins, network, or playback."""

import json
import shutil
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, override

import pytest
from fakes import (
    FakeInstrument,
    FakeMeasurer,
    FakeMixer,
    FakeRecordWriter,
    FakeRenderStore,
    FakeSection,
    FakeSound,
    FakeVisualizer,
    NpyAudioFiles,
)
from workspaces import CommandWorkspace, make_workspace, write_script_song, write_song

from poser.adapters.delivery import FileDeliveryWriter
from poser.adapters.filestore import FileRenderStore, FileSongStore
from poser.adapters.song_formats import ScriptSongFormat, YamlSongFormat
from poser.cli import export
from poser.cli.guide import workspace_guide
from poser.compose import Composition, WorkspaceConfig, load
from poser.core.analysis import AnalysisTools
from poser.core.delivery import (
    AnalysisFailed,
    DeliveryIOFailed,
    DeliveryResult,
    DestinationRefused,
    InputsChanged,
    InputSnapshot,
    PublishedDelivery,
    RenderFailed,
    SongBusy,
    capture_guide,
    capture_inputs,
    export_song,
    result_to_data,
    sha256,
)
from poser.core.guide import GENERATED
from poser.core.names import Identifier, SongId, Suffix
from poser.core.ports import (
    AdapterContext,
    DeliveryWriter,
    Record,
    RenderFailure,
    SongBusyError,
    StagedDelivery,
)
from poser.core.statuses import RenderFailureStatus

if TYPE_CHECKING:
    from poser.core.jsonvalue import JsonValue


class SvgVisualizer(FakeVisualizer):
    @property
    @override
    def suffix(self) -> Suffix:
        return Suffix(".svg")


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.config = root / "poser.toml"
        self.config.write_text("captured configuration\n")
        self.song = write_song(root / "songs" / "piece.yaml", ("lead", "fake"))
        self.identifier = SongId("songs/piece.yaml")
        self.destination = root / "deliveries" / "first-song"
        self.store = FileRenderStore(root / "renders", Suffix(".npy"))
        self.instrument = FakeInstrument("fake", {None: FakeSound(amplitude=0.2)})
        self.tools = AnalysisTools(FakeMixer(), FakeMeasurer(), SvgVisualizer())
        self.guide_path = root / "POSER.md"
        self.guide_text = f"{GENERATED}\nAdvice: off\nreference\n"
        self.guide_path.write_text(self.guide_text)
        self.includes: tuple[Path, ...] = ()
        self.writer: DeliveryWriter = FileDeliveryWriter()

    def run(self, mutation: Callable[[], None] | None = None) -> DeliveryResult:
        inputs = capture_inputs(
            self.root,
            InputSnapshot.capture(self.config),
            self.song,
            self.includes,
            self.destination,
            self.store,
        )
        evidence = capture_guide(self.guide_path, lambda: self.guide_text)
        if mutation is not None:
            mutation()
        return export_song(
            inputs,
            self.identifier,
            (YamlSongFormat(), ScriptSongFormat()),
            self.store,
            {Identifier("fake"): self.instrument},
            {},
            NpyAudioFiles(),
            FakeRecordWriter(),
            self.tools,
            self.writer,
            evidence,
            configured_advice=False,
            poser_version="test",
            timeout_s=10,
            force=True,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def manifest(destination: Path) -> bytes:
    return (destination / "manifest.json").read_bytes()


def test_bundle_hashes_portability_opaque_sections_and_later_runs(harness: Harness) -> None:
    dependency = harness.root / "parts" / "theme.py"
    dependency.parent.mkdir()
    dependency.write_text("NOTES = [60]\n")
    harness.includes = (Path("parts/theme.py"),)
    opaque: JsonValue = {"path": "/private/not-an-artifact.wav", "nested": ["../untouched.png"]}
    harness.tools = replace(harness.tools, sections=(FakeSection("opaque", opaque),))
    result = harness.run()
    assert isinstance(result, PublishedDelivery)
    original = {
        p.relative_to(harness.destination): p.read_bytes()
        for p in harness.destination.rglob("*")
        if p.is_file()
    }
    data = json.loads(manifest(harness.destination))
    assert data["listening"] == "not_auditioned"
    assert data["section_payload_portability"] == "opaque_not_verified"
    assert data["includes"] == ["parts/theme.py"]
    assert data["configuration_sha256"] == sha256(harness.config.read_bytes())
    assert data["lockfiles"] == [
        {"roles": ["source-root", "configuration-root"], "state": "absent", "sha256": None}
    ]
    assert data["guidance"] == {
        "configured_advice": False,
        "guide": {
            "state": "current",
            "sha256": sha256(harness.guide_path.read_bytes()),
            "observed_advice": False,
        },
    }
    report = json.loads(original[Path("report.json")])
    assert report["mix"]["path"] == "audio/mix.npy"
    assert report["mix"]["spectrogram"] == "images/mix.svg"
    assert report["nodes"][0]["sections"]["opaque"]["data"] == opaque
    assert report["song_sha256"] == result.report.song_sha256
    assert report["song_sha256"] != sha256(harness.song.read_bytes())
    moved = harness.root / "moved"
    shutil.move(harness.destination, moved)
    for name, digest in data["artifacts"].items():
        assert not Path(name).is_absolute()
        assert sha256((moved / name).read_bytes()) == digest
    for row in [*report["nodes"], report["mix"]]:
        assert (moved / row["spectrogram"]).is_file()
    assert (moved / report["mix"]["path"]).is_file()
    harness.destination = harness.root / "second"
    assert isinstance(harness.run(), PublishedDelivery)
    harness.instrument = FakeInstrument("fake", {None: FakeSound(amplitude=0)})
    harness.destination = harness.root / "failed"
    assert isinstance(harness.run(), RenderFailed)
    assert {
        p.relative_to(moved): p.read_bytes() for p in moved.rglob("*") if p.is_file()
    } == original


class HookWriter(FileDeliveryWriter):
    def __init__(self, hook: Callable[[], object]) -> None:
        self.hook = hook

    @contextmanager
    @override
    def stage(self, destination: Path, files: Mapping[Path, bytes]) -> Generator[StagedDelivery]:
        with super().stage(destination, files) as stage:
            self.hook()
            yield stage


def test_builder_once_lock_held_through_copy_and_published(harness: Harness) -> None:
    count = harness.root / "count"
    harness.song = write_script_song(harness.root / "songs" / "piece.py", "fake")
    harness.identifier = SongId("songs/piece.py")
    harness.song.write_text(
        harness.song.read_text().replace(
            "def build() -> Song:\n",
            "def build() -> Song:\n"
            f"    p = Path({str(count)!r})\n"
            "    p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n",
        )
    )
    original = harness.song.read_bytes()
    checks: list[str] = []

    def check_lock() -> None:
        with pytest.raises(SongBusyError), harness.store.song(harness.identifier).lock():
            pytest.fail("contender acquired export lock")
        checks.append("locked")

    harness.writer = HookWriter(check_lock)
    assert isinstance(harness.run(), PublishedDelivery)
    assert count.read_text() == "1"
    assert checks == ["locked"]
    assert (harness.destination / "source/songs/piece.py").read_bytes() == original
    with harness.store.song(harness.identifier).lock():
        pass


def test_busy_never_runs_builder(harness: Harness) -> None:
    marker = harness.root / "ran"
    harness.song = write_script_song(harness.root / "piece.py", "fake", marker)
    harness.identifier = SongId("piece.py")
    with harness.store.song(harness.identifier).lock():
        result = harness.run()
    assert isinstance(result, SongBusy)
    assert result.exit_code == 3
    assert not marker.exists()
    assert "bundle" not in result_to_data(result)


@pytest.mark.parametrize("which", ["song", "include", "configuration", "lockfile"])
@pytest.mark.parametrize("change", ["modify", "delete"])
def test_changed_inputs_during_staging_never_publish(
    harness: Harness,
    which: str,
    change: str,
) -> None:
    include = harness.root / "dep.py"
    include.write_text("original")
    harness.includes = (Path("dep.py"),)
    lock = harness.root / "uv.lock"
    lock.write_text("original lock")
    target = {
        "song": harness.song,
        "include": include,
        "configuration": harness.config,
        "lockfile": lock,
    }[which]

    def mutate() -> None:
        if change == "modify":
            target.write_bytes(target.read_bytes() + b"changed")
        else:
            target.unlink()

    harness.writer = HookWriter(mutate)
    result = harness.run()
    assert isinstance(result, InputsChanged)
    assert result.exit_code == 1
    assert not harness.destination.exists()
    assert not list(harness.root.rglob(".poser-export-*"))


def test_initially_absent_lock_created_during_export(harness: Harness) -> None:
    harness.writer = HookWriter(lambda: (harness.root / "uv.lock").write_text("new"))
    assert isinstance(harness.run(), InputsChanged)
    assert not harness.destination.exists()


def test_source_changed_by_builder_is_not_published(harness: Harness) -> None:
    harness.song = write_script_song(harness.root / "piece.py", "fake")
    harness.identifier = SongId("piece.py")
    harness.song.write_text(
        harness.song.read_text().replace(
            "def build() -> Song:\n",
            f"def build() -> Song:\n    Path({str(harness.song)!r}).write_text('# changed')\n",
        )
    )
    assert isinstance(harness.run(), InputsChanged)
    assert not harness.destination.exists()


@pytest.mark.parametrize("failure", ["render", "defect", "section", "missing_mix", "writer"])
def test_failure_outcomes_preserve_history_and_never_publish(
    harness: Harness,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected: type = AnalysisFailed
    if failure == "render":
        harness.instrument = FakeInstrument(
            "fake",
            {
                None: FakeSound(
                    render_failure=RenderFailure(RenderFailureStatus.CRASHED, "injected")
                )
            },
        )
        expected = RenderFailed
    elif failure == "defect":
        harness.instrument = FakeInstrument("fake", {None: FakeSound(amplitude=0.95)})
    elif failure == "section":
        harness.tools = replace(harness.tools, sections=(FakeSection("bad", raises=ValueError()),))
    elif failure == "missing_mix":
        original = FileSongStore.finish_record

        def missing(store: FileSongStore, record: Record, text: str) -> None:
            original(store, record, text)
            store.mix_path().unlink()

        monkeypatch.setattr(FileSongStore, "finish_record", missing)
    else:

        def fail() -> None:
            raise OSError("writer failed")

        harness.writer = HookWriter(fail)
        expected = DeliveryIOFailed
    result = harness.run()
    assert isinstance(result, expected)
    assert result.exit_code == 1
    assert not harness.destination.exists()
    assert "bundle" not in result_to_data(result)
    assert not list(harness.root.rglob(".poser-export-*"))
    if failure != "render":
        assert list((harness.root / "renders").rglob("history/*/report.json"))


def test_destination_race_does_not_replace_empty_owner_directory(harness: Harness) -> None:
    harness.writer = HookWriter(harness.destination.mkdir)
    result = harness.run()
    assert isinstance(result, DestinationRefused)
    assert result.exit_code == 2
    assert harness.destination.is_dir()
    assert list(harness.destination.iterdir()) == []
    assert not list(harness.root.rglob(".poser-export-*"))


@pytest.mark.parametrize("include", ["../outside.py", "/etc/passwd", ".", "songs"])
def test_unsafe_includes_refused(harness: Harness, include: str) -> None:
    harness.includes = (Path(include),)
    with pytest.raises(ValueError, match=r"include|regular"):
        harness.run()
    assert not harness.instrument.renders


def test_include_symlink_escape_and_reserved_namespaces(harness: Harness, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_text("private")
    (harness.root / "escape").symlink_to(outside)
    harness.includes = (Path("escape"),)
    with pytest.raises(ValueError, match="escapes"):
        harness.run()
    reserved = harness.root / ".poser-init"
    reserved.mkdir()
    (reserved / "private").write_text("private")
    harness.includes = (Path(".poser-init/private"),)
    with pytest.raises(ValueError, match="reserved"):
        harness.run()
    harness.includes = ()
    harness.destination = reserved / "bundle"
    with pytest.raises(ValueError, match="reserved"):
        harness.run()


@pytest.mark.parametrize("where", ["inside", "ancestor", "collision"])
def test_destination_preflight(harness: Harness, where: str) -> None:
    harness.destination = {
        "inside": harness.root / "renders" / "bundle",
        "ancestor": harness.root,
        "collision": harness.song,
    }[where]
    with pytest.raises(ValueError, match="destination"):
        harness.run()
    assert not harness.instrument.renders


@pytest.mark.parametrize(
    ("observed", "state", "mode"),
    [
        ("on", "stale", True),
        ("off", "current", False),
        ("foreign", "stale", None),
        ("old", "stale", None),
        ("missing", "unavailable", None),
    ],
)
def test_guidance_evidence(harness: Harness, observed: str, state: str, mode: bool | None) -> None:
    if observed == "missing":
        harness.guide_path.unlink()
    elif observed == "foreign":
        harness.guide_path.write_text("foreign\nAdvice: off\n")
    elif observed == "old":
        harness.guide_path.write_text(f"{GENERATED}\nold reference\n")
    else:
        harness.guide_path.write_text(f"{GENERATED}\nAdvice: {observed}\nreference\n")
    assert isinstance(harness.run(), PublishedDelivery)
    guidance = json.loads(manifest(harness.destination))["guidance"]
    assert guidance["configured_advice"] is False
    assert guidance["guide"]["state"] == state
    if observed == "missing":
        assert "sha256" not in guidance["guide"]
        assert "observed_advice" not in guidance["guide"]
    else:
        assert guidance["guide"]["observed_advice"] is mode
        assert guidance["guide"]["sha256"] == sha256(harness.guide_path.read_bytes())


def test_comparison_unavailable_and_guide_changed(harness: Harness) -> None:
    def unavailable() -> str:
        raise OSError("resource unavailable")

    evidence = capture_guide(harness.guide_path, unavailable).final()
    assert evidence["state"] == "unavailable"
    assert "resource unavailable" in str(evidence["reason"])
    assert "observed_advice" not in evidence
    harness.writer = HookWriter(lambda: harness.guide_path.write_text("changed"))
    assert isinstance(harness.run(), PublishedDelivery)
    assert json.loads(manifest(harness.destination))["guidance"]["guide"] == {
        "state": "unavailable",
        "reason": "changed_during_export",
    }


def test_unreadable_guide_is_unavailable(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_bytes

    def read(path: Path) -> bytes:
        if path == harness.guide_path:
            raise PermissionError("injected")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    assert isinstance(harness.run(), PublishedDelivery)
    evidence = json.loads(manifest(harness.destination))["guidance"]["guide"]
    assert evidence["state"] == "unavailable"
    assert "sha256" not in evidence


def test_success_constructor_refuses_missing_publication(harness: Harness) -> None:
    result = harness.run()
    assert isinstance(result, PublishedDelivery)
    with pytest.raises(ValueError, match="manifest"):
        PublishedDelivery(harness.root / "absent", result.manifest_sha256, result.report)
    with pytest.raises(ValueError, match="successful analysis"):
        PublishedDelivery(
            harness.destination, result.manifest_sha256, replace(result.report, mix=None)
        )
    (harness.destination / "audio/mix.npy").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="does not match"):
        PublishedDelivery(harness.destination, result.manifest_sha256, result.report)


@pytest.fixture
def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CommandWorkspace:
    workspace = make_workspace(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        Composition,
        "analysis_tools",
        lambda self: AnalysisTools(FakeMixer(), FakeMeasurer(), SvgVisualizer()),
    )
    return workspace


def test_cli_snapshot_configuration_not_a_later_read(
    cli_workspace: CommandWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    song = write_song(cli_workspace.root / "song.yaml", ("lead", "synth"))
    original = Composition.__init__

    def construct(self: Composition, config: WorkspaceConfig, context: AdapterContext) -> None:
        original(self, config, context)
        cli_workspace.config.write_text("invalid changed configuration")

    monkeypatch.setattr(Composition, "__init__", construct)
    assert export.main([str(song), "--destination", "bundle", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "changed_inputs"
    assert "bundle" not in data
    assert not (cli_workspace.root / "bundle").exists()


def test_cli_distinct_roots_and_subdirectory_include_resolution(
    cli_workspace: CommandWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = cli_workspace.root / "source-root"
    source.mkdir()
    song = write_song(source / "nested" / "song.yaml", ("lead", "synth"))
    (source / "dep.py").write_text("source dependency")
    (source / "uv.lock").write_text("source lock")
    (cli_workspace.root / "uv.lock").write_text("config lock")
    monkeypatch.chdir(source)
    assert (
        export.main(
            [
                "nested/song.yaml",
                "--config",
                str(cli_workspace.config),
                "--sounds-dir",
                "sounds",
                "--output-dir",
                "results",
                "--include",
                "dep.py",
                "--destination",
                "deliveries/bundle",
            ]
        )
        == 0
    )
    destination = source / "deliveries/bundle"
    data = json.loads(manifest(destination))
    assert data["song_id"] == "nested/song.yaml"
    assert (destination / "source/nested/song.yaml").read_bytes() == song.read_bytes()
    assert data["lockfiles"] == [
        {"roles": ["source-root"], "state": "present", "sha256": sha256(b"source lock")},
        {"roles": ["configuration-root"], "state": "present", "sha256": sha256(b"config lock")},
    ]
    monkeypatch.chdir(source / "nested")
    # With missing path options, selected configuration determines source-root, not cwd.
    assert (
        export.main(
            [
                "song.yaml",
                "--config",
                str(cli_workspace.config),
                "--include",
                "source-root/dep.py",
                "--destination",
                "another",
            ]
        )
        == 0
    )
    assert (
        json.loads(manifest(source / "nested/another"))["song_id"] == "source-root/nested/song.yaml"
    )


def test_legacy_store_refused_before_python_and_custom_actual_root(
    cli_workspace: CommandWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = cli_workspace.root / "ran"
    song = write_script_song(cli_workspace.root / "song.py", "synth", marker)
    monkeypatch.setattr(
        Composition,
        "render_store",
        lambda self: FakeRenderStore(cli_workspace.root / "hidden", ".wav"),
    )
    assert export.main([str(song), "--destination", "bundle"]) == 2
    assert not marker.exists()
    actual = cli_workspace.root / "actual"
    monkeypatch.setattr(
        Composition, "render_store", lambda self: FileRenderStore(actual, Suffix(".wav"))
    )
    assert export.main([str(song), "--destination", str(actual / "bundle")]) == 2
    assert not marker.exists()
    assert export.main([str(song), "--destination", "bundle"]) == 0
    assert marker.exists()
    assert (actual / "song.py/latest/mix.wav").exists()


@pytest.mark.parametrize("destination", ["POSER.md", "POSER.md/bundle"])
@pytest.mark.parametrize("separate_roots", [False, True])
def test_absent_guide_destination_refused_before_builder(
    cli_workspace: CommandWorkspace,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
    separate_roots: bool,
) -> None:
    marker = cli_workspace.root / "ran"
    source = cli_workspace.root / "source" if separate_roots else cli_workspace.root
    source.mkdir(exist_ok=True)
    song = write_script_song(source / "song.py", "synth", marker)
    guide = cli_workspace.root / "POSER.md"
    monkeypatch.chdir(source)
    assert not guide.exists()
    assert (
        export.main(
            [
                str(song),
                "--destination",
                str(cli_workspace.root / destination),
                "--json",
                "--config",
                str(cli_workspace.config),
                "--sounds-dir",
                str(source / "sounds"),
                "--output-dir",
                str(cli_workspace.renders),
            ]
        )
        == 2
    )
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "refused_destination"
    assert "POSER.md" in data["diagnostic"]
    assert "bundle" not in data
    assert not marker.exists()
    assert not guide.exists()


def test_cli_current_on_guide_and_collision_json(
    cli_workspace: CommandWorkspace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    song = write_song(cli_workspace.root / "song.yaml", ("lead", "synth"))
    composition = load(cli_workspace.config, cli_workspace.root / "sounds", cli_workspace.renders)
    (cli_workspace.root / "POSER.md").write_text(workspace_guide(composition))
    args = [str(song), "--destination", "bundle", "--json"]
    assert export.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["outcome"] == "published"
    evidence = json.loads(manifest(cli_workspace.root / "bundle"))["guidance"]
    assert evidence["configured_advice"] is True
    assert evidence["guide"]["state"] == "current"
    assert evidence["guide"]["observed_advice"] is True
    assert export.main(args) == 2
    second = json.loads(capsys.readouterr().out)
    assert second["outcome"] == "refused_destination"
    assert "bundle" not in second


@pytest.mark.parametrize("corruption", ["empty", "run", "digest", "missing_mix", "report"])
def test_success_constructor_rejects_forbidden_bundle_states(
    harness: Harness,
    corruption: str,
) -> None:
    result = harness.run()
    assert isinstance(result, PublishedDelivery)
    data = json.loads(manifest(harness.destination))
    if corruption == "empty":
        data = {"artifacts": {}}
    elif corruption == "run":
        data["run"] += 1
    elif corruption == "digest":
        data["song_sha256"] = "0" * 64
    elif corruption == "missing_mix":
        del data["artifacts"]["audio/mix.npy"]
    else:
        report_path = harness.destination / "report.json"
        report_path.write_text("{}")
        data["artifacts"]["report.json"] = sha256(report_path.read_bytes())
    raw = json.dumps(data).encode()
    (harness.destination / "manifest.json").write_bytes(raw)
    with pytest.raises(ValueError, match="published delivery"):
        PublishedDelivery(harness.destination, sha256(raw), result.report)


def test_python_execution_failure_is_exit_one_and_invalid_suffix_two(
    cli_workspace: CommandWorkspace,
    capsys: pytest.CaptureFixture[str],
) -> None:
    song = cli_workspace.root / "song.py"
    song.write_text("def build():\n    raise RuntimeError('execution failed')\n")
    assert export.main([str(song), "--destination", "bundle", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "execution_failed"
    bad = cli_workspace.root / "song.unknown"
    bad.write_text("not a supported format")
    assert export.main([str(bad), "--destination", "bundle", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["outcome"] == "invalid_input"


def test_confinement_method_failure_is_exit_two_before_code(
    cli_workspace: CommandWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = cli_workspace.root / "ran"
    song = write_script_song(cli_workspace.root / "song.py", "synth", marker)

    def broken(self: FileRenderStore) -> tuple[Path, ...]:
        raise RuntimeError("cannot establish confinement")

    monkeypatch.setattr(FileRenderStore, "protected_roots", broken)
    assert export.main([str(song), "--destination", "bundle"]) == 2
    assert not marker.exists()


def test_filestore_escape_is_not_a_confinement_promise(harness: Harness) -> None:
    outside = harness.root / "outside"
    outside.mkdir()
    renders = harness.root / "renders"
    renders.mkdir()
    (renders / "escaped").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink escapes"):
        harness.run()


@pytest.mark.parametrize("role", ["source", "configuration"])
@pytest.mark.parametrize("change", ["create", "delete", "modify"])
def test_dual_root_lock_changes_fail(
    cli_workspace: CommandWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    change: str,
) -> None:
    source = cli_workspace.root / "source"
    source.mkdir()
    song = write_song(source / "song.yaml", ("lead", "synth"))
    lock = (source if role == "source" else cli_workspace.root) / "uv.lock"
    if change != "create":
        lock.write_text("original")

    def mutate() -> None:
        if change == "delete":
            lock.unlink()
        else:
            lock.write_text("changed")

    monkeypatch.setattr(export, "delivery_writer", lambda: HookWriter(mutate))
    monkeypatch.chdir(source)
    assert (
        export.main(
            [
                str(song),
                "--config",
                str(cli_workspace.config),
                "--sounds-dir",
                "sounds",
                "--output-dir",
                "renders",
                "--destination",
                "bundle",
            ]
        )
        == 1
    )
    assert not (source / "bundle").exists()


def test_configuration_root_reserved_destination_with_distinct_source(
    cli_workspace: CommandWorkspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = cli_workspace.root / "source"
    source.mkdir()
    marker = source / "ran"
    song = write_script_song(source / "song.py", "synth", marker)
    monkeypatch.chdir(source)
    assert (
        export.main(
            [
                str(song),
                "--config",
                str(cli_workspace.config),
                "--sounds-dir",
                "sounds",
                "--output-dir",
                "renders",
                "--destination",
                str(cli_workspace.root / ".poser-init/out"),
            ]
        )
        == 2
    )
    assert not marker.exists()


def test_writer_cleanup_does_not_remove_owner_content(harness: Harness) -> None:
    def fail() -> None:
        (harness.destination.parent / "owner").write_text("keep")
        raise OSError("injected")

    harness.writer = HookWriter(fail)
    assert isinstance(harness.run(), DeliveryIOFailed)
    assert (harness.destination.parent / "owner").read_text() == "keep"
    assert not harness.destination.exists()


def test_safe_include_symlink_preserves_declared_editable_filename(harness: Harness) -> None:
    target = harness.root / "implementation.py"
    target.write_text("VALUE = 1\n")
    alias = harness.root / "helper.py"
    alias.symlink_to(target)
    harness.includes = (Path("helper.py"),)
    assert isinstance(harness.run(), PublishedDelivery)
    bundled = harness.destination / "source/helper.py"
    assert bundled.read_bytes() == target.read_bytes()
    assert not bundled.is_symlink()
    assert json.loads(manifest(harness.destination))["includes"] == ["helper.py"]


def test_parent_symlink_inserted_after_preflight_cannot_publish_in_store(harness: Harness) -> None:
    result = harness.run(
        lambda: harness.destination.parent.symlink_to(
            harness.root / "renders", target_is_directory=True
        )
    )
    assert isinstance(result, DestinationRefused)
    assert not (harness.root / "renders/first-song").exists()
    assert harness.destination.parent.is_symlink()
