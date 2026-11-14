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


def write(
    path: Path, *tracks: dict[str, object], marks: object = None, meter: object = None
) -> Path:
    data: dict[str, object] = {"bpm": 60, "tracks": list(tracks)}
    if marks is not None:
        data["marks"] = marks
    if meter is not None:
        data["meter"] = meter
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


# What the fixtures' notes make of their labels: the song leaves C major for F# major at beat 16.
F_A_DETAIL = "33% of the sounding time is on the tones of F/A, most of the rest on G and B"
G7_DETAIL = "0% of the sounding time is on the tones of G7, most of the rest on F#"
A_MINOR_DETAIL = "17% of the sounding time is outside A minor, most on C#, D# and A#"

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
        f"    warning: {F_A_DETAIL}\n"
        "  beats 24-27: G7\n"
        f"    warning: {G7_DETAIL}\n"
        "parts marks (text)\n"
        "  beats 0-16: opening\n"
        "  beats 16-27: lift\n" + TONAL_TEXT
    )


def test_options_show_every_mark(
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
                    "check": "agrees",
                },
                {
                    "beat": 4,
                    "end": 8,
                    "text": "F/A",
                    "value": {"chord": "F/A", "root": 5, "quality": "maj", "bass": 9},
                    "check": "contradicts",
                    "detail": F_A_DETAIL,
                },
                {
                    "beat": 24,
                    "end": 27,
                    "text": "G7",
                    "value": {"chord": "G7", "root": 7, "quality": "7", "bass": 7},
                    "check": "contradicts",
                    "detail": G7_DETAIL,
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
        f"key marks (key vocabulary)\n  beats 0-27: A minor\n    warning: {A_MINOR_DETAIL}\n"
        + TONAL_TEXT
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
                    "check": "contradicts",
                    "detail": A_MINOR_DETAIL,
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


# Claim checks: one line at most under a mark, by its check and its `deliberate` flag.

NOTHING_SOUNDS = "nothing sounds"


@pytest.mark.parametrize(
    ("mark", "line", "check"),
    [
        ({"beat": 4, "end": 8, "text": "F/A"}, f"warning: {F_A_DETAIL}", "contradicts"),
        (
            {"beat": 4, "end": 8, "text": "F/A", "deliberate": True},
            f"deliberate: {F_A_DETAIL}",
            "contradicts",
        ),
        ({"beat": 12, "end": 16, "text": "C"}, f"unchecked: {NOTHING_SOUNDS}", "unchecked"),
        (
            {"beat": 12, "end": 16, "text": "C", "deliberate": True},
            f"unchecked: {NOTHING_SOUNDS}",
            "unchecked",
        ),
        ({"beat": 0, "end": 4, "text": "Am"}, None, "agrees"),
        (
            {"beat": 0, "end": 4, "text": "Am", "deliberate": True},
            "deliberate: not needed, the notes agree",
            "agrees",
        ),
    ],
    ids=[
        "contradicts",
        "contradicts-deliberate",
        "unchecked",
        "unchecked-deliberate",
        "agrees",
        "agrees-deliberate",
    ],
)
def test_a_checked_mark_prints_at_most_one_line_about_its_check(
    workspace: CommandWorkspace,
    capsys: pytest.CaptureFixture[str],
    mark: dict[str, object],
    line: str | None,
    check: str,
) -> None:
    song = marked(workspace, [mark])
    span = f"  beats {mark['beat']}-{mark['end']}: {mark['text']}\n"
    under = "" if line is None else f"    {line}\n"

    assert read.main([str(song)]) == 0
    assert capsys.readouterr().out == (
        "chords marks (chords vocabulary)\n"
        + span
        + under
        + "parts marks (text)\n  beats 0-16: opening\n  beats 16-27: lift\n"
        + TONAL_TEXT
    )

    assert read.main([str(song), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    (written,) = data["marks"]["chords"]["marks"]
    assert written["check"] == check
    assert written.get("detail") == (
        None if line is None or check == "agrees" else line.split(": ", 1)[1]
    )
    assert written.get("deliberate") == mark.get("deliberate")


@pytest.mark.parametrize(
    ("flag", "under"),
    [({}, ""), ({"deliberate": True}, "    deliberate: nothing checks this layer\n")],
    ids=["plain", "deliberate"],
)
def test_a_mark_nothing_checks_says_so_only_when_deliberate(
    workspace: CommandWorkspace,
    capsys: pytest.CaptureFixture[str],
    flag: dict[str, object],
    under: str,
) -> None:
    song = write(
        workspace.root / "parts.yaml",
        track("keys", CADENCE),
        marks={"parts": [{"beat": 0, "end": 11, "text": "opening", **flag}]},
    )

    assert read.main([str(song)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("parts marks (text)\n  beats 0-11: opening\n" + under + "tonal (")

    assert read.main([str(song), "--json"]) == 0
    (written,) = json.loads(capsys.readouterr().out)["marks"]["parts"]["marks"]
    assert written == {"beat": 0, "end": 11, "text": "opening", **flag}


def test_a_track_selection_narrows_the_notes_a_claim_is_checked_against(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    song = write(
        workspace.root / "layered.yaml",
        track("keys", [(0, 60, 4), (0, 64, 4), (0, 67, 4)]),
        track("other", [(0, pitch, 4) for pitch in (54, 58, 61, 63, 68)]),
        marks={"chords": [{"beat": 0, "end": 4, "text": "C"}]},
    )
    header = "chords marks (chords vocabulary)\n  beats 0-4: C\n"

    assert read.main([str(song)]) == 0
    assert capsys.readouterr().out.startswith(
        header + "    warning: 37% of the sounding time is on the tones of C, most of the rest on "
        "C#, D# and F#\n"
    )

    assert read.main([str(song), "--track", "keys"]) == 0
    assert capsys.readouterr().out.startswith(header + "tonal (tracks: keys)\n")


def test_marks_that_agree_print_what_they_always_did(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    raised = [(beat + 16, pitch + 6, length) for beat, pitch, length in CADENCE]
    song = write(
        workspace.root / "agreeing.yaml",
        track("keys", CADENCE + raised),
        track("bass", [(0, 48, 2), (4, 43, 2), (16, 54, 2), (20, 49, 2)]),
        marks={
            "chords": [{"beat": 0, "end": 4, "text": "Am"}, {"beat": 7, "end": 11, "text": "C"}],
            "key": [
                {"beat": 0, "end": 16, "text": "C major"},
                {"beat": 16, "end": 27, "text": "F# major"},
            ],
        },
    )

    assert read.main([str(song)]) == 0

    assert capsys.readouterr().out == (
        "chords marks (chords vocabulary)\n"
        "  beats 0-4: Am\n"
        "  beats 7-11: C\n"
        "key marks (key vocabulary)\n"
        "  beats 0-16: C major\n"
        "  beats 16-27: F# major\n" + TONAL_TEXT
    )


# Meter


def metered(workspace: CommandWorkspace, meter: object, marks: object = None) -> Path:
    """The `song` fixture's pitched tracks, which end at beat 27, with a meter."""
    raised = [(beat + 16, pitch + 6, length) for beat, pitch, length in CADENCE]
    return write(
        workspace.root / "metered.yaml",
        track("keys", CADENCE + raised),
        track("bass", [(0, 48, 2), (4, 43, 2), (16, 54, 2), (20, 49, 2)]),
        marks=marks,
        meter=meter,
    )


def test_a_meter_prints_before_the_marks_with_bar_numbers(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    parts = [
        {"beat": 0, "end": 16, "text": "opening"},
        {"beat": 16, "end": 20, "text": "turn"},
        {"beat": 20, "end": 22, "text": "half"},
        {"beat": 22, "end": 40, "text": "lift"},
    ]
    path = metered(workspace, [{"beat": 0, "bar": 4}], {"chords": CHORDS, "parts": parts})

    assert read.main([str(path)]) == 0

    assert capsys.readouterr().out == (
        "meter\n"
        "  beats 0-27: 4-beat bars\n"
        "chords marks (chords vocabulary)\n"
        "  beats 0-4 (bar 1): Am\n"
        "  beats 4-8 (bar 2): F/A\n"
        f"    warning: {F_A_DETAIL}\n"
        "  beats 24-27 (bar 7): G7\n"
        f"    warning: {G7_DETAIL}\n"
        "parts marks (text)\n"
        "  beats 0-16 (bars 1-4): opening\n"
        "  beats 16-20 (bar 5): turn\n"
        "  beats 20-22: half\n"
        "  beats 22-27: lift\n"
        "tonal (tracks: keys, bass)\n"
        "  beats 0-16 (bars 1-4): C major (r 0.90)\n"
        "  beats 16-27 (bars 5-7): F# major (r 0.90)\n"
    )


def test_changing_and_fractional_meters_number_bars_across_gaps(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    meter = [{"beat": 2, "bar": 3.5, "end": 9}, {"beat": 16, "bar": 5}, {"beat": 40, "bar": 4}]
    parts = [
        {"beat": 0, "end": 2, "text": "free"},
        {"beat": 2, "end": 9, "text": "sevens"},
        {"beat": 9, "end": 16, "text": "gap"},
        {"beat": 16, "end": 21, "text": "five"},
        {"beat": 21, "end": 27, "text": "rest"},
    ]
    path = metered(workspace, meter, {"parts": parts})

    assert read.main([str(path)]) == 0

    assert capsys.readouterr().out == (
        "meter\n"
        "  beats 2-9: 3.5-beat bars\n"
        "  beats 16-27: 5-beat bars\n"
        "parts marks (text)\n"
        "  beats 0-2: free\n"
        "  beats 2-9 (bars 1-2): sevens\n"
        "  beats 9-16: gap\n"
        "  beats 16-21 (bar 3): five\n"
        "  beats 21-27 (bars 4-5): rest\n"
        "tonal (tracks: keys, bass)\n"
        "  beats 0-16: C major (r 0.90)\n"
        "  beats 16-27 (bars 3-5): F# major (r 0.90)\n"
    )


def test_json_adds_the_meter_after_the_end(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    meter = [{"beat": 2, "bar": 3.5, "end": 9}, {"beat": 16, "bar": 5}, {"beat": 40, "bar": 4}]
    path = metered(workspace, meter, {"parts": PARTS})

    assert read.main([str(path), "--json"]) == 0
    data = json_object(json.loads(capsys.readouterr().out))

    assert data is not None
    assert list(data) == ["end", "meter", "tracks", "frameworks", "marks"]
    assert data["meter"] == [
        {"beat": 2, "end": 9, "bar": 3.5},
        {"beat": 16, "end": 27, "bar": 5},
    ]
    # Apart from the meter, the JSON is what the song gives without one: no bar numbers.
    assert read.main([str(marked(workspace, CHORDS)), "--json"]) == 0
    plain = json_object(json.loads(capsys.readouterr().out))
    assert plain is not None
    assert {key: data[key] for key in ("end", "tracks", "frameworks")} == TONAL_JSON
    plain_marks = json_object(plain["marks"])
    assert plain_marks is not None
    assert data["marks"] == {"parts": plain_marks["parts"]}


def test_a_meter_that_starts_after_the_song_prints_no_block(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    path = metered(workspace, [{"beat": 40, "bar": 4}])

    assert read.main([str(path)]) == 0
    assert capsys.readouterr().out == TONAL_TEXT
    assert read.main([str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {**TONAL_JSON, "meter": []}


def test_a_song_without_a_meter_prints_no_bars(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert read.main([str(marked(workspace))]) == 0
    out = capsys.readouterr().out
    assert "meter" not in out
    assert "bar" not in out
    assert read.main([str(marked(workspace)), "--json"]) == 0
    assert "meter" not in json.loads(capsys.readouterr().out)
