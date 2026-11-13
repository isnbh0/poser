"""`read-song`: readings in text and JSON, exit codes, and no instrument built."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from workspaces import CommandWorkspace, make_workspace

from poser.cli import read
from poser.compose import PRESETS
from poser.core.jsonvalue import json_object
from poser.core.reading import OUTCOME_TAGS

CADENCE = [(i, p, 1) for i, p in enumerate([60, 62, 64, 65, 67, 69, 71])] + [(7, 72, 4)]


def track(
    name: str, notes: Sequence[tuple[float, float, float]], **keys: object
) -> dict[str, object]:
    return {
        "name": name,
        "sound": "synth",
        "notes": [{"beat": b, "pitch": p, "length": n, "velocity": 100} for b, p, n in notes],
        **keys,
    }


def write(path: Path, *tracks: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump({"bpm": 60, "tracks": list(tracks)}))
    return path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CommandWorkspace:
    made = make_workspace(tmp_path / "studio", monkeypatch)
    monkeypatch.chdir(made.root)
    return made


@pytest.fixture
def song(workspace: CommandWorkspace) -> Path:
    raised = [(beat + 16, pitch + 6, length) for beat, pitch, length in CADENCE]
    drums = [(0, 36, 1), (1, 38, 1)]
    return write(
        workspace.root / "song.yaml",
        track("keys", CADENCE + raised),
        track("bass", [(0, 48, 2), (4, 43, 2), (16, 54, 2), (20, 49, 2)]),
        track("drums", drums, pitched=False),
    )


def test_reading_builds_no_instrument_and_writes_nothing(
    workspace: CommandWorkspace, song: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = sorted(workspace.root.rglob("*"))

    assert read.main([str(song)]) == 0

    assert not workspace.module.BUILT
    assert sorted(workspace.root.rglob("*")) == before
    assert capsys.readouterr().out == (
        "tonal (tracks: keys, bass)\n"
        "  beats 0-16: C major (r 0.90)\n"
        "  beats 16-27: F# major (r 0.90)\n"
    )


def test_json_has_the_readings_shape(song: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert read.main([str(song), "--json", "--track", "keys"]) == 0

    data = json_object(json.loads(capsys.readouterr().out))
    assert data is not None
    assert data["end"] == 27
    assert data["tracks"] == ["keys"]
    frameworks = json_object(data["frameworks"])
    assert frameworks is not None
    assert list(frameworks) == ["tonal"]
    tonal = json_object(frameworks["tonal"])
    assert tonal is not None
    spans = tonal["spans"]
    assert isinstance(spans, list)
    assert [(span["start"], span["end"]) for span in spans] == [(0, 16), (16, 27)]
    for span in spans:
        assert span["outcome"] in OUTCOME_TAGS
    assert spans[0]["value"] == {
        "key": "C major",
        "tonic": 0,
        "mode": "major",
        "correlation": spans[0]["value"]["correlation"],
        "deviation_cents": 0,
    }
    assert 0.85 <= spans[0]["value"]["correlation"] <= 1


def test_a_failing_framework_exits_1(
    workspace: CommandWorkspace, song: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.write_config(
        workspace.config.read_text()
        + '[adapters]\nframeworks = ["tonal", "{module}:raising_framework"]\n'
    )

    assert read.main([str(song)]) == 1

    out = capsys.readouterr().out
    assert "tonal (tracks: keys, bass)\n" in out
    assert out.endswith("broken: error: RuntimeError: cannot read\n")


@pytest.mark.parametrize(
    ("args", "named"),
    [
        (["--track", "lead"], "'lead'"),
        (["--track", "drums"], "track 'drums' is unpitched"),
        (["--framework", "modal"], "no framework 'modal' is configured"),
    ],
    ids=["unknown-track", "unpitched-track", "unknown-framework"],
)
def test_an_unknown_name_exits_2(
    song: Path, capsys: pytest.CaptureFixture[str], args: list[str], named: str
) -> None:
    assert read.main([str(song), *args]) == 2

    assert named in capsys.readouterr().err


def test_an_unreadable_song_exits_2(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = workspace.root / "broken.yaml"
    broken.write_text("bpm: 60\ntracks: 3\n")

    assert read.main([str(broken)]) == 2
    assert "tracks" in capsys.readouterr().err
    assert read.main([str(workspace.root / "missing.yaml")]) == 2
    assert "missing.yaml" in capsys.readouterr().err


def test_the_neutral_preset_reads_nothing(
    workspace: CommandWorkspace, song: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert PRESETS["neutral"] == ()
    workspace.write_config(workspace.config.read_text() + '[adapters]\nframeworks = "neutral"\n')

    assert read.main([str(song)]) == 0

    assert capsys.readouterr().out == "no frameworks are configured (preset neutral)\n"


def test_a_song_of_unpitched_tracks_reads_no_track(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    drums = write(workspace.root / "drums.yaml", track("drums", [(0, 36, 1)], pitched=False))

    assert read.main([str(drums), "--config", str(workspace.config)]) == 0

    assert capsys.readouterr().out == (
        "tonal (tracks: none; every track is unpitched)\n"
        "  beats 0-1: unknowable: no selected notes sound\n"
    )


def test_span_boundaries_print_without_float_noise() -> None:
    assert read._beat(3 * 1.1) == "3.3"
    assert read._beat(16.0) == "16"
