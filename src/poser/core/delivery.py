"""Verified delivery: captured inputs, one loaded song, one lock, one publication.

Hashes bind declared bytes, not arbitrary Python external state or plugin determinism.
Only schema-owned artifact locations are portable.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from poser.core.analysis import REPORT_NAME, AnalysisTools, _analyze
from poser.core.jsonvalue import JsonValue
from poser.core.loading import load_song
from poser.core.names import Identifier, SongId
from poser.core.ports import (
    AudioFiles,
    DeliveryWriter,
    Effect,
    Instrument,
    RecordWriter,
    RenderStore,
    SongBusyError,
    SongFormat,
)
from poser.core.rendering import Notify, _render, ignore_progress, require_stem_format
from poser.core.report import Report, report_failed, report_to_data
from poser.core.session import song_session
from poser.core.song import SongError
from poser.core.statuses import StemStatus

# The record directories a bundle copies beside the record's report.
RECORD_ARTIFACT_DIRS = ("audio", "images")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class InputSnapshot:
    path: Path
    data: bytes | None

    @classmethod
    def capture(cls, path: Path, *, optional: bool = False) -> "InputSnapshot":
        try:
            if not path.is_file():
                if optional and not path.exists() and not path.is_symlink():
                    return cls(path, None)
                raise ValueError(f"not a regular input file: {path}")
            return cls(path, path.read_bytes())
        except FileNotFoundError:
            if optional:
                return cls(path, None)
            raise

    def unchanged(self) -> bool:
        try:
            return self == self.capture(self.path, optional=True)
        except (OSError, ValueError):
            return False


@dataclass(frozen=True)
class DeliveryInputs:
    source_root: Path
    configuration_root: Path
    song: InputSnapshot
    includes: tuple[InputSnapshot, ...]
    configuration: InputSnapshot
    lockfiles: tuple[tuple[InputSnapshot, tuple[str, ...]], ...]
    destination: Path

    def changed(self) -> tuple[Path, ...]:
        inputs = (
            self.song,
            *self.includes,
            self.configuration,
            *(snapshot for snapshot, _roles in self.lockfiles),
        )
        return tuple(item.path for item in inputs if not item.unchanged())


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


class DestinationError(ValueError):
    """The requested publication location is occupied or overlaps protected inputs/state."""


def _validate_roots(roots: tuple[Path, ...], destination: Path) -> None:
    if not isinstance(roots, tuple) or not roots:
        raise ValueError("protected roots must be a nonempty tuple")
    for root in roots:
        if not isinstance(root, Path) or not root.is_absolute() or root != root.resolve():
            raise ValueError(f"protected root is not a resolved absolute path: {root}")
        if _overlap(destination, root):
            raise DestinationError(f"destination overlaps protected render root: {root}")


def capture_inputs(
    source_root: Path,
    configuration: InputSnapshot,
    song: Path,
    includes: Sequence[Path],
    destination: Path,
    renders: RenderStore,
) -> DeliveryInputs:
    """Validate all argument/confinement boundaries before any song code is evaluated."""
    source_root = source_root.resolve()
    configuration_root = configuration.path.parent.resolve()
    if destination.exists() or destination.is_symlink():
        raise DestinationError(f"destination already exists: {destination}")
    requested_destination = destination.absolute()
    destination = destination.resolve()
    for parent in destination.parents:
        if parent.exists() and not parent.is_dir():
            raise DestinationError(f"destination parent is not a directory: {parent}")
    try:
        roots = renders.protected_roots()
        _validate_roots(roots, destination)
    except DestinationError:
        raise
    except Exception as error:
        raise ValueError(f"cannot establish protected render roots: {error}") from error
    reserved = tuple(
        path
        for root in (source_root, configuration_root)
        for path in (root / ".poser-init", (root / ".poser-init").resolve())
    )
    if any(
        _overlap(destination, path) or _overlap(requested_destination, path) for path in reserved
    ):
        raise DestinationError("destination overlaps reserved .poser-init namespace")

    def source(path: Path, *, preserve_name: bool = False) -> InputSnapshot:
        resolved = path.resolve()
        if not resolved.is_relative_to(source_root):
            raise ValueError(f"source escapes source root: {path}")
        if any(
            resolved.is_relative_to(root) or path.absolute().is_relative_to(root)
            for root in reserved
        ):
            raise ValueError(f"source is in reserved .poser-init namespace: {path}")
        return InputSnapshot.capture(path.absolute() if preserve_name else resolved)

    captured_song = source(song)
    captured_includes: list[InputSnapshot] = []
    for include in includes:
        if include.is_absolute() or ".." in include.parts or not include.parts:
            raise ValueError(f"include must be a nonescaping source-relative file: {include}")
        item = source(source_root / include, preserve_name=True)
        if item.path != captured_song.path and item not in captured_includes:
            captured_includes.append(item)
    locks: dict[Path, list[str]] = {}
    for root, role in ((source_root, "source-root"), (configuration_root, "configuration-root")):
        locks.setdefault(root / "uv.lock", []).append(role)
    captured_locks = tuple(
        (InputSnapshot.capture(path, optional=True), tuple(roles)) for path, roles in locks.items()
    )
    inputs = (
        captured_song,
        *captured_includes,
        configuration,
        *(snapshot for snapshot, _roles in captured_locks),
    )
    if any(item.path.is_relative_to(destination) for item in inputs):
        raise DestinationError("destination contains a captured input")
    # Preserve the workspace guide namespace even when its file is absent.
    guide_path = configuration_root / "POSER.md"
    if any(
        _overlap(candidate, guide)
        for candidate in (requested_destination, destination)
        for guide in (guide_path, guide_path.resolve())
    ):
        raise DestinationError("destination overlaps workspace guide: POSER.md")
    if any(configuration.path.is_relative_to(root) for root in reserved):
        raise ValueError("configuration is in reserved .poser-init namespace")
    return DeliveryInputs(
        source_root,
        configuration_root,
        captured_song,
        tuple(captured_includes),
        configuration,
        captured_locks,
        destination,
    )


@dataclass(frozen=True)
class DeliveryFailure:
    detail: str
    outcome: ClassVar[str]
    exit_code: ClassVar[int] = 1


class RenderFailed(DeliveryFailure):
    outcome = "render_failed"


class AnalysisFailed(DeliveryFailure):
    outcome = "analysis_failed"


class InputsChanged(DeliveryFailure):
    outcome = "changed_inputs"


class DestinationRefused(DeliveryFailure):
    outcome = "refused_destination"
    exit_code = 2


class InvalidInput(DeliveryFailure):
    outcome = "invalid_input"
    exit_code = 2


class SongBusy(DeliveryFailure):
    outcome = "busy"
    exit_code = 3


class DeliveryIOFailed(DeliveryFailure):
    outcome = "io_failed"


class ExecutionFailed(DeliveryFailure):
    outcome = "execution_failed"


@dataclass(frozen=True)
class PublishedDelivery:
    """A success requires an actual complete publication and a successful report."""

    destination: Path
    manifest_sha256: str
    report: Report
    outcome: ClassVar[str] = "published"
    exit_code: ClassVar[int] = 0

    def __post_init__(self) -> None:
        if report_failed(self.report) or self.report.mix is None:
            raise ValueError("published delivery requires a successful analysis")
        manifest = self.destination / "manifest.json"
        if not manifest.is_file() or sha256(manifest.read_bytes()) != self.manifest_sha256:
            raise ValueError("published delivery requires its verified manifest")
        data = json.loads(manifest.read_bytes())
        if not isinstance(data, dict) or data.get("schema_version") != 2:
            raise ValueError("published delivery requires schema version 2")
        required_fields = {
            "schema_version",
            "song_id",
            "run",
            "song_sha256",
            "sources",
            "artifacts",
            "includes",
            "configuration_sha256",
            "lockfiles",
            "poser_version",
            "listening",
        }
        if not required_fields.issubset(data) or data["listening"] != "not_auditioned":
            raise ValueError("published delivery manifest is incomplete")
        artifacts, sources = data.get("artifacts"), data.get("sources")
        if not isinstance(artifacts, dict) or not isinstance(sources, dict) or not sources:
            raise ValueError("published delivery requires source and artifact hashes")
        if data.get("run") != self.report.run or data.get("song_sha256") != self.report.song_sha256:
            raise ValueError("published delivery analysis identity does not match")
        song_id = data.get("song_id")
        if not isinstance(song_id, str) or f"source/{song_id}" not in sources:
            raise ValueError("published delivery requires the editable song source")
        mix = self.report.mix
        required = {REPORT_NAME, mix.path, mix.spectrogram, *sources}
        required.update(node.spectrogram for node in self.report.nodes if node.spectrogram)
        if not required.issubset(artifacts):
            raise ValueError("published delivery is missing required artifacts")
        if any(artifacts[name] != digest for name, digest in sources.items()):
            raise ValueError("published delivery source hashes disagree")
        for name, digest in artifacts.items():
            relative = Path(name)
            target = self.destination / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not target.resolve().is_relative_to(self.destination.resolve())
            ):
                raise ValueError("published delivery has an unsafe artifact reference")
            if not target.is_file() or sha256(target.read_bytes()) != digest:
                raise ValueError(f"published delivery artifact does not match: {name}")
        bundled = json.loads((self.destination / REPORT_NAME).read_bytes())
        if bundled != report_to_data(self.report):
            raise ValueError("published delivery report does not match its successful analysis")


type DeliveryResult = PublishedDelivery | DeliveryFailure


def result_to_data(result: DeliveryResult) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"outcome": result.outcome}
    if isinstance(result, PublishedDelivery):
        data.update(
            bundle=str(result.destination),
            manifest="manifest.json",
            manifest_sha256=result.manifest_sha256,
        )
    else:
        data["diagnostic"] = result.detail
    return data


def _bundle(
    inputs: DeliveryInputs,
    report: Report,
    record: Path,
    poser_version: str,
    song_id: SongId,
) -> dict[Path, bytes]:
    """Return the bundle's files: the sources, the published record's report, audio, and images
    under the record's own relative paths, and the manifest."""
    files: dict[Path, bytes] = {}
    sources: dict[str, JsonValue] = {}
    for snapshot in (inputs.song, *inputs.includes):
        relative = Path("source") / snapshot.path.relative_to(inputs.source_root)
        if snapshot.data is None:
            raise ValueError("captured source has no bytes")
        files[relative] = snapshot.data
        sources[relative.as_posix()] = sha256(snapshot.data)

    files[Path(REPORT_NAME)] = (record / REPORT_NAME).read_bytes()
    for directory in RECORD_ARTIFACT_DIRS:
        for path in sorted((record / directory).rglob("*")):
            if path.is_file():
                files[path.relative_to(record)] = path.read_bytes()
    configuration_bytes = inputs.configuration.data
    if configuration_bytes is None:
        raise ValueError("configuration snapshot has no bytes")
    manifest: dict[str, JsonValue] = {
        "schema_version": 2,
        "song_id": str(song_id),
        "run": report.run,
        "song_sha256": report.song_sha256,
        "sources": sources,
        "artifacts": {path.as_posix(): sha256(data) for path, data in files.items()},
        "includes": [
            item.path.relative_to(inputs.source_root).as_posix() for item in inputs.includes
        ],
        "configuration_sha256": sha256(configuration_bytes),
        "lockfiles": [
            {
                "roles": list(roles),
                "state": "absent" if snapshot.data is None else "present",
                "sha256": None if snapshot.data is None else sha256(snapshot.data),
            }
            for snapshot, roles in inputs.lockfiles
        ],
        "poser_version": poser_version,
        "listening": "not_auditioned",
    }
    files[Path("manifest.json")] = (json.dumps(manifest, indent=2, allow_nan=False) + "\n").encode()
    return files


def export_song(
    inputs: DeliveryInputs,
    song_id: SongId,
    formats: Sequence[SongFormat],
    renders: RenderStore,
    instruments: Mapping[Identifier, Instrument],
    effects: Mapping[Identifier, Effect],
    audio_files: AudioFiles,
    record_writer: RecordWriter,
    tools: AnalysisTools,
    writer: DeliveryWriter,
    *,
    poser_version: str,
    timeout_s: float,
    force: bool,
    notify: Notify = ignore_progress,
) -> DeliveryResult:
    """Load once inside one session, render/analyze it, then stage and publish its evidence.

    Each rendering of a track again after a late start is reported to `notify` as one line."""
    try:
        if not any(inputs.song.path.suffix in fmt.suffixes for fmt in formats):
            return InvalidInput(f"unsupported song suffix: {inputs.song.path.suffix}")
        require_stem_format(renders, audio_files)
        with song_session(renders, song_id) as store:
            if changed := inputs.changed():
                return InputsChanged(f"inputs changed: {', '.join(map(str, changed))}")
            loaded = load_song(inputs.song.path, formats)
            results = _render(
                loaded,
                store,
                instruments,
                audio_files,
                effects,
                timeout_s,
                force=force,
                notify=notify,
            )
            if any(result.status is not StemStatus.RENDERED for result in results):
                return RenderFailed(
                    "; ".join(
                        f"{result.track}: {result.status}: {result.detail or ''}"
                        for result in results
                        if result.status is not StemStatus.RENDERED
                    )
                )
            report = _analyze(
                loaded,
                inputs.song.path.stem,
                store,
                instruments,
                effects,
                audio_files,
                record_writer,
                tools,
            )
            if report_failed(report) or report.mix is None:
                return AnalysisFailed(f"analysis run {report.run} failed")
            record = store.record_path(report.run)
            if not (record / report.mix.path).is_file():
                return AnalysisFailed(f"reported mix is missing: {record / report.mix.path}")
            files = _bundle(inputs, report, record, poser_version, song_id)
            with writer.stage(inputs.destination, files) as staged:
                if changed := inputs.changed():
                    return InputsChanged(f"inputs changed: {', '.join(map(str, changed))}")
                staged.publish()
            return PublishedDelivery(
                inputs.destination, sha256(files[Path("manifest.json")]), report
            )
    except SongBusyError as error:
        return SongBusy(str(error))
    except FileExistsError as error:
        return DestinationRefused(str(error))
    except SongError as error:
        return ExecutionFailed(str(error))
    except OSError as error:
        return DeliveryIOFailed(str(error))
    except Exception as error:  # noqa: BLE001 -- execution failures are explicit export outcomes.
        return ExecutionFailed(f"{type(error).__name__}: {error}")
