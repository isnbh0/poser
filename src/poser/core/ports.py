"""Ports: the protocols through which the core reaches everything outside it.

Every request and result type here is a frozen dataclass that checks its rules in its constructor,
so a value that crosses a port is checked once, when it is built. Audio crosses as `Audio`,
adapter-owned payloads as `Json`, names as name types, and failure statuses as exact enums, and
every collection is a tuple, so nothing a receiver does to a value can change what another sees.
"""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from poser.core.audio import Audio
from poser.core.cache import Stamp, StampProblem
from poser.core.graph import Graph, Node, Wire
from poser.core.jsonvalue import Json
from poser.core.names import Identifier, NodeName, SongId, Suffix
from poser.core.performance import Performance
from poser.core.report import Levels, Measurements
from poser.core.song import Song, is_int, is_number
from poser.core.sounds import SoundEntry, SoundRef
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus


def _require(
    owner: str, field: str, value: object, kind: type[object] | tuple[type[object], ...]
) -> None:
    if not isinstance(value, kind):
        names = " or ".join(k.__name__ for k in kind) if isinstance(kind, tuple) else kind.__name__
        raise ValueError(f"{owner}.{field} must be a {names}, got {value!r}")


def _text(owner: str, field: str, value: object) -> None:
    if not (isinstance(value, str) and value):
        raise ValueError(f"{owner}.{field} must be non-empty text, got {value!r}")


def _tuple_of[T](owner: str, field: str, value: object, kind: type[T]) -> tuple[T, ...]:
    """Return a sequence as a tuple, raising ValueError unless every item is a `kind`."""
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError(f"{owner}.{field} must be a sequence, got {value!r}")
    items = tuple(cast("Sequence[object]", value))
    for item in items:
        if not isinstance(item, kind):
            raise ValueError(f"{owner}.{field} must hold only {kind.__name__}, got {item!r}")
    return cast("tuple[T, ...]", items)


# Song files


@runtime_checkable
class SongFormat(Protocol):
    """Reads one kind of song file into a Song or a Graph."""

    @property
    def suffixes(self) -> tuple[Suffix, ...]:
        """The file suffixes this format reads, such as `(".yaml", ".yml")`."""
        ...

    def load(self, path: Path) -> Song | Graph:
        """Read the song at `path`, raising SongError for any failure."""
        ...


@runtime_checkable
class RecordWriter(Protocol):
    """Writes a song or a graph as text for an analysis record."""

    @property
    def suffix(self) -> Suffix:
        """The record's song file is `song<suffix>`."""
        ...

    def dump(self, song: Song | Graph) -> str:
        """Return the song or graph as text, raising SongError if it cannot be written.

        Returns the same text on every call and for equal songs or graphs.
        """
        ...


# Audio files


class AudioFileError(Exception):
    """A file cannot be read as stem-format audio."""


@runtime_checkable
class AudioFiles(Protocol):
    """Reads and writes audio in the stem format: SAMPLE_RATE, NUM_CHANNELS, 32-bit float."""

    @property
    def suffix(self) -> Suffix:
        """Stems are named `<track><suffix>`."""
        ...

    def read(self, path: Path, frames: int) -> Audio:
        """Read stem-format audio of `frames` frames.

        Raises AudioFileError when the file is missing, unreadable, at another sample rate or
        channel count, holds a frame count other than `frames`, or holds non-finite samples.
        """
        ...

    def write(self, path: Path, audio: Audio) -> None:
        """Write audio in the stem format.

        Writes through a temporary file with a unique name (tempfile.mkstemp) in the same
        directory, then replaces `path`, so a reader never sees a partial file and two writers
        never share a temporary file. Removes the temporary file and raises OSError on failure.
        """
        ...


# General types


class ConfigError(Exception):
    """A workspace config or an adapter's options cannot be used."""


@dataclass(frozen=True)
class AdapterContext:
    """What a factory may know about the workspace it builds an adapter for."""

    root: Path  # The directory holding poser.toml. Relative option paths resolve against it.
    sounds_dir: Path
    renders_dir: Path

    def __post_init__(self) -> None:
        for field in ("root", "sounds_dir", "renders_dir"):
            _require("AdapterContext", field, getattr(self, field), Path)


@runtime_checkable
class Guided(Protocol):
    """An adapter that adds text to the workspace guide."""

    @property
    def guide(self) -> str:
        """Markdown starting with a `## ` heading. Reads package data only; never fails."""
        ...


# Instruments


@dataclass(frozen=True)
class ResolvedSound:
    """A sound an instrument found, ready to render."""

    ref: SoundRef
    identity: str  # A recipe: equal when the render inputs are equal. Samples may still differ.
    payload: Json  # The instrument's own data for rendering. The core never reads it.

    def __post_init__(self) -> None:
        _require("ResolvedSound", "ref", self.ref, SoundRef)
        _text("ResolvedSound", "identity", self.identity)
        _require("ResolvedSound", "payload", self.payload, Json)


@dataclass(frozen=True)
class SoundFailure:
    """Why an instrument could not resolve a sound."""

    status: SoundFailureStatus
    detail: str

    def __post_init__(self) -> None:
        _require("SoundFailure", "status", self.status, SoundFailureStatus)
        _text("SoundFailure", "detail", self.detail)


@dataclass(frozen=True)
class SoundListing:
    """The sounds an instrument offers."""

    entries: tuple[SoundEntry, ...]  # Sounds a song can name, the default sound first.
    problems: tuple[str, ...]  # Files or sources that could not be listed.
    # Files the sounds' sources hold that poser cannot read.
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        entries = _tuple_of("SoundListing", "entries", self.entries, SoundEntry)
        object.__setattr__(self, "entries", entries)
        problems = _tuple_of("SoundListing", "problems", self.problems, str)
        object.__setattr__(self, "problems", problems)
        notes = _tuple_of("SoundListing", "notes", self.notes, str)
        object.__setattr__(self, "notes", notes)


@dataclass(frozen=True)
class RenderRequest:
    """One render: a sound, what to play on it, and where to write the result."""

    sound: ResolvedSound  # From this instrument's own `resolve`.
    performance: Performance
    audio_path: Path  # Where to write the result with write_raw_audio.

    def __post_init__(self) -> None:
        _require("RenderRequest", "sound", self.sound, ResolvedSound)
        _require("RenderRequest", "performance", self.performance, Performance)
        _require("RenderRequest", "audio_path", self.audio_path, Path)


@dataclass(frozen=True)
class RenderFailure:
    """Why an instrument or an effect could not render."""

    status: RenderFailureStatus
    detail: str

    def __post_init__(self) -> None:
        _require("RenderFailure", "status", self.status, RenderFailureStatus)
        _text("RenderFailure", "detail", self.detail)


@runtime_checkable
class Instrument(Protocol):
    """Something that resolves sound names and renders performances on them.

    The contract every instrument meets:

    - `sounds()` and `resolve()` load no plugin code and run in the parent process.
    - `resolve(ref)` receives only refs whose `instrument` is this instrument's configured name;
      `ref.name is None` means the default sound. While the instrument's sources are unchanged,
      `resolve` of a listed entry never returns UNKNOWN_SOUND, though it may return LOAD_FAILED
      when the sound's data cannot be read.
    - An instrument may read its sources once and keep what it read for its lifetime, except that
      a successful `SoundCapture.store` makes its sound listed and resolvable on the same instance.
    - `identity` is a recipe identity: equal when the render inputs are equal, without promising
      identical samples. It covers every input the adapter controls and can read cheaply.
    - `adapter_version` rises whenever the adapter's own code renders one identity differently.
    """

    @property
    def adapter_id(self) -> Identifier:
        """The adapter's name, such as `pedalboard`."""
        ...

    @property
    def adapter_version(self) -> str:
        """Non-empty; raised when the output for one identity changes."""
        ...

    def sounds(self) -> SoundListing:
        """List the sounds a song can name on this instrument, the default sound first."""
        ...

    def resolve(self, ref: SoundRef) -> ResolvedSound | SoundFailure:
        """Find the sound `ref` names, or say why it cannot be used."""
        ...

    def render(self, request: RenderRequest, timeout_s: float) -> RenderFailure | None:
        """Render the performance on the sound, or return why it could not.

        Writes exactly `(NUM_CHANNELS, request.performance.samples)` float32 frames to
        `request.audio_path` with `write_raw_audio` and returns None, or returns a RenderFailure
        and may leave the file absent. Returns only after every writer of `audio_path` has
        finished. Never raises for plugin failures; anything it raises is a poser bug.
        LOAD_FAILED means a plugin or state that does not load, CRASHED a worker that died, and
        TIMED_OUT one past `timeout_s`.
        """
        ...


# Effects


@dataclass(frozen=True)
class ResolvedEffect:
    """An effect recipe an adapter resolved, ready to render."""

    recipe: str  # The configured recipe name the adapter resolved.
    identity: str  # A recipe: equal when the render inputs are equal. Samples may still differ.
    payload: Json  # The adapter's own data for rendering. The core never reads it.

    def __post_init__(self) -> None:
        _text("ResolvedEffect", "recipe", self.recipe)
        _text("ResolvedEffect", "identity", self.identity)
        _require("ResolvedEffect", "payload", self.payload, Json)


@dataclass(frozen=True)
class EffectRenderRequest:
    """One effect render: the summed upstream audio, the one control wire's stem when the node is
    fed one, the resolved recipe, and where to write."""

    audio: Audio
    recipe: ResolvedEffect  # From this adapter's own `resolve`.
    audio_path: Path  # Where to write the result with write_raw_audio.
    control: Audio | None = None  # The control wire's stem; frames match `audio` or it is None.

    def __post_init__(self) -> None:
        _require("EffectRenderRequest", "audio", self.audio, Audio)
        _require("EffectRenderRequest", "recipe", self.recipe, ResolvedEffect)
        _require("EffectRenderRequest", "audio_path", self.audio_path, Path)
        if self.control is not None:
            _require("EffectRenderRequest", "control", self.control, Audio)
            if self.control.frames != self.audio.frames:
                raise ValueError(
                    f"EffectRenderRequest.control must hold the audio's {self.audio.frames} "
                    f"frames, got {self.control.frames}"
                )


@runtime_checkable
class Effect(Protocol):
    """Something that resolves one configured effect recipe and renders audio through it.

    The contract every effect meets:

    - `resolve()` loads no plugin code and runs in the parent process.
    - `resolve(recipe)` receives only the name this effect is configured under; the effect holds
      one recipe, so its identity is fixed at build time and `resolve` never fails for a
      configured one.
    - `render` receives `request.control` only when the rendered node was fed a control wire. An
      effect whose recipe cannot use a control edge returns a LOAD_FAILED failure whose detail
      names the recipe and the missing control input; whether a recipe takes one is a render-time
      fact, since build time loads no plugin and promises no plugin surface.
    - `identity` is a recipe identity: equal when the render inputs are equal, without promising
      identical samples. It covers every input the adapter controls and can read cheaply.
    - `adapter_version` rises whenever the adapter's own code renders one identity differently.
    """

    @property
    def adapter_id(self) -> Identifier:
        """The adapter's name, such as `pedalboard`."""
        ...

    @property
    def adapter_version(self) -> str:
        """Non-empty; raised when the output for one identity changes."""
        ...

    def resolve(self, recipe: str) -> ResolvedEffect | SoundFailure:
        """Return the recipe ready to render, or say why it cannot be used."""
        ...

    def render(self, request: EffectRenderRequest, timeout_s: float) -> RenderFailure | None:
        """Render the audio through the recipe, or return why it could not.

        Writes exactly as many frames as `request.audio` carries, at `SAMPLE_RATE` and
        `NUM_CHANNELS`, to `request.audio_path` with `write_raw_audio` and returns None, or
        returns a RenderFailure and may leave the file absent. A control edge the recipe cannot
        use is a LOAD_FAILED failure naming the recipe and the missing control input. Returns
        only after every writer of `audio_path` has finished. Never raises for plugin failures;
        anything it raises is a poser bug. LOAD_FAILED means a plugin or recipe that does not
        load, CRASHED a worker that died, and TIMED_OUT one past `timeout_s`.
        """
        ...


# Cycle groups


@dataclass(frozen=True)
class GroupMember:
    """One member of a cycle group: an effect recipe, or a delay with its declared samples,
    together with the audio and control the group is fed from outside itself."""

    name: NodeName
    recipe: ResolvedEffect | None  # None exactly when the member is a delay node.
    delay_samples: int | None  # None exactly when the member is an effect node.
    audio: Audio  # The member's summed external audio, whole horizon; silence when it has none.
    control: Audio | None = None  # The member's one external control stem, or None.

    def __post_init__(self) -> None:
        _require("GroupMember", "name", self.name, NodeName)
        if (self.recipe is None) == (self.delay_samples is None):
            raise ValueError(
                "GroupMember needs exactly one of recipe and delay_samples, got "
                f"{self.recipe!r} and {self.delay_samples!r}"
            )
        _require("GroupMember", "audio", self.audio, Audio)
        if self.control is not None:
            _require("GroupMember", "control", self.control, Audio)


@dataclass(frozen=True)
class GroupRenderRequest:
    """One cycle group's render: its members, the wires between them, and each output's path.

    The adapter runs the block pass (C22) inside one worker job: it loads every recipe once,
    steps `frames` through fixed `BUFFER_SIZE` blocks in the instantaneous graph's order, keeps
    the feedback buffers in the job, and writes each member's whole output.
    """

    frames: int  # Every member's input, control, and output hold exactly this many frames.
    members: tuple[GroupMember, ...]
    wires: tuple[Wire, ...]  # Wires whose ends are both members; nothing else.
    outputs: tuple[tuple[NodeName, Path], ...]  # One output path per member.

    def __post_init__(self) -> None:
        if not (is_int(self.frames) and self.frames >= 1):
            raise ValueError(
                f"GroupRenderRequest.frames must be a positive integer, got {self.frames!r}"
            )
        members = _tuple_of("GroupRenderRequest", "members", self.members, GroupMember)
        object.__setattr__(self, "members", members)
        if not members:
            raise ValueError("GroupRenderRequest.members must hold at least one member")
        names = [member.name for member in members]
        if len(set(names)) != len(names):
            raise ValueError(f"GroupRenderRequest.members repeats a name: {names}")
        for member in members:
            if member.audio.frames != self.frames or (
                member.control is not None and member.control.frames != self.frames
            ):
                raise ValueError(
                    f"GroupMember {member.name!r}'s audio must hold the request's "
                    f"{self.frames} frames"
                )
        wires = _tuple_of("GroupRenderRequest", "wires", self.wires, Wire)
        for wire in wires:
            for end in (wire.from_name, wire.to_name):
                if end not in names:
                    raise ValueError(
                        f"GroupRenderRequest.wires names {end!r}, which is no member's name"
                    )
        object.__setattr__(self, "wires", wires)
        outputs = _tuple_of("GroupRenderRequest", "outputs", self.outputs, tuple)
        for output in outputs:
            name, path = output
            if not isinstance(name, NodeName) or not isinstance(path, Path):
                raise ValueError(
                    f"GroupRenderRequest.outputs must pair NodeNames with Paths, got {output!r}"
                )
        if {name for name, _ in outputs} != set(names):
            raise ValueError("GroupRenderRequest.outputs must hold exactly one path per member")
        object.__setattr__(self, "outputs", outputs)


@runtime_checkable
class GroupEffect(Protocol):
    """An effect adapter that renders a cycle group as one block pass.

    The contract a group renderer meets, beyond `Effect`'s:

    - `render_group` runs in the parent process but loads plugin code only inside its one worker
      job, exactly as `render` does.
    - It loads every member's recipe once, steps `request.frames` through fixed `BUFFER_SIZE`
      blocks in the instantaneous graph's order, lets a wire out of a delay member read the
      previous block's output, and keeps the feedback buffers in the job, never in the store.
    - It writes exactly `request.frames` float32 frames at `SAMPLE_RATE` and `NUM_CHANNELS` to
      every member's output path with `write_raw_audio`, or returns a RenderFailure naming the
      member that could not load, and may leave the files absent.
    """

    @property
    def adapter_id(self) -> Identifier:
        """The adapter's name, such as `pedalboard`."""
        ...

    def render_group(self, request: GroupRenderRequest, timeout_s: float) -> RenderFailure | None:
        """Render the whole cycle group, or return why it could not."""
        ...


# Sound capture


@dataclass(frozen=True)
class CaptureRequest:
    """What the owner starts from when shaping a new sound, and what to preview it with."""

    name: Identifier  # The new sound's name, already accepted by check_name.
    start: ResolvedSound | None  # The sound to open on, from this instrument; None for default.
    preview: Performance  # Played on the captured sound to make the preview.
    preview_path: Path  # Where to write the preview with write_raw_audio.

    def __post_init__(self) -> None:
        _require("CaptureRequest", "name", self.name, Identifier)
        if self.start is not None:
            _require("CaptureRequest", "start", self.start, ResolvedSound)
        _require("CaptureRequest", "preview", self.preview, Performance)
        _require("CaptureRequest", "preview_path", self.preview_path, Path)


@dataclass(frozen=True)
class Captured:
    """A sound the owner shaped, not yet saved."""

    payload: Json  # The instrument's own data for `store`. The core never reads it.

    def __post_init__(self) -> None:
        _require("Captured", "payload", self.payload, Json)


@dataclass(frozen=True)
class CaptureFailure:
    """Why a capture produced no sound."""

    detail: str

    def __post_init__(self) -> None:
        _text("CaptureFailure", "detail", self.detail)


@runtime_checkable
class SoundCapture(Protocol):
    """An optional capability of an instrument: letting the owner shape and save a sound."""

    def check_name(self, name: Identifier, *, replace: bool) -> str | None:
        """Return a problem with saving a sound under `name`, or None.

        Runs before any window opens.
        """
        ...

    def capture(self, request: CaptureRequest) -> Captured | CaptureFailure:
        """Let the owner shape a sound, then render `request.preview` from it.

        Blocks until the owner is done, with no time limit, and returns only after every writer
        of `request.preview_path` has finished. Never raises for plugin failures.
        """
        ...

    def store(
        self, name: Identifier, description: str, captured: Captured, *, replace: bool
    ) -> SoundRef | str:
        """Save the sound so that `resolve` on this instance finds it as the returned ref, or
        return a problem.

        Returning the ref is the commit: the sound is saved from then on. Repeats check_name
        atomically with the save, so of two saves racing for one name, or for names that clash,
        one is saved and the other gets a problem. Raises OSError when writing fails, leaving no
        partial sound.
        """
        ...


# Analysis


@dataclass(frozen=True)
class MixInput:
    """One stem going into the mix, and the gain to apply to it."""

    track: NodeName  # The stem's node; a leaf, so its name may hold dots.
    audio: Audio
    gain_db: float  # Finite.

    def __post_init__(self) -> None:
        _require("MixInput", "track", self.track, NodeName)
        _require("MixInput", "audio", self.audio, Audio)
        if not is_number(self.gain_db):
            raise ValueError(f"MixInput.gain_db must be a finite number, got {self.gain_db!r}")


@runtime_checkable
class Mixer(Protocol):
    """Mixes stems into one audio."""

    def mix(self, stems: Sequence[MixInput], frames: int) -> Audio:
        """Return the mix of the stems.

        Every stem has `frames` frames, and so does the result. A mix that overflows to a
        non-finite value cannot be an Audio, so the mixer raises ValueError and analysis stops.
        """
        ...


@runtime_checkable
class Measurer(Protocol):
    """Measures loudness and band levels."""

    def measure(self, audio: Audio) -> Levels:
        """Return the audio's levels. Precondition: `audio` is at least TAIL_S long, as every
        stem and mix is."""
        ...


@runtime_checkable
class Visualizer(Protocol):
    """Draws an image of audio."""

    @property
    def suffix(self) -> Suffix:
        """The image file's suffix, such as `.png`."""
        ...

    def draw(self, audio: Audio, path: Path, title: str) -> None:
        """Write an image of the audio to `path`, whose parent exists. Precondition: `audio` is
        at least TAIL_S long, as every stem and mix is."""
        ...


@dataclass(frozen=True)
class SectionInput:
    """What a report section sees for one node or the mix."""

    song: Song | Graph
    node: Node | None  # None for the mix.
    audio: Audio  # The node's stem before gain, or the mix.
    measurements: Measurements

    def __post_init__(self) -> None:
        _require("SectionInput", "song", self.song, (Song, Graph))
        if self.node is not None:
            _require("SectionInput", "node", self.node, Node)
        _require("SectionInput", "audio", self.audio, Audio)
        _require("SectionInput", "measurements", self.measurements, Measurements)


@dataclass(frozen=True)
class SectionResult:
    """What a report section found for one track or the mix."""

    data: Json
    defects: tuple[Identifier, ...] = ()  # Any defect fails the analysis.

    def __post_init__(self) -> None:
        _require("SectionResult", "data", self.data, Json)
        defects = _tuple_of("SectionResult", "defects", self.defects, Identifier)
        object.__setattr__(self, "defects", defects)


@runtime_checkable
class ReportSection(Protocol):
    """A named, versioned addition to every track's and the mix's report."""

    @property
    def name(self) -> Identifier:
        """The key under `sections`."""
        ...

    @property
    def version(self) -> int:
        """Positive; raised when the section's data changes meaning."""
        ...

    @property
    def guide(self) -> str:
        """Describes the data and defects; makes every section Guided."""
        ...

    def track(self, request: SectionInput) -> SectionResult:
        """Analyze one node's stem, before gain."""
        ...

    def mix(self, request: SectionInput) -> SectionResult:
        """Analyze the mix."""
        ...


# The render store


class SongBusyError(Exception):
    """Another render-song or analyze-song is running on the same song."""


@dataclass(frozen=True)
class Record:
    """One kept analysis."""

    run: int  # 1 for the first record, then one above the highest kept.
    path: Path  # The record's directory.

    def __post_init__(self) -> None:
        if not (is_int(self.run) and self.run >= 1):
            raise ValueError(f"Record.run must be a positive integer, got {self.run!r}")
        _require("Record", "path", self.path, Path)


@runtime_checkable
class SongStore(Protocol):
    """Everything kept for one song between and during runs.

    A store keeps every kind of file it writes apart, so that no track name, image suffix, or
    record suffix makes two files share a path.
    """

    def lock(self) -> AbstractContextManager[None]:
        """Held for a whole render or analysis. Raises SongBusyError at once if another run
        holds it. Released when the process dies."""
        ...

    def stem_path(self, node: NodeName) -> Path:
        """Where the node's stem lives. Parent exists on return."""
        ...

    def read_stamp(self, node: NodeName) -> Stamp | StampProblem:
        """The node's stamp, MISSING when there is none or it cannot be read, or CHANGED when
        the stem is no longer the file the stamp recorded."""
        ...

    def write_stamp(self, node: NodeName, stamp: Stamp) -> None:
        """Called right after the stem is written; records that this stem is the one stamped.
        Atomic."""
        ...

    def discard(self, node: NodeName) -> None:
        """Remove the stamp, then the stem."""
        ...

    def clear_latest(self) -> None:
        """Remove the latest report, mix, and images, and nothing else."""
        ...

    def image_path(self, node: NodeName | None, suffix: Suffix) -> Path:
        """A node's latest image, or the mix's for None. Parent exists on return."""
        ...

    def mix_path(self) -> Path:
        """The latest mix. Parent exists on return."""
        ...

    def report_path(self) -> Path:
        """The latest report. Parent exists on return."""
        ...

    def start_record(self, song_text: str, suffix: Suffix) -> Record:
        """Create the next record holding the song text as `song<suffix>`."""
        ...

    def keep_image(self, record: Record, node: NodeName | None, suffix: Suffix) -> Path:
        """Copy image_path(node, suffix) into the record and return the copy's path."""
        ...

    def finish_record(self, record: Record, report_text: str) -> None:
        """Write the record's report."""
        ...


@runtime_checkable
class RenderStore(Protocol):
    """Where songs' results and saved sounds' previews are kept."""

    @property
    def stem_suffix(self) -> Suffix:
        """Given when the store is built; AudioFiles.suffix."""
        ...

    def song(self, song_id: SongId) -> SongStore:
        """The store for one song. No two song ids' stores share a file. Raises ValueError for
        an id whose store would overlap another song's."""
        ...

    def preview_path(self, instrument: Identifier, name: Identifier) -> Path:
        """Where a saved sound's preview goes. Parent exists on return."""
        ...
