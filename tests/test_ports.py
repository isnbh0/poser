"""The request and result types of the ports and use cases check their rules when built."""

from pathlib import Path

import numpy as np
import pytest

from poser.core.audio import Audio
from poser.core.capturing import SaveRequest
from poser.core.checking import CheckResult
from poser.core.checks import CheckStatus
from poser.core.graph import Wire
from poser.core.jsonvalue import Json
from poser.core.names import Identifier, NodeName, TrackName
from poser.core.ports import (
    AdapterContext,
    CaptureFailure,
    DelayMember,
    EffectMember,
    EffectRenderRequest,
    GroupRenderRequest,
    MixInput,
    RenderFailure,
    ResolvedEffect,
    ResolvedSound,
    SoundFailure,
    SoundListing,
)
from poser.core.rendering import TrackResult
from poser.core.sounds import SoundEntry, SoundRef
from poser.core.statuses import (
    CORE_RENDER_FAILURES,
    RenderFailureStatus,
    SoundFailureStatus,
    StemStatus,
)

FAKE = Identifier("fake")
REF = SoundRef(FAKE)
AUDIO = Audio(np.zeros((2, 100), dtype=np.float32))
RECIPE = ResolvedEffect("drive", "a drive recipe", Json.of({"gain_db": 6}))


def test_types_refuse_what_their_rules_forbid() -> None:
    with pytest.raises(ValueError, match="identity"):
        ResolvedSound(REF, "", Json.of(None))
    with pytest.raises(ValueError, match="detail"):
        SoundFailure(SoundFailureStatus.LOAD_FAILED, "")
    with pytest.raises(ValueError, match="notes"):
        SoundListing(
            (SoundEntry(REF, "the default sound"),),
            (),
            (0,),  # ty: ignore[invalid-argument-type]
        )
    with pytest.raises(ValueError, match="detail"):
        RenderFailure(RenderFailureStatus.CRASHED, "")
    with pytest.raises(ValueError, match="detail"):
        CaptureFailure("")
    with pytest.raises(ValueError, match="gain_db"):
        MixInput(TrackName("lead"), AUDIO, float("inf"))
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


@pytest.mark.parametrize("status", sorted(CORE_RENDER_FAILURES))
def test_an_adapter_failure_cannot_carry_a_status_the_core_decides(
    status: RenderFailureStatus,
) -> None:
    with pytest.raises(ValueError, match="decided by the core"):
        RenderFailure(status, "no")


def test_collection_fields_are_tuples_that_later_changes_cannot_reach() -> None:
    entries = [SoundEntry(REF, "the default sound")]
    problems = ["broken.fxp could not be read"]
    notes = ["presets hold 1 .txt poser cannot read"]
    listing = SoundListing(entries, problems, notes)  # ty: ignore[invalid-argument-type]
    peaks = [-6.0, -7.0]
    check = CheckResult(
        FAKE,
        FAKE,
        CheckStatus.PASS,
        note_peaks_dbfs=peaks,  # ty: ignore[invalid-argument-type]
    )

    entries.append(SoundEntry(SoundRef(FAKE, "pad"), "a pad"))
    problems.clear()
    notes.append("more strays appeared")
    peaks.append(-8.0)

    assert listing.entries == (SoundEntry(REF, "the default sound"),)
    assert listing.problems == ("broken.fxp could not be read",)
    assert listing.notes == ("presets hold 1 .txt poser cannot read",)
    assert check.note_peaks_dbfs == (-6.0, -7.0)
    for value in (
        listing.entries,
        listing.problems,
        listing.notes,
    ):
        assert isinstance(value, tuple)
    assert isinstance(check.note_peaks_dbfs, tuple)


def test_effect_types_refuse_what_their_rules_forbid() -> None:
    with pytest.raises(ValueError, match="recipe"):
        ResolvedEffect("", "identity", Json.of(None))
    with pytest.raises(ValueError, match="identity"):
        ResolvedEffect("drive", "", Json.of(None))
    with pytest.raises(ValueError, match="payload"):
        ResolvedEffect("drive", "identity", {"gain_db": 6})  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="audio"):
        EffectRenderRequest(np.zeros((2, 10)), RECIPE, Path("out.npy"))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="recipe"):
        EffectRenderRequest(AUDIO, "drive", Path("out.npy"))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="audio_path"):
        EffectRenderRequest(AUDIO, RECIPE, "out.npy")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="frames"):
        EffectRenderRequest(
            AUDIO, RECIPE, Path("out.npy"), control=Audio(np.zeros((2, 99), dtype=np.float32))
        )
    request = EffectRenderRequest(AUDIO, RECIPE, Path("out.npy"), control=AUDIO)
    assert request.control is not None
    assert request.control.frames == request.audio.frames


def test_cycle_members_hold_only_their_own_kinds_payload() -> None:
    name = NodeName("loop")
    assert EffectMember(name, RECIPE, AUDIO).recipe == RECIPE
    assert DelayMember(name, 512, AUDIO).delay_samples == 512
    with pytest.raises(ValueError, match="recipe"):
        EffectMember(name, "drive", AUDIO)  # ty: ignore[invalid-argument-type]
    for samples in (512.0, "512", True, 0):
        with pytest.raises(ValueError, match="delay_samples"):
            DelayMember(name, samples, AUDIO)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError):
        EffectMember(name, RECIPE, AUDIO, delay_samples=512)  # ty: ignore[unknown-argument]
    with pytest.raises(TypeError):
        DelayMember(name, 512, AUDIO, recipe=RECIPE)  # ty: ignore[unknown-argument]


def test_a_group_request_holds_only_cycle_members(tmp_path: Path) -> None:
    members = (
        EffectMember(NodeName("loop"), RECIPE, AUDIO),
        DelayMember(NodeName("late"), 512, AUDIO),
    )
    outputs = tuple((member.name, tmp_path / f"{member.name}.npy") for member in members)
    wires = (Wire("loop", "late", "audio"), Wire("late", "loop", "audio"))
    request = GroupRenderRequest(AUDIO.frames, members, wires, outputs)
    assert request.members == members
    with pytest.raises(ValueError, match="EffectMember or DelayMember"):
        GroupRenderRequest(AUDIO.frames, (*members, "late"), wires, outputs)  # ty: ignore[invalid-argument-type]
