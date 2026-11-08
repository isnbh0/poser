"""Saving a sound the owner shaped, step by step, on fakes."""

from pathlib import Path
from typing import override

import numpy as np
import pytest
from fakes import FakeInstrument, FakeRenderStore, FakeSound, NpyAudioFiles, PlainInstrument

from poser.core.audio import Audio
from poser.core.capturing import (
    NotSaved,
    PreviewFailed,
    Refused,
    Saved,
    SaveRequest,
    SaveResult,
    save_sound,
)
from poser.core.checks import CHECK_SAMPLES, check_pattern
from poser.core.names import Identifier
from poser.core.ports import CaptureFailure, Instrument, ResolvedSound, SoundFailure
from poser.core.sounds import SoundRef
from poser.core.statuses import SoundFailureStatus

FAKE = Identifier("fake")
NAME = Identifier("bright")
REF = SoundRef(FAKE, NAME)


class FailingAudioFiles(NpyAudioFiles):
    """Audio files whose write raises, after checking that the sound was already stored."""

    def __init__(self, instrument: FakeInstrument, error: BaseException) -> None:
        self.instrument = instrument
        self.error = error

    @override
    def write(self, path: Path, audio: Audio) -> None:
        assert self.instrument.stores == [NAME]
        raise self.error


class OrderedAudioFiles(NpyAudioFiles):
    """Audio files that record what had been stored when each write happened."""

    def __init__(self, instrument: FakeInstrument) -> None:
        self.instrument = instrument
        self.stored_before: list[list[Identifier]] = []

    @override
    def write(self, path: Path, audio: Audio) -> None:
        self.stored_before.append(list(self.instrument.stores))
        super().write(path, audio)


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        failure = SoundFailure(SoundFailureStatus.LOAD_FAILED, "the preset is corrupt")
        self.instrument = FakeInstrument(
            FAKE,
            {
                None: FakeSound(),
                "pad": FakeSound("a pad", "pad", 0.2),
                "broken": FakeSound(sound_failure=failure),
            },
        )
        self.instruments: dict[Identifier, Instrument] = {FAKE: self.instrument}
        self.renders = FakeRenderStore(tmp_path / "renders")
        self.audio_files: NpyAudioFiles = NpyAudioFiles()

    def save(
        self,
        instrument: str = FAKE,
        start: SoundRef | None = None,
        *,
        replace: bool = False,
    ) -> SaveResult:
        request = SaveRequest(Identifier(instrument), NAME, "a bright lead", start, replace=replace)
        return save_sound(request, self.instruments, self.renders, self.audio_files)

    @property
    def preview(self) -> Path:
        return self.renders.preview_path(FAKE, NAME)


def test_unknown_instrument_is_refused(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    assert setup.save("other") == Refused("no instrument 'other' in poser.toml")
    assert setup.instrument.captures == []


def test_instrument_without_capture_is_refused(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.instruments[FAKE] = PlainInstrument(setup.instrument)
    assert setup.save() == Refused("instrument 'fake' cannot save sounds")
    assert setup.instrument.captures == []


def test_taken_name_is_refused_unless_replacing(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    assert isinstance(setup.save(), Saved)

    assert setup.save() == Refused("a sound named 'bright' already exists")
    assert len(setup.instrument.captures) == 1
    assert isinstance(setup.save(replace=True), Saved)


def test_taken_name_is_refused_before_the_start_is_looked_at(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    assert isinstance(setup.save(), Saved)
    setup.instrument.resolves.clear()

    for start in (SoundRef(Identifier("other"), "pad"), SoundRef(FAKE, "broken")):
        assert setup.save(start=start) == Refused("a sound named 'bright' already exists")
    assert setup.instrument.resolves == []
    assert len(setup.instrument.captures) == 1


def test_start_of_another_instrument_is_refused(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    result = setup.save(start=SoundRef(Identifier("other"), "pad"))
    assert result == Refused("--from must name a sound of 'fake'")
    assert setup.instrument.captures == []


def test_start_that_does_not_resolve_is_refused(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    assert setup.save(start=SoundRef(FAKE, "broken")) == Refused("the preset is corrupt")
    assert setup.save(start=SoundRef(FAKE, "gone")) == Refused("no sound named 'gone'")
    assert setup.instrument.captures == []


def test_start_reaches_capture_as_the_resolved_sound(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    start = SoundRef(FAKE, "pad")

    assert isinstance(setup.save(start=start), Saved)

    (capture,) = setup.instrument.captures
    assert capture.start == setup.instrument.resolve(start)
    assert capture.name == NAME
    assert capture.preview == check_pattern()
    assert not capture.preview_path.exists()


def test_capture_failure_is_not_saved_and_never_stores(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.instrument.capture_failure = CaptureFailure("the editor did not open")

    assert setup.save() == NotSaved("the editor did not open")
    assert setup.instrument.stores == []


def test_unusable_preview_is_not_saved_and_never_stores(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.instrument.capture_bad_output = "wrong_length"

    result = setup.save()

    assert result == NotSaved(
        f"instrument wrote an unusable preview: {CHECK_SAMPLES - 1} samples, "
        f"expected {CHECK_SAMPLES}"
    )
    assert setup.instrument.stores == []


def test_store_problem_is_not_saved_and_writes_no_preview(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.instrument.store_problem = "a sound named 'bright' was saved meanwhile"

    assert setup.save() == NotSaved("a sound named 'bright' was saved meanwhile")
    assert not setup.preview.exists()


def test_store_raising_os_error_is_not_saved_and_writes_no_preview(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.instrument.store_raises = OSError("disk full")

    assert setup.save() == NotSaved("disk full")
    assert not setup.preview.exists()


def test_failed_preview_write_names_the_saved_ref(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.audio_files = FailingAudioFiles(setup.instrument, OSError("disk full"))

    assert setup.save() == PreviewFailed(REF, "disk full")
    assert isinstance(setup.instrument.resolve(REF), ResolvedSound)


def test_saved_sound_stores_then_writes_its_preview(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    ordered = OrderedAudioFiles(setup.instrument)
    setup.audio_files = ordered

    result = setup.save()

    assert isinstance(result, Saved)
    assert (result.ref, result.preview) == (REF, setup.preview)
    assert result.peak_dbfs == pytest.approx(20 * np.log10(0.5))
    assert ordered.stored_before == [[NAME]]
    preview = NpyAudioFiles().read(setup.preview, CHECK_SAMPLES)
    assert np.allclose(preview.samples, 0.5)
    assert isinstance(setup.instrument.resolve(REF), ResolvedSound)


def test_interrupt_in_capture_propagates_and_writes_nothing(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.instrument.capture_raises = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        setup.save()

    assert setup.instrument.stores == []
    assert not setup.preview.exists()


def test_interrupt_in_store_propagates_and_writes_no_preview(tmp_path: Path) -> None:
    setup = Setup(tmp_path)
    setup.instrument.store_raises = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        setup.save()

    assert not setup.preview.exists()


def test_interrupt_after_store_is_a_preview_failure_and_the_sound_resolves(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path)
    setup.audio_files = FailingAudioFiles(setup.instrument, KeyboardInterrupt())

    assert setup.save() == PreviewFailed(REF, "interrupted")
    assert isinstance(setup.instrument.resolve(REF), ResolvedSound)
