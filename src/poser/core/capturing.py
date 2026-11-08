"""Capturing: letting the owner shape a sound on an instrument, saving it, and writing its
preview.

`SoundCapture.store` returning a ref is the commit. Before it, nothing is saved, and an interrupt
propagates; after it, the sound is saved whatever happens to the preview.
"""

import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from poser.core.audio import AudioProblem, measure_peak_dbfs, read_raw_audio
from poser.core.checks import CHECK_SAMPLES, check_pattern
from poser.core.names import Identifier
from poser.core.ports import (
    AudioFiles,
    CaptureFailure,
    CaptureRequest,
    Instrument,
    RenderStore,
    ResolvedSound,
    SoundCapture,
    SoundFailure,
)
from poser.core.song import is_number
from poser.core.sounds import SoundRef


@dataclass(frozen=True)
class SaveRequest:
    """A request to shape and save a new sound."""

    instrument: Identifier
    name: Identifier
    description: str  # Non-empty.
    start: SoundRef | None  # Must name a sound of `instrument`.
    replace: bool

    def __post_init__(self) -> None:
        for field in ("instrument", "name"):
            value = getattr(self, field)
            if not isinstance(value, Identifier):
                raise ValueError(f"SaveRequest.{field} must be an Identifier, got {value!r}")
        if not (isinstance(self.description, str) and self.description.strip()):
            raise ValueError("the description must not be empty")
        if not (self.start is None or isinstance(self.start, SoundRef)):
            raise ValueError(f"SaveRequest.start must be a SoundRef or None, got {self.start!r}")
        if not isinstance(self.replace, bool):
            raise ValueError(f"SaveRequest.replace must be a bool, got {self.replace!r}")


def _problem(owner: str, value: object) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{owner} needs text, got {value!r}")


@dataclass(frozen=True)
class Refused:
    """Nothing was opened or saved; the command exits 2."""

    problem: str

    def __post_init__(self) -> None:
        _problem("Refused.problem", self.problem)


@dataclass(frozen=True)
class NotSaved:
    """The owner's work was not saved; the command exits 1."""

    problem: str

    def __post_init__(self) -> None:
        _problem("NotSaved.problem", self.problem)


@dataclass(frozen=True)
class Saved:
    """The sound was saved and its preview written."""

    ref: SoundRef
    preview: Path
    peak_dbfs: float

    def __post_init__(self) -> None:
        if not isinstance(self.ref, SoundRef):
            raise ValueError(f"Saved.ref must be a SoundRef, got {self.ref!r}")
        if not isinstance(self.preview, Path):
            raise ValueError(f"Saved.preview must be a Path, got {self.preview!r}")
        if not is_number(self.peak_dbfs):
            raise ValueError(f"Saved.peak_dbfs must be a finite number, got {self.peak_dbfs!r}")


@dataclass(frozen=True)
class PreviewFailed:
    """The sound was saved, but its preview was not written; the command exits 1."""

    ref: SoundRef
    error: str

    def __post_init__(self) -> None:
        if not isinstance(self.ref, SoundRef):
            raise ValueError(f"PreviewFailed.ref must be a SoundRef, got {self.ref!r}")
        _problem("PreviewFailed.error", self.error)


type SaveResult = Refused | NotSaved | Saved | PreviewFailed


def save_sound(
    request: SaveRequest,
    instruments: Mapping[Identifier, Instrument],
    renders: RenderStore,
    audio_files: AudioFiles,
) -> SaveResult:
    """Let the owner shape a sound, save it, and write its preview.

    Has no timeout: the owner shapes the sound by hand. A KeyboardInterrupt before the sound is
    stored propagates; one after is reported as a preview failure, since the sound is saved.
    """
    name = request.instrument
    instrument = instruments.get(name)
    if instrument is None:
        return Refused(f"no instrument {name!r} in poser.toml")
    if not isinstance(instrument, SoundCapture):
        return Refused(f"instrument {name!r} cannot save sounds")
    problem = instrument.check_name(request.name, replace=request.replace)
    if problem is not None:
        return Refused(problem)

    start: ResolvedSound | None = None
    if request.start is not None:
        if request.start.instrument != name:
            return Refused(f"--from must name a sound of {name!r}")
        resolved = instrument.resolve(request.start)
        if isinstance(resolved, SoundFailure):
            return Refused(resolved.detail)
        start = resolved

    with tempfile.TemporaryDirectory() as directory:
        capture = CaptureRequest(
            request.name, start, check_pattern(), Path(directory) / "preview.npy"
        )
        captured = instrument.capture(capture)
        if isinstance(captured, CaptureFailure):
            return NotSaved(captured.detail)
        preview = read_raw_audio(capture.preview_path, CHECK_SAMPLES)
    if isinstance(preview, AudioProblem):
        return NotSaved(f"instrument wrote an unusable preview: {preview.message}")

    # Nothing is stored before this step, so a failed run leaves no new sound.
    try:
        stored = instrument.store(
            request.name, request.description, captured, replace=request.replace
        )
    except OSError as error:
        return NotSaved(str(error))
    if isinstance(stored, str):
        return NotSaved(stored)

    # The sound is saved from here on, so nothing below reports it as not saved.
    ref = stored
    try:
        path = renders.preview_path(name, request.name)
        audio_files.write(path, preview)
        saved = Saved(ref, path, measure_peak_dbfs(preview.samples))
    except OSError as error:
        return PreviewFailed(ref, str(error))
    except KeyboardInterrupt:
        return PreviewFailed(ref, "interrupted")
    return saved
