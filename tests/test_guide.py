"""The composer guide, `src/poser/scaffold/guide.md`, agrees with the code it describes."""

import dataclasses
import re
import tomllib
from pathlib import Path
from typing import cast

import pytest
import yaml
from helpers import REPO_DIR, SCAFFOLD_DIR, SURGE_DIR, real_analyze, real_composition, real_render

import poser
from poser.adapters.song_formats import SONG_FORMATS
from poser.compose import DEFAULT_PRESET, PRESETS, read_config
from poser.core import analysis, checks, music, report, song
from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE, TAIL_S
from poser.core.graph import Graph
from poser.core.guide import FIRST_INSTRUMENT_PLACEHOLDER
from poser.core.loading import load_song
from poser.core.meter import Meter, MeterSpan
from poser.core.ports import ResolvedSound
from poser.core.reading import OUTCOME_TAGS, Agrees, Contradicts, Unchecked
from poser.core.rules import MAX_SONG_S
from poser.core.song import Song, SongError, parse_song
from poser.core.song import Song as SongValue
from poser.core.sounds import parse_sound_ref
from poser.core.statuses import RENDER_STATUSES

GUIDE_PATH = SCAFFOLD_DIR / "guide.md"
GUIDE = GUIDE_PATH.read_text()
LINES = GUIDE.splitlines()
FENCE = re.compile(r"^```([a-z]+)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
BACKTICKED = re.compile(r"`([^`\n]+)`")
INT_RANGE = re.compile(r"^(-?\d+) to (-?\d+)$")
MUSIC_HELPERS = (
    "pitch",
    "scale",
    "degree",
    "chord",
    "swing",
    "humanize",
    "notes",
    "transpose",
    "transpose_degrees",
)
COMPOSER_API = [
    "ControlChange",
    "DelayNode",
    "EffectNode",
    "Graph",
    "GraphNode",
    "Mark",
    "Marks",
    "Meter",
    "MeterSpan",
    "Note",
    "Song",
    "SongError",
    "Track",
    "TrackNode",
    "Wire",
    "chord",
    "degree",
    "humanize",
    "notes",
    "pitch",
    "scale",
    "swing",
    "transpose",
    "transpose_degrees",
]


def blocks(language: str) -> list[str]:
    return [body for lang, body in FENCE.findall(GUIDE) if lang == language]


def backticked() -> set[str]:
    return set(BACKTICKED.findall(GUIDE))


def table(heading: str) -> list[list[str]]:
    """Return the cells of each body row of the first table under a `##` heading."""
    start = LINES.index(f"## {heading}")
    rows: list[list[str]] = []
    for line in LINES[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
        elif rows:
            break
    assert len(rows) > 2, f"no table under ## {heading}"
    return rows[2:]  # Skip the header and separator rows.


def first_column(heading: str) -> list[str]:
    names = []
    for row in table(heading):
        match = BACKTICKED.search(row[0])
        assert match is not None, f"row without a backticked name: {row}"
        names.append(match.group(1))
    return names


def python_examples(tmp_path: Path) -> list[Song | Graph]:
    """Load the guide's example scripts through the public song frontend."""
    songs = []
    first = read_config(SURGE_DIR / "poser.toml").instruments[0].name
    for index, body in enumerate(blocks("python")):
        path = tmp_path / f"example{index}" / "example.py"
        path.parent.mkdir()
        path.write_text(body.replace(FIRST_INSTRUMENT_PLACEHOLDER, f'"{first}"'))
        song = load_song(path, SONG_FORMATS).source
        assert isinstance(song, Song | Graph)
        songs.append(song)
    return songs


def python_songs(tmp_path: Path) -> list[Song]:
    return [example for example in python_examples(tmp_path) if isinstance(example, Song)]


def test_core_audio_format_and_fixed_tail_are_documented() -> None:
    rendering = " ".join(
        GUIDE.split("## Rendering\n", 1)[1].split("## Reading the analysis", 1)[0].split()
    )
    assert {int(value) for value in re.findall(r"\b(\d+)\s+channels\b", rendering)} == {
        NUM_CHANNELS
    }
    assert {int(value) for value in re.findall(r"\b(\d+)\s+Hz\b", rendering)} == {SAMPLE_RATE}
    assert {float(value) for value in re.findall(r"\b(\d+(?:\.\d+)?)\s+seconds\b", rendering)} == {
        TAIL_S
    }


def test_python_examples_load(tmp_path: Path) -> None:
    assert python_examples(tmp_path)


def test_graph_example_branches_into_several_mix_leaves(tmp_path: Path) -> None:
    (graph,) = [example for example in python_examples(tmp_path) if isinstance(example, Graph)]
    assert len(analysis._leaves(graph)) >= 2
    assert any(
        sum(wire.from_name == node.name for wire in graph.wires) >= 2 for node in graph.nodes
    )


def test_yaml_examples_parse() -> None:
    for body in blocks("yaml"):
        parse_song(yaml.safe_load(body))


def test_commands_are_project_scripts() -> None:
    scripts = tomllib.loads((REPO_DIR / "pyproject.toml").read_text())["project"]["scripts"]
    commands = re.findall(r"uv run ([\w-]+)", GUIDE)
    assert set(commands) <= set(scripts)


def section(heading: str) -> str:
    """Return the text under a `##` heading, up to the next one."""
    start = LINES.index(f"## {heading}")
    end = next(
        (i for i, line in enumerate(LINES[start + 1 :], start + 1) if line.startswith("## ")),
        len(LINES),
    )
    return "\n".join(LINES[start + 1 : end])


def test_framework_readings_name_every_outcome_and_preset() -> None:
    text = section("Framework readings")
    assert re.findall(r"^- (\w+):", text, re.MULTILINE) == list(OUTCOME_TAGS)
    named = set(BACKTICKED.findall(text))
    assert set(PRESETS) <= named
    assert f"`{DEFAULT_PRESET}` preset" in " ".join(text.split())
    assert set(PRESETS[DEFAULT_PRESET]["vocabularies"]) <= named


def test_statuses_and_defects_are_named() -> None:
    names = {
        *RENDER_STATUSES,
        *report.ANALYSIS_STATUSES,
        *(str(status) for status in checks.CheckFailureStatus),
        *(str(status) for status in checks.MeasuredStatus),
        *(str(defect) for defect in report.Defect),
    }
    assert names <= backticked()


def test_report_fields_exist() -> None:
    fields = {
        field.name
        for cls in (
            report.CompleteReport,
            report.AnalyzedNode,
            report.UnrenderedNode,
            report.MixReport,
            report.Measurements,
        )
        for field in dataclasses.fields(cls)
    }
    names = set(first_column("Reading the analysis"))
    assert names <= fields
    assert {
        "run",
        "song_sha256",
        "node_key",
        "sound_key",
        "bands_db",
        "spans",
        "span_dbfs",
    } <= names


def test_bands_are_named_with_their_ranges() -> None:
    text = " ".join(GUIDE.split())
    ranges = re.findall(
        r"`(\w+)`\s+(\d+(?:\.\d+)?)\s*(?:[-\u2013\u2014]|to)\s*(\d+(?:\.\d+)?)\s+Hz\b",
        text,
    )
    assert {name: (float(low), float(high)) for name, low, high in ranges} == report.BANDS


def test_song_keys_match_the_code() -> None:
    keys = (
        song.SONG_KEYS
        | song.OPTIONAL_SONG_KEYS
        | song.MARK_KEYS
        | song.OPTIONAL_MARK_KEYS
        | song.METER_KEYS
        | song.OPTIONAL_METER_KEYS
        | song.TRACK_KEYS
        | song.OPTIONAL_TRACK_KEYS
        | song.NOTE_KEYS
        | song.CC_KEYS
        | song.OPTIONAL_CC_KEYS
    )
    names = first_column("Songs")
    assert len(names) == len(set(names))
    assert set(names) == keys


def with_key(key: str, value: object) -> dict[str, object]:
    """Return a valid one-track song with `key` set to `value` wherever its key sets place it."""
    note: dict[str, object] = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}
    cc: dict[str, object] = {"beat": 0, "control": 1, "value": 0}
    track: dict[str, object] = {
        "name": "lead",
        "sound": "serum",
        "notes": [note],
        "cc": [cc],
    }
    mark: dict[str, object] = {"beat": 0, "end": 1, "text": "a"}
    span: dict[str, object] = {"beat": 0, "bar": 4}
    data: dict[str, object] = {"bpm": 100, "tracks": [track]}
    if key in song.SONG_KEYS | song.OPTIONAL_SONG_KEYS:
        data[key] = value
    if key in (song.MARK_KEYS | song.OPTIONAL_MARK_KEYS) - {"beat"}:
        mark[key] = value
        data["marks"] = {"parts": [mark]}
    if key in (song.METER_KEYS | song.OPTIONAL_METER_KEYS) - {"beat"}:
        span[key] = value
        data["meter"] = [span]
    if key in song.TRACK_KEYS | song.OPTIONAL_TRACK_KEYS:
        track[key] = value
    if key in song.NOTE_KEYS:
        note[key] = value
    if key in song.CC_KEYS | song.OPTIONAL_CC_KEYS:
        cc[key] = value
    return data


# A valid value for every key in the Songs table, so `with_key` places each one.
VALID_NOTES = [{"beat": 0, "pitch": 62, "length": 1, "velocity": 90}]
VALID_VALUES: dict[str, object] = {
    "bpm": 120,
    "tracks": [{"name": "solo", "sound": "serum", "notes": VALID_NOTES}],
    "name": "solo",
    "sound": "serum:Pad",
    "effects": [],
    "notes": VALID_NOTES,
    "gain": -3,
    "cc": [],
    "pitched": True,
    "beat": 0.5,
    "pitch": 64,
    "length": 2,
    "velocity": 90,
    "control": 7,
    "value": 64,
    "shape": "step",
    "marks": {"parts": [{"beat": 0, "end": 4, "text": "intro"}]},
    "end": 8,
    "text": "verse two",
    "deliberate": True,
    "meter": [{"beat": 0, "bar": 7, "end": 14}, {"beat": 14, "bar": 3.5}],
    "bar": 3.5,
}


def test_every_song_key_is_placed_and_accepts_a_valid_value() -> None:
    assert set(VALID_VALUES) == set(first_column("Songs"))
    for key, value in VALID_VALUES.items():
        data = with_key(key, value)
        assert data != with_key(key, object()), f"with_key does not place {key!r}"
        parse_song(data)


def test_mark_rows_match_validation() -> None:
    rows = {row[0]: row[1:] for row in table("Songs")}
    assert rows["`marks`"][0] == "song, optional"
    assert rows["`end`"] == ["mark; meter span, optional", "number", "after `beat`"]
    assert rows["`text`"][:2] == ["mark", "text"]
    for rejected in (0, -1):
        with pytest.raises(SongError):
            parse_song(with_key("end", rejected))
    for rejected in ("", " a", "a\nb", 5):
        with pytest.raises(SongError):
            parse_song(with_key("text", rejected))
    with pytest.raises(SongError):
        parse_song(with_key("marks", {"Parts": [{"beat": 0, "end": 1, "text": "a"}]}))


def test_the_deliberate_row_matches_validation() -> None:
    rows = {row[0]: row[1:] for row in table("Songs")}
    assert rows["`deliberate`"][:2] == ["mark, optional", "`true` or `false`"]
    parse_song(with_key("deliberate", True))
    parse_song(with_key("deliberate", False))
    for rejected in ("yes", 1, None):
        with pytest.raises(SongError, match=r"marks\.parts\[0\]\.deliberate"):
            parse_song(with_key("deliberate", rejected))


def test_meter_rows_match_validation() -> None:
    rows = {row[0]: row[1:] for row in table("Songs")}
    assert rows["`meter`"] == ["song, optional", "list of meter spans", "in order without overlap"]
    assert rows["`bar`"] == ["meter span", "beats", "at least 1"]
    assert "meter span" in rows["`beat`"][0]
    for accepted in (1, 3.5, 7):
        parse_song(with_key("bar", accepted))
    for rejected in (0, 0.99, -4, "4", None):
        with pytest.raises(SongError, match=r"meter\[0\]\.bar"):
            parse_song(with_key("bar", rejected))
    # `end` is placed on a mark and on a meter span, and both refuse one at or before `beat`.
    with pytest.raises(SongError) as error:
        parse_song(with_key("end", 0))
    assert [problem.split(":")[0] for problem in error.value.problems] == [
        "marks.parts[0].end",
        "meter[0].end",
    ]
    with pytest.raises(SongError, match=r"meter\[1\]: must start at or after"):
        parse_song(with_key("meter", [{"beat": 0, "bar": 4, "end": 8}, {"beat": 4, "bar": 3}]))


def test_the_guide_states_the_meter_and_its_examples_declare_one(tmp_path: Path) -> None:
    songs = " ".join(section("Songs").split())
    assert "`meter=Meter([MeterSpan(0, 7)])`" in songs
    assert "`Meter.beat_of`" in songs
    assert "Meter never changes audio" in songs
    meter = Meter([MeterSpan(0, 7)])
    assert '`Mark(meter.beat_of(5), meter.beat_of(9), "Am")`' in songs
    assert (meter.beat_of(5), meter.beat_of(9)) == (meter.bars(63)[4].beat, meter.bars(63)[7].end)
    assert all(example.meter for example in python_examples(tmp_path))
    readings = " ".join(section("Framework readings").split())
    assert "`beats 0-84: 7-beat bars`" in readings
    # The bar numbers the guide shows are the ones a 7-beat span followed by 4-beat bars gives.
    bars = Meter([MeterSpan(0, 7, 84), MeterSpan(84, 4)]).bars(116)
    assert "`beats 84-112 (bars 13-19)`" in readings
    assert [(bar.beat, bar.end) for bar in bars if bar.number in (13, 19)] == [(84, 88), (108, 112)]
    assert "`beats 112-116 (bar 20)`" in readings
    assert (bars[-1].number, bars[-1].beat, bars[-1].end) == (20, 112, 116)


def test_framework_readings_cover_claim_checks() -> None:
    text = " ".join(section("Framework readings").split())
    named = set(BACKTICKED.findall(text))
    assert {"warning: <detail>", "unchecked: <reason>", "deliberate: <detail>"} <= named
    assert {"check", "detail", "deliberate", Agrees.TAG, Contradicts.TAG, Unchecked.TAG} <= named
    assert '`Mark(0, 4, "C", deliberate=True)`' in text
    assert "`--track` narrows the notes a claim is checked against" in text
    assert "Neither line changes the exit code" in text
    assert "Marks never change audio or readings" in " ".join(section("Songs").split())


def integer_ranges() -> list[tuple[str, int, int]]:
    ranges = []
    for name, row in zip(first_column("Songs"), table("Songs"), strict=True):
        # A `number` row takes fractions inside its range, so its bounds are not integer bounds.
        match = INT_RANGE.match(row[-1])
        if match and row[2] != "number":
            ranges.append((name, int(match.group(1)), int(match.group(2))))
    return ranges


def test_song_key_table_has_integer_ranges() -> None:
    names = {name for name, _, _ in integer_ranges()}
    assert names == {"velocity", "gain", "control", "value"}


def test_pitch_row_is_a_number_and_matches_validation() -> None:
    (row,) = [row for row in table("Songs") if row[0] == "`pitch`"]
    assert row[2:] == ["number", "0 to 127"]
    for accepted in (0, 127, 60.5):
        parse_song(with_key("pitch", accepted))
    for rejected in (-0.5, 127.5):
        with pytest.raises(SongError):
            parse_song(with_key("pitch", rejected))


def test_pitched_row_matches_validation() -> None:
    (row,) = [row for row in table("Songs") if row[0] == "`pitched`"]
    assert row[1:3] == ["track, optional", "true or false"]
    parse_song(with_key("pitched", True))
    parse_song(with_key("pitched", False))
    with pytest.raises(SongError):
        parse_song(with_key("pitched", "false"))


@pytest.mark.parametrize(("key", "low", "high"), integer_ranges())
def test_song_key_ranges_match_validation(key: str, low: int, high: int) -> None:
    parse_song(with_key(key, low))
    parse_song(with_key(key, high))
    with pytest.raises(SongError):
        parse_song(with_key(key, low - 1))
    with pytest.raises(SongError):
        parse_song(with_key(key, high + 1))


def test_music_helpers_are_listed() -> None:
    listed = re.findall(r"^- `(\w+)` — ", GUIDE, re.MULTILINE)
    assert sorted(listed) == sorted(MUSIC_HELPERS)
    assert all(callable(getattr(music, name, None)) for name in listed)
    assert all(callable(getattr(poser, name, None)) for name in listed)


def test_composer_api_exports() -> None:
    assert poser.__all__ == COMPOSER_API
    assert set(MUSIC_HELPERS) <= set(poser.__all__)


def test_song_length_limit_is_stated() -> None:
    assert f"{MAX_SONG_S:.0f} seconds" in " ".join(GUIDE.split())


def scaffold_notes() -> list[str]:
    config = read_config(SURGE_DIR / "poser.toml")
    return [item.guide for item in config.instruments if item.guide is not None]


@pytest.mark.real_plugins
def test_sound_names_resolve(tmp_path: Path) -> None:
    instruments = real_composition(tmp_path).instruments()
    prefixes = tuple(f"{name}:" for name in instruments)
    texts = [GUIDE, *scaffold_notes()]
    spans = {
        span for text in texts for span in BACKTICKED.findall(text) if span.startswith(prefixes)
    }
    songs = python_songs(tmp_path) + [parse_song(yaml.safe_load(b)) for b in blocks("yaml")]
    songs.append(cast("SongValue", load_song(SURGE_DIR / "demo.yaml", SONG_FORMATS).source))
    sounds = {track.sound for example in songs for track in example.tracks}
    assert spans
    assert sounds
    for name in sorted(spans | sounds):
        ref = parse_sound_ref(name)
        resolved = instruments[ref.instrument].resolve(ref)
        assert isinstance(resolved, ResolvedSound), f"{name}: {resolved}"


@pytest.mark.real_plugins
def test_python_example_renders_without_defects(tmp_path: Path) -> None:
    (example,) = python_songs(tmp_path)
    real_render(tmp_path, example, "example0/example.py")

    result = real_analyze(tmp_path, example, "example0/example.py")

    assert isinstance(result, report.CompleteReport), result
    for node in result.nodes:
        assert node.defects == ()
    assert result.mix.defects == (), result.mix.measurements.peak_dbfs
