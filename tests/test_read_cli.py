"""`read-song`: marks and readings in text and JSON, exit codes, and no instrument built."""

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


def write(path: Path, *tracks: dict[str, object], marks: object = None) -> Path:
    data: dict[str, object] = {"bpm": 60, "tracks": list(tracks)}
    if marks is not None:
        data["marks"] = marks
    path.write_text(yaml.safe_dump(data, sort_keys=False))
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
    assert PRESETS["neutral"]["frameworks"] == ()
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


# Marks

CHORDS = [
    {"beat": 0, "end": 4, "text": "Am"},
    {"beat": 4, "end": 8, "text": "F/A"},
    {"beat": 24, "end": 32, "text": "G7"},
]
PARTS = [{"beat": 0, "end": 16, "text": "opening"}, {"beat": 16, "end": 27.5, "text": "lift"}]


def marked(workspace: CommandWorkspace, chords: object = CHORDS) -> Path:
    raised = [(beat + 16, pitch + 6, length) for beat, pitch, length in CADENCE]
    return write(
        workspace.root / "marked.yaml",
        track("keys", CADENCE + raised),
        track("bass", [(0, 48, 2), (4, 43, 2), (16, 54, 2), (20, 49, 2)]),
        marks={"chords": chords, "parts": PARTS},
    )


TONAL_TEXT = (
    "tonal (tracks: keys, bass)\n  beats 0-16: C major (r 0.90)\n  beats 16-27: F# major (r 0.90)\n"
)


# Exactly what `read-song --json` printed for the `song` fixture before songs had marks.
TONAL_JSON = {
    "end": 27,
    "tracks": ["keys", "bass"],
    "frameworks": {
        "tonal": {
            "spans": [
                {
                    "start": 0,
                    "end": 16,
                    "outcome": "known",
                    "value": {
                        "key": "C major",
                        "tonic": 0,
                        "mode": "major",
                        "correlation": 0.903,
                        "deviation_cents": 0,
                    },
                },
                {
                    "start": 16,
                    "end": 27,
                    "outcome": "known",
                    "value": {
                        "key": "F# major",
                        "tonic": 6,
                        "mode": "major",
                        "correlation": 0.903,
                        "deviation_cents": 0,
                    },
                },
            ]
        }
    },
}


def test_marks_print_above_the_readings(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert read.main([str(marked(workspace))]) == 0

    assert capsys.readouterr().out == (
        "chords marks (chords vocabulary)\n"
        "  beats 0-4: Am\n"
        "  beats 4-8: F/A\n"
        "  beats 24-27: G7\n"
        "parts marks (text)\n"
        "  beats 0-16: opening\n"
        "  beats 16-27: lift\n" + TONAL_TEXT
    )


def test_options_do_not_filter_marks(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert read.main([str(marked(workspace)), "--track", "keys", "--framework", "tonal"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("chords marks (chords vocabulary)\n")
    assert "parts marks (text)\n" in out


def test_json_adds_the_marks(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert read.main([str(marked(workspace)), "--json"]) == 0

    data = json_object(json.loads(capsys.readouterr().out))
    assert data is not None
    assert list(data) == ["end", "tracks", "frameworks", "marks"]
    assert data["marks"] == {
        "chords": {
            "vocabulary": "chords",
            "marks": [
                {
                    "beat": 0,
                    "end": 4,
                    "text": "Am",
                    "value": {"chord": "Am", "root": 9, "quality": "min", "bass": 9},
                },
                {
                    "beat": 4,
                    "end": 8,
                    "text": "F/A",
                    "value": {"chord": "F/A", "root": 5, "quality": "maj", "bass": 9},
                },
                {
                    "beat": 24,
                    "end": 27,
                    "text": "G7",
                    "value": {"chord": "G7", "root": 7, "quality": "7", "bass": 7},
                },
            ],
        },
        "parts": {
            "vocabulary": None,
            "marks": [
                {"beat": 0, "end": 16, "text": "opening"},
                {"beat": 16, "end": 27, "text": "lift"},
            ],
        },
    }


def test_a_rejected_label_exits_2_naming_the_mark(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    song = marked(workspace, [{"beat": 0, "end": 4, "text": "Amj7"}])

    assert read.main([str(song)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "marks.chords[0]" in captured.err
    assert "'Amj7'" in captured.err


def keyed(workspace: CommandWorkspace, text: str) -> Path:
    raised = [(beat + 16, pitch + 6, length) for beat, pitch, length in CADENCE]
    return write(
        workspace.root / "keyed.yaml",
        track("keys", CADENCE + raised),
        track("bass", [(0, 48, 2), (4, 43, 2), (16, 54, 2), (20, 49, 2)]),
        marks={"key": [{"beat": 0, "end": 64, "text": text}]},
    )


def test_a_stated_key_prints_above_the_inferred_key(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    song = keyed(workspace, "A minor")

    assert read.main([str(song)]) == 0
    assert capsys.readouterr().out == (
        "key marks (key vocabulary)\n  beats 0-27: A minor\n" + TONAL_TEXT
    )

    assert read.main([str(song), "--json"]) == 0
    data = json_object(json.loads(capsys.readouterr().out))
    assert data is not None
    assert data["marks"] == {
        "key": {
            "vocabulary": "key",
            "marks": [
                {
                    "beat": 0,
                    "end": 27,
                    "text": "A minor",
                    "value": {"key": "A minor", "tonic": 9, "mode": "minor"},
                }
            ],
        }
    }


def test_a_misspelled_key_exits_2_naming_the_mark(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert read.main([str(keyed(workspace, "A mnior"))]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "marks.key[0]" in captured.err
    assert "'A mnior'" in captured.err


def test_a_song_without_marks_prints_what_it_always_did(
    song: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert read.main([str(song)]) == 0
    assert capsys.readouterr().out == TONAL_TEXT
    assert read.main([str(song), "--json"]) == 0
    assert capsys.readouterr().out == json.dumps(TONAL_JSON, indent=2) + "\n"


def test_the_neutral_vocabularies_show_chords_as_text(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.write_config(
        workspace.config.read_text()
        + '[adapters]\nframeworks = "neutral"\nvocabularies = "neutral"\n'
    )
    song = marked(workspace, [{"beat": 0, "end": 4, "text": "Amj7"}])

    assert read.main([str(song)]) == 0

    assert capsys.readouterr().out == (
        "chords marks (text)\n"
        "  beats 0-4: Amj7\n"
        "parts marks (text)\n"
        "  beats 0-16: opening\n"
        "  beats 16-27: lift\n"
        "no frameworks are configured (preset neutral)\n"
    )


def test_a_failing_vocabulary_exits_1(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace.write_config(
        workspace.config.read_text()
        + '[adapters]\nvocabularies = ["{module}:raising_vocabulary"]\n'
    )

    assert read.main([str(marked(workspace))]) == 1

    out = capsys.readouterr().out
    assert out.startswith("chords marks: error: RuntimeError: cannot parse\nparts marks (text)\n")
    assert out.endswith(TONAL_TEXT)
