"""Checking: whether an instrument's default sound loads, plays every note, and renders the same
way twice."""

import tempfile
from dataclasses import dataclass
from pathlib import Path

from poser.core.audio import Audio, AudioFault, AudioProblem, read_raw_audio
from poser.core.checks import (
    CHECK_SAMPLES,
    CheckAnalysis,
    CheckFailure,
    CheckFailureStatus,
    CheckStatus,
    analyze_check,
    check_pattern,
    check_status,
)
from poser.core.names import Identifier
from poser.core.ports import Instrument, RenderFailure, RenderRequest, SoundFailure
from poser.core.sounds import SoundRef


@dataclass(frozen=True)
class CheckResult:
    """One instrument's check: its measured renders, or why it has none."""

    name: Identifier
    adapter: Identifier
    outcome: CheckAnalysis | CheckFailure

    def __post_init__(self) -> None:
        for field in ("name", "adapter"):
            value = getattr(self, field)
            if not isinstance(value, Identifier):
                raise ValueError(f"CheckResult.{field} must be an Identifier, got {value!r}")
        if not isinstance(self.outcome, CheckAnalysis | CheckFailure):
            raise ValueError(f"CheckResult.outcome must be a check outcome, got {self.outcome!r}")

    @property
    def status(self) -> CheckStatus:
        return self.outcome.status


def check_instrument(instrument: Instrument, name: Identifier, timeout_s: float) -> CheckResult:
    """Render the check pattern twice on the instrument's default sound and judge the renders."""
    adapter = instrument.adapter_id
    sound = instrument.resolve(SoundRef(name, None))
    if isinstance(sound, SoundFailure):
        detail = f"default sound did not resolve: {sound.detail}"
        return CheckResult(name, adapter, CheckFailure(check_status(sound.status), detail))

    renders: list[Audio] = []
    with tempfile.TemporaryDirectory() as directory:
        for index in range(2):
            path = Path(directory) / f"check-{index + 1}.npy"
            failure = instrument.render(RenderRequest(sound, check_pattern(), path), timeout_s)
            if isinstance(failure, RenderFailure):
                outcome = CheckFailure(check_status(failure.status), failure.detail)
                return CheckResult(name, adapter, outcome)
            audio = read_raw_audio(path, CHECK_SAMPLES)
            if isinstance(audio, AudioProblem):
                if audio.fault is AudioFault.NON_FINITE:
                    outcome = CheckFailure(CheckFailureStatus.NON_FINITE, audio.message)
                else:
                    detail = f"instrument wrote unusable audio: {audio.message}"
                    outcome = CheckFailure(CheckFailureStatus.CRASHED, detail)
                return CheckResult(name, adapter, outcome)
            renders.append(audio)

    first, second = renders
    return CheckResult(name, adapter, analyze_check(first, second))
