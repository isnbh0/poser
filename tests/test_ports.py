"""The request and result types of the ports and use cases check their rules when built."""

from pathlib import Path

import numpy as np
import pytest
from fakes import FakeMeasurer, FakeMixer, FakeSection, FakeVisualizer

from poser.core.analysis import AnalysisTools
from poser.core.audio import Audio
from poser.core.capturing import SaveRequest
from poser.core.checking import CheckResult
from poser.core.checks import CheckStatus
from poser.core.jsonvalue import Json
from poser.core.names import Identifier, TrackName
from poser.core.ports import (
    AdapterContext,
    CaptureFailure,
    MixInput,
    Record,
    RenderFailure,
    ResolvedSound,
    SectionResult,
    SoundFailure,
    SoundListing,
)
from poser.core.rendering import TrackResult
from poser.core.sounds import SoundEntry, SoundRef
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus, StemStatus

FAKE = Identifier("fake")
REF = SoundRef(FAKE)
AUDIO = Audio(np.zeros((2, 100), dtype=np.float32))


def test_types_refuse_what_their_rules_forbid() -> None:
    with pytest.raises(ValueError, match="identity"):
        ResolvedSound(REF, "", Json.of(None))
    with pytest.raises(ValueError, match="detail"):
        SoundFailure(SoundFailureStatus.LOAD_FAILED, "")
    with pytest.raises(ValueError, match="detail"):
        RenderFailure(RenderFailureStatus.CRASHED, "")
    with pytest.raises(ValueError, match="detail"):
        CaptureFailure("")
    with pytest.raises(ValueError, match="gain_db"):
        MixInput(TrackName("lead"), AUDIO, float("inf"))
    with pytest.raises(ValueError, match="defects"):
        SectionResult(Json.of(None), ("not an identifier",))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="no stem"):
        TrackResult(
            TrackName("lead"),
            RenderFailureStatus.CRASHED,
            peak_dbfs=-6.0,
            stem=Path("lead.npy"),
            detail="it crashed",
        )
    with pytest.raises(ValueError, match="needs a stem"):
        TrackResult(TrackName("lead"), StemStatus.RENDERED, peak_dbfs=-6.0)
    with pytest.raises(ValueError, match="description must not be empty"):
        SaveRequest(FAKE, Identifier("pad"), "  ", None, replace=False)
    with pytest.raises(ValueError, match="positive"):
        Record(0, Path("records/0"))
    with pytest.raises(ValueError, match="root"):
        AdapterContext("root", Path("sounds"), Path("renders"))  # ty: ignore[invalid-argument-type]


def test_failure_types_take_only_their_own_status_family() -> None:
    with pytest.raises(ValueError, match="status"):
        SoundFailure(RenderFailureStatus.LOAD_FAILED, "no")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="status"):
        RenderFailure(SoundFailureStatus.LOAD_FAILED, "no")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="status"):
        SoundFailure("load_failed", "no")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="status"):
        RenderFailure("crashed", "no")  # ty: ignore[invalid-argument-type]


def test_collection_fields_are_tuples_that_later_changes_cannot_reach() -> None:
    entries = [SoundEntry(REF, "the default sound")]
    problems = ["broken.fxp could not be read"]
    listing = SoundListing(entries, problems)  # ty: ignore[invalid-argument-type]
    defects = [Identifier("loud")]
    result = SectionResult(Json.of(None), defects)  # ty: ignore[invalid-argument-type]
    sections = [FakeSection("one")]
    tools = AnalysisTools(
        FakeMixer(),
        FakeMeasurer(),
        FakeVisualizer(),
        sections,  # ty: ignore[invalid-argument-type]
    )
    peaks = [-6.0, -7.0]
    check = CheckResult(
        FAKE,
        FAKE,
        CheckStatus.PASS,
        note_peaks_dbfs=peaks,  # ty: ignore[invalid-argument-type]
    )

    entries.append(SoundEntry(SoundRef(FAKE, "pad"), "a pad"))
    problems.clear()
    defects.append(Identifier("quiet"))
    sections.append(FakeSection("two"))
    peaks.append(-8.0)

    assert listing.entries == (SoundEntry(REF, "the default sound"),)
    assert listing.problems == ("broken.fxp could not be read",)
    assert result.defects == (Identifier("loud"),)
    assert [section.name for section in tools.sections] == ["one"]
    assert check.note_peaks_dbfs == (-6.0, -7.0)
    for value in (listing.entries, listing.problems, result.defects, tools.sections):
        assert isinstance(value, tuple)
    assert isinstance(check.note_peaks_dbfs, tuple)


def test_section_result_holds_json_data_only() -> None:
    result = SectionResult(Json.of({"a": [1]}), (Identifier("loud"),))
    assert result.data.value() == {"a": [1]}
    with pytest.raises(ValueError, match="not a JSON value"):
        SectionResult(Json.of({"a": float("nan")}))
    with pytest.raises(ValueError, match="data"):
        SectionResult({"a": [1]})  # ty: ignore[invalid-argument-type]
