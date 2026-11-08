"""Checking: whether an instrument's default sound loads, plays every note, and renders the same
way twice."""

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from poser.core.audio import Audio, AudioFault, AudioProblem, read_raw_audio
from poser.core.checks import CHECK_SAMPLES, CheckStatus, analyze_check, check_pattern, check_status
from poser.core.names import Identifier
from poser.core.ports import Instrument, RenderFailure, RenderRequest, SoundFailure
from poser.core.song import is_number
from poser.core.sounds import SoundRef


def _optional_number(field: str, value: object) -> None:
    if not (value is None or is_number(value)):
        raise ValueError(f"CheckResult.{field} must be None or a finite number, got {value!r}")


def _optional_bool(field: str, value: object) -> None:
    if not (value is None or isinstance(value, bool)):
        raise ValueError(f"CheckResult.{field} must be None or a bool, got {value!r}")


@dataclass(frozen=True)
class CheckResult:
    """One instrument's check."""

    name: Identifier
    adapter: Identifier
    status: CheckStatus
    peak_dbfs: float | None = None
    note_peaks_dbfs: tuple[float, ...] | None = None
    clipped: bool | None = None
    repeatable: bool | None = None
    max_difference_dbfs: float | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        for field in ("name", "adapter"):
            value = getattr(self, field)
            if not isinstance(value, Identifier):
                raise ValueError(f"CheckResult.{field} must be an Identifier, got {value!r}")
        if not isinstance(self.status, CheckStatus):
            raise ValueError(f"CheckResult.status must be a CheckStatus, got {self.status!r}")
        _optional_number("peak_dbfs", self.peak_dbfs)
        _optional_number("max_difference_dbfs", self.max_difference_dbfs)
        _optional_bool("clipped", self.clipped)
        _optional_bool("repeatable", self.repeatable)
        if self.note_peaks_dbfs is not None:
            peaks = self.note_peaks_dbfs
            if not isinstance(peaks, Sequence) or isinstance(peaks, str):
                raise ValueError(f"CheckResult.note_peaks_dbfs must be a sequence, got {peaks!r}")
            peaks = tuple(cast("Sequence[object]", peaks))
            if not all(is_number(peak) for peak in peaks):
                raise ValueError(f"CheckResult.note_peaks_dbfs must hold finite numbers: {peaks}")
            object.__setattr__(self, "note_peaks_dbfs", peaks)
        if not (self.detail is None or (isinstance(self.detail, str) and self.detail)):
            raise ValueError(f"CheckResult.detail must be None or non-empty, got {self.detail!r}")


def check_instrument(instrument: Instrument, name: Identifier, timeout_s: float) -> CheckResult:
    """Render the check pattern twice on the instrument's default sound and judge the renders."""
    adapter = instrument.adapter_id
    sound = instrument.resolve(SoundRef(name, None))
    if isinstance(sound, SoundFailure):
        return CheckResult(
            name,
            adapter,
            check_status(sound.status),
            detail=f"default sound did not resolve: {sound.detail}",
        )

    renders: list[Audio] = []
    with tempfile.TemporaryDirectory() as directory:
        for index in range(2):
            path = Path(directory) / f"check-{index + 1}.npy"
            failure = instrument.render(RenderRequest(sound, check_pattern(), path), timeout_s)
            if isinstance(failure, RenderFailure):
                return CheckResult(
                    name, adapter, check_status(failure.status), detail=failure.detail
                )
            audio = read_raw_audio(path, CHECK_SAMPLES)
            if isinstance(audio, AudioProblem):
                if audio.fault is AudioFault.NON_FINITE:
                    return CheckResult(name, adapter, CheckStatus.NON_FINITE, detail=audio.message)
                return CheckResult(
                    name,
                    adapter,
                    CheckStatus.CRASHED,
                    detail=f"instrument wrote unusable audio: {audio.message}",
                )
            renders.append(audio)

    first, second = renders
    analysis = analyze_check(first, second)
    return CheckResult(
        name,
        adapter,
        analysis.status,
        peak_dbfs=analysis.peak_dbfs,
        note_peaks_dbfs=analysis.note_peaks_dbfs,
        clipped=analysis.clipped,
        repeatable=analysis.repeatable,
        max_difference_dbfs=analysis.max_difference_dbfs,
    )
