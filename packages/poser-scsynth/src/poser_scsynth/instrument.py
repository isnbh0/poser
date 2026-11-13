"""The scsynth instrument: sounds from folders, identities from bytes, one subprocess per render.

Building one reads nothing, starts no process, and writes no file. The first `sounds` or
`resolve` scans the sources once, and the scan stays for the instrument's lifetime. A sound is a
pair of files, `<name>.scsyndef` and `<name>.json`, the JSON holding `{"cc": {...}}`, the map
from controller number to synthdef parameter name. The identity digests the bytes the render
will read, so an equal identity means an equal render input.

`render` runs one `scsynth -N` subprocess: the process boundary is the isolation, so no worker
machinery is needed. The render is total: exact frames or an exact failure, never an exception
for an engine fault.
"""

import json
import os
import struct
import subprocess
import tempfile
import threading
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE, write_raw_audio
from poser.core.jsonvalue import Json, JsonValue
from poser.core.names import Identifier
from poser.core.performance import PitchSupport
from poser.core.ports import (
    AdapterContext,
    RenderFailure,
    RenderRequest,
    ResolvedSound,
    SoundFailure,
    SoundListing,
)
from poser.core.sounds import SoundEntry, SoundRef
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus
from poser_scsynth.config import BUILTIN_SOURCE, SAVED_SOURCE, ScsynthConfig, from_options
from poser_scsynth.formats import SoundRecipe
from poser_scsynth.scores import compile_score
from poser_scsynth.synthdefs import VOICE_BYTES, VOICE_META, VOICE_NAME

ADAPTER_VERSION = "2"
SYNTHDEF_SUFFIX = ".scsyndef"
META_SUFFIX = ".json"
BUILTIN_SOUND = f"{BUILTIN_SOURCE}/{VOICE_NAME}"
DEFAULT_DESCRIPTION = "built-in sound"
SOUND_DESCRIPTION = "synth definition"
STDERR_TAIL = 500


@dataclass(frozen=True)
class SourceScan:
    """The files a scan found by sound name, and what it could not use.

    A name is `<source>/<relative path without suffix>`; the JSON sidecar is looked up beside
    the synthdef at resolve time, so the scan reads only names.
    """

    sounds: dict[str, Path]
    problems: tuple[str, ...] = ()
    unavailable: frozenset[str] = frozenset()
    source_problems: dict[str, str] = field(default_factory=dict)
    name_problems: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


def _walk(directory: Path) -> dict[str, Path]:
    """Map the relative POSIX path of every non-hidden file under `directory` to its path.

    Symlinked folders count; link loops stop. Raises OSError for a folder it cannot read.
    """

    def unreadable(error: OSError) -> None:
        raise error

    files: dict[str, Path] = {}
    seen: set[str] = set()
    for folder, dirs, names in os.walk(directory, onerror=unreadable, followlinks=True):
        real = os.path.realpath(folder)
        if real in seen:
            dirs.clear()
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            path = Path(folder, name)
            if name.startswith(".") or not path.is_file():
                continue
            files[path.relative_to(directory).as_posix()] = path
    return files


def _group_by_stem(found: dict[str, Path]) -> dict[str, list[Path]]:
    """Group relative paths by the path without its suffix."""
    groups: dict[str, list[Path]] = {}
    for relative, path in found.items():
        groups.setdefault(relative[: -len(path.suffix)], []).append(path)
    return groups


def scan_source(source: Identifier, directory: Path) -> SourceScan:
    """Scan one configured source, which may be missing or unreadable."""
    if not directory.is_dir():
        reason = "does not exist" if not directory.exists() else "is not a directory"
        problem = f"source {source!r} directory {directory} {reason}"
        return SourceScan({}, (problem,), frozenset({source}), {source: problem})
    try:
        found = _walk(directory)
    except OSError as error:
        problem = f"cannot read source {source!r}: {error}"
        return SourceScan({}, (problem,), frozenset({source}), {source: problem})

    defs = {rel: path for rel, path in found.items() if path.suffix.lower() == SYNTHDEF_SUFFIX}
    sounds: dict[str, Path] = {}
    problems: list[str] = []
    name_problems: dict[str, str] = {}
    for stem, paths in sorted(_group_by_stem(defs).items()):
        name = f"{source}/{stem}"
        if len(paths) > 1:
            problem = f"{name} is ambiguous: {' and '.join(str(path) for path in paths)}"
            problems.append(problem)
            name_problems[name] = problem
            continue
        sounds[name] = paths[0]
    usable = {SYNTHDEF_SUFFIX, META_SUFFIX}
    stray = sum(1 for path in found.values() if path.suffix.lower() not in usable)
    notes: tuple[str, ...] = ()
    if stray:
        note = (
            f"source {source!r} holds {stray} file(s) that are neither a .scsyndef nor a "
            "json sidecar"
        )
        notes = (note,)
    return SourceScan(sounds, tuple(problems), frozenset(), {}, name_problems, notes)


def scan_saved(config: ScsynthConfig, sounds_dir: Path) -> SourceScan:
    """Scan the saved source, directly in `<sounds dir>/<instrument name>`. A missing folder
    holds no sounds."""
    directory = sounds_dir / config.name
    if not directory.is_dir():
        return SourceScan({})
    try:
        listed = sorted(directory.iterdir())
    except OSError as error:
        problem = f"cannot read saved sounds folder {directory}: {error}"
        return SourceScan({}, (problem,), frozenset({SAVED_SOURCE}), {SAVED_SOURCE: problem})
    sounds: dict[str, Path] = {}
    for path in listed:
        if path.name.startswith(".") or not path.is_file():
            continue
        if path.suffix.lower() == SYNTHDEF_SUFFIX:
            sounds[f"{SAVED_SOURCE}/{path.stem}"] = path
    return SourceScan(sounds)


def combine(*scans: SourceScan) -> SourceScan:
    """Return one scan holding everything the scans of separate sources found."""
    return SourceScan(
        sounds={name: path for scan in scans for name, path in scan.sounds.items()},
        problems=tuple(problem for scan in scans for problem in scan.problems),
        unavailable=frozenset().union(*(scan.unavailable for scan in scans)),
        source_problems={
            source: text for scan in scans for source, text in scan.source_problems.items()
        },
        name_problems={name: text for scan in scans for name, text in scan.name_problems.items()},
        notes=tuple(note for scan in scans for note in scan.notes),
    )


class ScsynthInstrument:
    """A SuperCollider engine, its sound sources, and the sounds saved with save-sound."""

    def __init__(self, config: ScsynthConfig, sounds_dir: Path, renders_dir: Path) -> None:
        self._config = config
        self._sounds_dir = sounds_dir
        self._renders_dir = renders_dir
        self._lock = threading.Lock()
        self._scan: SourceScan | None = None

    @property
    def adapter_id(self) -> Identifier:
        return Identifier("scsynth")

    @property
    def adapter_version(self) -> str:
        return ADAPTER_VERSION

    @property
    def pitches(self) -> PitchSupport:
        return PitchSupport.SEMITONES

    @property
    def guide(self) -> str:
        return resources.files(__package__).joinpath("guide.md").read_text(encoding="utf-8")

    def _ref(self, name: str | None) -> SoundRef:
        return SoundRef(self._config.name, name)

    def _scan_sources(self) -> SourceScan:
        """Scan the configured and saved sources, once, on first use."""
        with self._lock:
            if self._scan is None:
                scans = [
                    scan_source(source, directory)
                    for source, directory in sorted(self._config.sources.items())
                ]
                scans.append(scan_saved(self._config, self._sounds_dir))
                self._scan = combine(*scans)
            return self._scan

    def sounds(self) -> SoundListing:
        scan = self._scan_sources()
        entries = [
            SoundEntry(self._ref(None), DEFAULT_DESCRIPTION),
            SoundEntry(self._ref(BUILTIN_SOUND), DEFAULT_DESCRIPTION),
        ]
        entries += [SoundEntry(self._ref(name), SOUND_DESCRIPTION) for name in sorted(scan.sounds)]
        return SoundListing(tuple(entries), scan.problems, scan.notes)

    def resolve(self, ref: SoundRef) -> ResolvedSound | SoundFailure:
        if ref.name is None or ref.name == BUILTIN_SOUND:
            return self._resolved(ref, VOICE_BYTES, VOICE_META)
        scan = self._scan_sources()
        source = ref.name.split("/", 1)[0]
        if source in scan.unavailable:
            problem = scan.source_problems.get(source, "source unavailable")
            return SoundFailure(SoundFailureStatus.LOAD_FAILED, f"sound {ref}: {problem}")
        path = scan.sounds.get(ref.name)
        if path is None:
            detail = f"{self._config.name!r} has no sound {ref.name!r}"
            if problem := scan.name_problems.get(ref.name):
                detail = f"{detail}; {problem}"
            return SoundFailure(SoundFailureStatus.UNKNOWN_SOUND, detail)
        try:
            synthdef = path.read_bytes()
        except (OSError, ValueError) as error:
            return SoundFailure(SoundFailureStatus.LOAD_FAILED, f"sound {ref}: {error}")
        try:
            meta = json.loads(path.with_suffix(META_SUFFIX).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return SoundFailure(
                SoundFailureStatus.LOAD_FAILED, f"sound {ref}: its .json sidecar: {error}"
            )
        return self._resolved(ref, synthdef, meta)

    def _resolved(
        self, ref: SoundRef, synthdef: bytes, meta: object
    ) -> ResolvedSound | SoundFailure:
        """Check the sound's meta, digest its identity, and build the render payload."""
        try:
            recipe = SoundRecipe(synthdef, Json.of(meta))
        except ValueError as error:
            return SoundFailure(SoundFailureStatus.LOAD_FAILED, f"sound {ref}: {error}")
        return ResolvedSound(ref, recipe.identity(ADAPTER_VERSION), recipe.payload)

    def render(self, request: RenderRequest, timeout_s: float) -> RenderFailure | None:
        try:
            recipe = SoundRecipe.from_payload(request.sound.payload)
        except ValueError as error:
            return RenderFailure(RenderFailureStatus.LOAD_FAILED, f"scsynth payload: {error}")
        scsynth = self._config.scsynth
        if scsynth is None:
            return RenderFailure(RenderFailureStatus.LOAD_FAILED, "no scsynth binary is configured")
        if not scsynth.is_file():
            return RenderFailure(
                RenderFailureStatus.LOAD_FAILED, f"scsynth binary {scsynth} is missing"
            )
        score = compile_score(recipe, request.performance, SAMPLE_RATE)
        self._renders_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".scsynth-", dir=self._renders_dir) as directory:
            failure = self._run(scsynth, score, Path(directory), timeout_s)
            if failure is not None:
                return failure
            with warnings.catch_warnings():
                # libsndfile writes chunks scipy warns it does not know.
                warnings.simplefilter("ignore", wavfile.WavFileWarning)
                try:
                    rate, data = wavfile.read(Path(directory) / "output.wav")
                except (OSError, ValueError, EOFError, struct.error) as error:
                    return RenderFailure(
                        RenderFailureStatus.CRASHED, f"cannot read scsynth output: {error}"
                    )
        if rate != SAMPLE_RATE:
            return RenderFailure(
                RenderFailureStatus.CRASHED, f"scsynth output is at {rate} Hz, not {SAMPLE_RATE}"
            )
        return self._write(request, data)

    def _run(
        self, scsynth: Path, score: bytes, directory: Path, timeout_s: float
    ) -> RenderFailure | None:
        """Run one `scsynth -N` subprocess and return why it failed, or None when it finished."""
        score_path = directory / "score.osc"
        score_path.write_bytes(score)
        output_path = directory / "output.wav"
        command = [
            str(scsynth),
            "-N",
            str(score_path),
            "_",  # No input file.
            str(output_path),
            str(SAMPLE_RATE),
            "WAV",
            "float",
            "-o",
            str(NUM_CHANNELS),
        ]
        try:
            # subprocess.run kills and reaps the child before raising TimeoutExpired.
            finished = subprocess.run(command, timeout=timeout_s, capture_output=True, check=False)
        except subprocess.TimeoutExpired as error:
            return RenderFailure(RenderFailureStatus.TIMED_OUT, f"scsynth: {error}")
        except OSError as error:
            return RenderFailure(RenderFailureStatus.LOAD_FAILED, f"cannot start scsynth: {error}")
        if finished.returncode != 0:
            tail = finished.stderr.decode(errors="replace")[-STDERR_TAIL:].strip()
            return RenderFailure(
                RenderFailureStatus.CRASHED, f"scsynth exited {finished.returncode}: {tail}"
            )
        # scsynth reports command/load errors on stdout and can still exit zero with silence.
        # Unknown control names are ignored by the engine and do not produce these diagnostics.
        messages = (finished.stdout + finished.stderr).decode(errors="replace")
        failures = ("FAILURE IN SERVER", "*** ERROR", "exception in GraphDef", "ERROR:")
        if any(marker in messages for marker in failures):
            return RenderFailure(RenderFailureStatus.LOAD_FAILED, messages[-STDERR_TAIL:].strip())
        if not output_path.is_file():
            return RenderFailure(RenderFailureStatus.CRASHED, "scsynth wrote no output file")
        return None

    def _write(self, request: RenderRequest, data: np.ndarray) -> RenderFailure | None:
        """Reshape the engine's file to the exact stem and write it as raw audio.

        poser fixes the stem length (`core/audio.py`), so a short file is padded and a long one
        truncated. Non-finite engine output is a crash, never a successful raw-audio write.
        """
        if data.dtype != np.float32:
            return RenderFailure(RenderFailureStatus.CRASHED, "scsynth output is not float32")
        if not np.isfinite(data).all():
            return RenderFailure(RenderFailureStatus.CRASHED, "non-finite audio")
        if data.ndim != 2 or data.shape[1] != NUM_CHANNELS:
            return RenderFailure(
                RenderFailureStatus.CRASHED, f"scsynth output has shape {data.shape}, not (n, 2)"
            )
        audio = np.ascontiguousarray(data.T, dtype=np.float32)
        samples = request.performance.samples
        if audio.shape[1] < samples:
            padding = np.zeros((NUM_CHANNELS, samples - audio.shape[1]), dtype=np.float32)
            audio = np.concatenate([audio, padding], axis=1)
        audio = audio[:, :samples]
        if not np.isfinite(audio).all():
            return RenderFailure(RenderFailureStatus.CRASHED, "non-finite audio")
        write_raw_audio(request.audio_path, audio)
        return None


def create(
    name: Identifier, options: Mapping[str, JsonValue], context: AdapterContext
) -> ScsynthInstrument:
    """The factory for the adapter. Relative option paths resolve against the workspace root,
    and saved sounds live under the command's sounds directory."""
    config = from_options(name, options, context.root)
    return ScsynthInstrument(config, context.sounds_dir, context.renders_dir)
