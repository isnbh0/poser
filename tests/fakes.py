"""Fakes for every port, in pure Python: no plugin, no worker, and no adapter library."""

import json
import os
import shutil
import tempfile
import threading
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

import numpy as np

from poser.core.audio import Audio, AudioProblem, read_raw_audio, write_raw_audio
from poser.core.cache import Stamp, StampProblem
from poser.core.graph import BUFFER_SIZE, Graph, instantaneous_order
from poser.core.jsonvalue import Json, JsonValue, json_object
from poser.core.names import Identifier, NodeName, SongId, Suffix
from poser.core.performance import NoteOff
from poser.core.ports import (
    AudioFileError,
    Captured,
    CaptureFailure,
    CaptureRequest,
    EffectRenderRequest,
    GroupRenderRequest,
    MixInput,
    Record,
    RenderFailure,
    RenderFailureStatus,
    RenderRequest,
    ResolvedEffect,
    ResolvedSound,
    SectionInput,
    SectionResult,
    SongBusyError,
    SoundFailure,
    SoundListing,
)
from poser.core.rendering import delay_line, delay_signal
from poser.core.report import BANDS, Levels
from poser.core.song import Song, SongError, parse_song, song_to_data
from poser.core.sounds import SoundEntry, SoundRef
from poser.core.statuses import SoundFailureStatus, StemStatus

type BadOutput = Literal["wrong_length", "nan", "no_file"]


def _amplitude(payload: Json) -> float:
    data = json_object(payload.value())
    assert data is not None
    amplitude = data["amplitude"]
    assert isinstance(amplitude, int | float)
    return float(amplitude)


def write_constant(path: Path, frames: int, amplitude: float, bad: BadOutput | None = None) -> None:
    """Write raw audio of one constant value, or the bad output named."""
    if bad == "no_file":
        return
    length = frames - 1 if bad == "wrong_length" else frames
    samples = np.full((2, length), amplitude, dtype=np.float32)
    if bad == "nan":
        samples[:, length // 2] = np.nan
    write_raw_audio(path, samples)


# Instruments


@dataclass(frozen=True)
class FakeSound:
    """A sound a fake instrument plays: one constant value, or a forced failure."""

    description: str = "a fake sound"
    identity: str = "fake"
    amplitude: float = 0.5  # 0.0 for silence.
    drift: float = 0.0  # Added to the amplitude once per earlier render, so renders can differ.
    sound_failure: SoundFailure | None = None
    render_failure: RenderFailure | None = None
    bad_output: BadOutput | None = None
    # Attempts up to this one start silent, for less of the first note on each later attempt, as
    # a plugin still loading: until the first note-off divided by the attempt number.
    late_attempts: int = 0
    # Every render is silent for this many frames from the start, as a sound that starts quietly.
    quiet_frames: int = 0


class SavedSounds:
    """Saved sounds that several fake instruments share, as instances on one directory would."""

    def __init__(self) -> None:
        self.sounds: dict[str, FakeSound] = {}
        self.lock = threading.Lock()


class FakeInstrument:
    """An instrument that can also save sounds. It records every request it receives.

    Instances given one `saved` share their saved sounds; each other instance has its own.
    """

    def __init__(
        self,
        name: str,
        sounds: Mapping[str | None, FakeSound],
        adapter_version: str = "1",
        adapter_id: str = "fake",
        saved: SavedSounds | None = None,
    ) -> None:
        self.name = Identifier(name)
        self._sounds = dict(sounds)
        self._adapter_version = adapter_version
        self._adapter_id = Identifier(adapter_id)
        library = saved or SavedSounds()
        self._saved = library.sounds
        self._saved_lock = library.lock
        self._render_counts: dict[str | None, int] = {}
        self.resolves: list[SoundRef] = []
        self.renders: list[RenderRequest] = []
        self.captures: list[CaptureRequest] = []
        self.stores: list[Identifier] = []
        # What capture and store do, set by a test.
        self.capture_failure: CaptureFailure | None = None
        self.capture_bad_output: BadOutput | None = None
        self.capture_raises: BaseException | None = None
        self.store_problem: str | None = None
        self.store_raises: BaseException | None = None

    @property
    def adapter_id(self) -> Identifier:
        return self._adapter_id

    @property
    def adapter_version(self) -> str:
        return self._adapter_version

    def _sound(self, name: str | None) -> FakeSound | None:
        if name in self._sounds:
            return self._sounds[name]
        with self._saved_lock:
            return None if name is None else self._saved.get(name)

    def sounds(self) -> SoundListing:
        names = sorted(self._sounds, key=lambda name: (name is not None, name or ""))
        entries = [
            SoundEntry(SoundRef(self.name, name), self._sounds[name].description) for name in names
        ]
        with self._saved_lock:
            saved = sorted(self._saved.items())
        entries += [
            SoundEntry(SoundRef(self.name, name), sound.description) for name, sound in saved
        ]
        return SoundListing(tuple(entries), ())

    def resolve(self, ref: SoundRef) -> ResolvedSound | SoundFailure:
        self.resolves.append(ref)
        sound = self._sound(ref.name)
        if sound is None:
            missing = "no default sound" if ref.name is None else f"no sound named {ref.name!r}"
            return SoundFailure(SoundFailureStatus.UNKNOWN_SOUND, missing)
        if sound.sound_failure is not None:
            return sound.sound_failure
        return ResolvedSound(ref, sound.identity, Json.of({"sound": ref.name}))

    def render(self, request: RenderRequest, timeout_s: float) -> RenderFailure | None:
        assert timeout_s > 0
        self.renders.append(request)
        data = json_object(request.sound.payload.value())
        assert data is not None
        name = data["sound"]
        assert name is None or isinstance(name, str)
        sound = self._sound(name)
        assert sound is not None
        if sound.render_failure is not None:
            return sound.render_failure
        count = self._render_counts.get(name, 0)
        self._render_counts[name] = count + 1
        amplitude = sound.amplitude + count * sound.drift
        write_constant(request.audio_path, request.performance.samples, amplitude, sound.bad_output)
        silent = sound.quiet_frames
        if request.attempt <= sound.late_attempts:
            first_off = next(e.sample for e in request.performance.events if isinstance(e, NoteOff))
            silent = max(silent, first_off // request.attempt)
        if silent:
            samples = np.load(request.audio_path)
            samples[:, :silent] = 0.0
            write_raw_audio(request.audio_path, samples)
        return None

    def _name_problem(self, name: Identifier, *, replace: bool) -> str | None:
        if name in self._sounds:
            return f"{name!r} names a built-in sound"
        if name in self._saved and not replace:
            return f"a sound named {name!r} already exists"
        return None

    def check_name(self, name: Identifier, *, replace: bool) -> str | None:
        with self._saved_lock:
            return self._name_problem(name, replace=replace)

    def capture(self, request: CaptureRequest) -> Captured | CaptureFailure:
        self.captures.append(request)
        if self.capture_raises is not None:
            raise self.capture_raises
        if self.capture_failure is not None:
            return self.capture_failure
        amplitude = 0.5 if request.start is None else 0.25
        write_constant(
            request.preview_path, request.preview.samples, amplitude, self.capture_bad_output
        )
        return Captured(Json.of({"name": request.name, "amplitude": amplitude}))

    def store(
        self, name: Identifier, description: str, captured: Captured, *, replace: bool
    ) -> SoundRef | str:
        self.stores.append(name)
        if self.store_raises is not None:
            raise self.store_raises
        if self.store_problem is not None:
            return self.store_problem
        with self._saved_lock:
            problem = self._name_problem(name, replace=replace)
            if problem is not None:
                return problem
            identity = f"saved:{name}:{captured.payload.text}"
            self._saved[name] = FakeSound(description, identity, _amplitude(captured.payload))
        return SoundRef(self.name, name)


class PlainInstrument:
    """A fake instrument seen only as an Instrument, without SoundCapture."""

    def __init__(self, inner: FakeInstrument) -> None:
        self._inner = inner

    @property
    def adapter_id(self) -> Identifier:
        return self._inner.adapter_id

    @property
    def adapter_version(self) -> str:
        return self._inner.adapter_version

    def sounds(self) -> SoundListing:
        return self._inner.sounds()

    def resolve(self, ref: SoundRef) -> ResolvedSound | SoundFailure:
        return self._inner.resolve(ref)

    def render(self, request: RenderRequest, timeout_s: float) -> RenderFailure | None:
        return self._inner.render(request, timeout_s)


# Effects


class FakeEffect:
    """An effect that scales the audio by the amount its payload carries, recording every
    request. Its failures are set by a test, like a FakeSound's are. A sidechain-capable one
    ducks its output by the control wire's peak level; any other refuses the control edge, as
    every recipe without a sidechain input must at render time (C13).
    """

    def __init__(
        self,
        name: str,
        identity: str = "drive",
        amount: float = 2.0,
        render_failure: RenderFailure | None = None,
        bad_output: BadOutput | None = None,
        sidechain: bool = False,
    ) -> None:
        self.name = Identifier(name)
        self._identity = identity
        self._amount = amount
        self._render_failure = render_failure
        self._bad_output = bad_output
        self._sidechain = sidechain
        self.renders: list[EffectRenderRequest] = []
        self.group_renders: list[GroupRenderRequest] = []
        self.group_blocks: list[list[int]] = []  # Each call's visited block sizes, in order.

    @property
    def adapter_id(self) -> Identifier:
        return Identifier("fake")

    @property
    def adapter_version(self) -> str:
        return "1"

    def resolve(self, recipe: str) -> ResolvedEffect | SoundFailure:
        if recipe != self.name:
            return SoundFailure(
                SoundFailureStatus.UNKNOWN_SOUND, f"no effect {recipe!r} in poser.toml"
            )
        return ResolvedEffect(recipe, self._identity, Json.of({"amount": self._amount}))

    def render(self, request: EffectRenderRequest, timeout_s: float) -> RenderFailure | None:
        assert timeout_s > 0
        self.renders.append(request)
        if request.control is not None and not self._sidechain:
            return RenderFailure(
                RenderFailureStatus.LOAD_FAILED,
                f"effect {self.name!r} has no sidechain input, so it takes no control wire",
            )
        if self._render_failure is not None:
            return self._render_failure
        payload = json_object(request.recipe.payload.value())
        assert payload is not None
        amount = float(payload["amount"])  # ty: ignore[invalid-argument-type]
        samples = np.array(request.audio.samples, dtype=np.float64) * amount
        if request.control is not None:
            # The fake's sidechain law: the control's peak pulls the output down in proportion.
            peak = float(np.max(np.abs(request.control.samples)))
            samples = samples * max(0.0, 1.0 - peak)
        frames = request.audio.frames
        if self._bad_output == "wrong_length":
            frames -= 1
        if self._bad_output == "nan":
            samples[:, frames // 2] = np.nan
        if self._bad_output != "no_file":
            write_raw_audio(request.audio_path, samples.astype(np.float32)[:, :frames])
        return None

    def render_group(self, request: GroupRenderRequest, timeout_s: float) -> RenderFailure | None:
        """Run the block pass in-process, as the Pedalboard worker does: every recipe applies to
        each fixed block, the buffers stay here, and the visited block sizes are recorded.

        Each member's own payload carries its amount, so one instance renders any member. A
        control edge pulls its block down by the block's own peak, the block-wise shape of the
        single-node law.
        """
        assert timeout_s > 0
        self.group_renders.append(request)
        blocks = [
            min(BUFFER_SIZE, request.frames - start)
            for start in range(0, request.frames, BUFFER_SIZE)
        ]
        self.group_blocks.append(blocks)
        amounts = {
            str(member.name): float(
                cast(
                    "float",
                    cast("dict[str, JsonValue]", json_object(member.recipe.payload.value()))[
                        "amount"
                    ],
                )
            )
            for member in request.members
            if member.recipe is not None
        }
        delayed = {
            str(member.name): int(member.delay_samples)
            for member in request.members
            if member.delay_samples is not None
        }
        order = instantaneous_order(
            [str(member.name) for member in request.members],
            sorted(delayed),
            [(wire.from_name, wire.to_name) for wire in request.wires],
        )
        inputs = {
            str(member.name): np.array(member.audio.samples, copy=True)
            for member in request.members
        }
        controls = {
            str(member.name): (
                None if member.control is None else np.array(member.control.samples, copy=True)
            )
            for member in request.members
        }
        buffers = {name: np.zeros_like(audio) for name, audio in inputs.items()}
        lines = {name: np.zeros_like(audio) for name, audio in inputs.items() if name in delayed}
        for start in range(0, request.frames, BUFFER_SIZE):
            end = min(start + BUFFER_SIZE, request.frames)
            for name in order:
                block = inputs[name][:, start:end].astype(np.float64)
                member_control = controls[name]
                control = None if member_control is None else member_control[:, start:end]
                for wire in request.wires:
                    if wire.to_name != name:
                        continue
                    if wire.from_name in delayed:
                        sample = delay_signal(
                            lines[wire.from_name],
                            start,
                            end - start,
                            delayed[wire.from_name],
                        )
                    else:
                        sample = buffers[wire.from_name][:, start:end]
                    if wire.kind == "audio":
                        block += sample.astype(np.float64) * 10 ** (wire.gain_db / 20)
                    else:
                        control = sample
                stepped = block.astype(np.float32)
                if name in delayed:
                    lines[name][:, start:end] = stepped
                    out = delay_line(lines[name][:, :start], stepped, delayed[name])
                else:
                    out = stepped * amounts[name]
                    if control is not None:
                        peak = float(np.max(np.abs(control)))
                        out = out * max(0.0, 1.0 - peak)
                    out = np.asarray(out, dtype=np.float32)
                buffers[name][:, start:end] = out
        for name, path in request.outputs:
            write_raw_audio(path, buffers[str(name)])
        return None


# The render store

_HELD: set[Path] = set()
_HELD_LOCK = threading.Lock()

type StampEntry = tuple[str, tuple[int, int]]


def _file_key(path: Path) -> tuple[int, int]:
    info = path.stat()
    return (info.st_size, info.st_mtime_ns)


def _image_name(node: NodeName | None, suffix: Suffix) -> str:
    # `[` is never in a node name, so the mix's image cannot share a node's.
    return f"{'[mix]' if node is None else node}{suffix}"


def _stamp_text(stamp: Stamp) -> str:
    return json.dumps(
        {
            "track_key": stamp.track_key,
            "sound_key": stamp.sound_key,
            "status": str(stamp.status),
            "peak_dbfs": stamp.peak_dbfs,
        }
    )


def _parse_stamp(text: str) -> Stamp | None:
    try:
        data = json_object(json.loads(text))
        if data is None:
            return None
        status, peak = data["status"], data["peak_dbfs"]
        if not (isinstance(status, str) and isinstance(peak, int | float)):
            return None
        return Stamp(str(data["track_key"]), str(data["sound_key"]), StemStatus(status), peak)
    except (ValueError, KeyError):
        return None


class FakeSongStore:
    """One song's files in a flat layout, with stamps kept in memory."""

    def __init__(
        self, directory: Path, stem_suffix: Suffix, stamps: dict[Path, StampEntry]
    ) -> None:
        self.directory = directory
        self._stem_suffix = stem_suffix
        self._stamps = stamps

    def _path(self, *parts: str) -> Path:
        path = self.directory.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def lock(self) -> Generator[None]:
        with _HELD_LOCK:
            if self.directory in _HELD:
                raise SongBusyError(f"another run holds {self.directory}")
            _HELD.add(self.directory)
        try:
            yield
        finally:
            with _HELD_LOCK:
                _HELD.discard(self.directory)

    def stem_path(self, node: NodeName) -> Path:
        return self._path("stems", f"{node}{self._stem_suffix}")

    def read_stamp(self, node: NodeName) -> Stamp | StampProblem:
        stem = self.stem_path(node)
        entry = self._stamps.get(stem)
        if entry is None:
            return StampProblem.MISSING
        text, key = entry
        stamp = _parse_stamp(text)
        if stamp is None:
            return StampProblem.MISSING
        if not stem.is_file() or _file_key(stem) != key:
            return StampProblem.CHANGED
        return stamp

    def write_stamp(self, node: NodeName, stamp: Stamp) -> None:
        stem = self.stem_path(node)
        self._stamps[stem] = (_stamp_text(stamp), _file_key(stem))

    def forget_stamp(self, node: NodeName) -> None:
        """Remove the node's stamp and keep its stem."""
        self._stamps.pop(self.stem_path(node), None)

    def spoil_stamp(self, node: NodeName) -> None:
        """Replace the node's stamp with text that is not a stamp."""
        stem = self.stem_path(node)
        self._stamps[stem] = ("garbage", _file_key(stem))

    def discard(self, node: NodeName) -> None:
        stem = self.stem_path(node)
        self._stamps.pop(stem, None)
        stem.unlink(missing_ok=True)

    def clear_latest(self) -> None:
        shutil.rmtree(self.directory / "latest", ignore_errors=True)

    def image_path(self, node: NodeName | None, suffix: Suffix) -> Path:
        return self._path("latest", "images", _image_name(node, suffix))

    def mix_path(self) -> Path:
        return self._path("latest", f"mix{self._stem_suffix}")

    def report_path(self) -> Path:
        return self._path("latest", "report.json")

    def records(self) -> Path:
        return self.directory / "records"

    def start_record(self, song_text: str, suffix: Suffix) -> Record:
        records = self.records()
        records.mkdir(parents=True, exist_ok=True)
        runs = [int(entry.name) for entry in records.iterdir() if entry.name.isdigit()]
        run = max(runs, default=0) + 1
        path = records / str(run)
        path.mkdir()
        (path / f"song{suffix}").write_text(song_text, encoding="utf-8")
        return Record(run, path)

    def keep_image(self, record: Record, node: NodeName | None, suffix: Suffix) -> Path:
        kept = record.path / "images" / _image_name(node, suffix)
        kept.parent.mkdir(exist_ok=True)
        shutil.copyfile(self.image_path(node, suffix), kept)
        return kept

    def finish_record(self, record: Record, report_text: str) -> None:
        (record.path / "report.json").write_text(report_text, encoding="utf-8")


class FakeRenderStore:
    """A render store with a flat layout under `root` and an in-process lock."""

    def __init__(self, root: Path, stem_suffix: str = ".npy") -> None:
        self.root = root
        self._stem_suffix = Suffix(stem_suffix)
        self._stamps: dict[Path, StampEntry] = {}

    @property
    def stem_suffix(self) -> Suffix:
        return self._stem_suffix

    def song(self, song_id: SongId) -> FakeSongStore:
        directory = self.root / "songs" / quote(song_id, safe="")
        return FakeSongStore(directory, self._stem_suffix, self._stamps)

    def preview_path(self, instrument: Identifier, name: Identifier) -> Path:
        path = self.root / "previews" / f"{instrument}.{name}{self._stem_suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


class NpyAudioFiles:
    """Audio files as raw audio, written through a temporary file."""

    @property
    def suffix(self) -> Suffix:
        return Suffix(".npy")

    def read(self, path: Path, frames: int) -> Audio:
        audio = read_raw_audio(path, frames)
        if isinstance(audio, AudioProblem):
            raise AudioFileError(audio.message)
        return audio

    def write(self, path: Path, audio: Audio) -> None:
        handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        os.close(handle)
        temporary = Path(name)
        try:
            write_raw_audio(temporary, audio.samples)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


# Analysis

FAKE_LEVELS = Levels(-14.0, dict.fromkeys(BANDS, -20.0))


class FakeMixer:
    """Applies each stem's gain and sums the stems."""

    def mix(self, stems: Sequence[MixInput], frames: int) -> Audio:
        total = np.zeros((2, frames), dtype=np.float64)
        for stem in stems:
            total += stem.audio.samples.astype(np.float64) * 10 ** (stem.gain_db / 20)
        return Audio(total.astype(np.float32))


class FakeMeasurer:
    """Returns fixed levels, and no loudness for digital silence."""

    def __init__(self, levels: Levels = FAKE_LEVELS) -> None:
        self.levels = levels

    def measure(self, audio: Audio) -> Levels:
        if not np.any(audio.samples):
            return Levels(None, self.levels.bands_db)
        return self.levels


class FakeVisualizer:
    """Writes the title to the image path."""

    def __init__(self, raises: OSError | None = None) -> None:
        self.raises = raises
        self.drawn: list[tuple[Path, str]] = []

    @property
    def suffix(self) -> Suffix:
        return Suffix(".png")

    def draw(self, audio: Audio, path: Path, title: str) -> None:
        assert audio.frames > 0
        if self.raises is not None:
            raise self.raises
        self.drawn.append((path, title))
        path.write_text(title, encoding="utf-8")


class FakeSection:
    """A report section returning fixed data and defects, or raising."""

    def __init__(
        self,
        name: str,
        data: JsonValue = None,
        defects: Sequence[str] = (),
        raises: Exception | None = None,
        version: int = 1,
    ) -> None:
        self._name = Identifier(name)
        self._data = Json.of(data)
        self._defects = tuple(Identifier(defect) for defect in defects)
        self._raises = raises
        self._version = version
        self.seen: list[SectionInput] = []
        self.parts: list[str] = []  # "track" or "mix", for each call in turn.

    @property
    def name(self) -> Identifier:
        return self._name

    @property
    def version(self) -> int:
        return self._version

    @property
    def guide(self) -> str:
        return f"## {self._name}\n\nA fake section.\n"

    def _result(self, part: str, request: SectionInput) -> SectionResult:
        self.parts.append(part)
        self.seen.append(request)
        if self._raises is not None:
            raise self._raises
        return SectionResult(self._data, self._defects)

    def track(self, request: SectionInput) -> SectionResult:
        return self._result("track", request)

    def mix(self, request: SectionInput) -> SectionResult:
        return self._result("mix", request)


class FakeRecordWriter:
    """Writes a song or graph as sorted JSON."""

    @property
    def suffix(self) -> Suffix:
        return Suffix(".json")

    def dump(self, song: Song | Graph) -> str:
        data = song_to_data(song) if isinstance(song, Song) else song.to_data()
        return json.dumps(data, sort_keys=True)


class FakeSongFormat(FakeRecordWriter):
    """Reads songs written as JSON, and writes them as FakeRecordWriter does."""

    @property
    def suffixes(self) -> tuple[Suffix, ...]:
        return (Suffix(".json"),)

    def load(self, path: Path) -> Song:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SongError([f"cannot read {path}: {error}"]) from error
        return parse_song(data)
