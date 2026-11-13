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
from poser.compose import read_config
from poser.core import analysis, music, report, song
from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE, TAIL_S
from poser.core.graph import Graph
from poser.core.guide import FIRST_INSTRUMENT_PLACEHOLDER
from poser.core.loading import load_song
from poser.core.ports import ResolvedSound
from poser.core.song import MAX_SONG_S, Song, SongError, parse_song
from poser.core.song import Song as SongValue
from poser.core.sounds import parse_sound_ref
from poser.core.statuses import RENDER_STATUSES

GUIDE_PATH = SCAFFOLD_DIR / "guide.md"
GUIDE = GUIDE_PATH.read_text()
LINES = GUIDE.splitlines()
FENCE = re.compile(r"^```([a-z]+)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
BACKTICKED = re.compile(r"`([^`\n]+)`")
INT_RANGE = re.compile(r"^(-?\d+) to (-?\d+)$")
MUSIC_HELPERS = ("pitch", "scale", "degree", "chord", "swing", "humanize", "notes")
COMPOSER_API = [
    "ControlChange",
    "Graph",
    "Node",
    "Note",
    "Song",
    "SongError",
    "Track",
    "Wire",
    "chord",
    "degree",
    "humanize",
    "notes",
    "pitch",
    "scale",
    "swing",
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
        song = load_song(path, SONG_FORMATS)
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


def test_statuses_and_defects_are_named() -> None:
    names = {
        *RENDER_STATUSES,
        *(str(status) for status in report.AnalysisStatus),
        *(str(defect) for defect in report.Defect),
    }
    assert names <= backticked()


def test_report_fields_exist() -> None:
    fields = {
        field.name
        for cls in (report.Report, report.NodeReport, report.MixReport)
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
        "bar_dbfs",
        "sections",
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
    data: dict[str, object] = {"bpm": 100, "tracks": [track]}
    if key in song.SONG_KEYS:
        data[key] = value
    if key in song.TRACK_KEYS | song.OPTIONAL_TRACK_KEYS:
        track[key] = value
    if key in song.NOTE_KEYS:
        note[key] = value
    if key in song.CC_KEYS | song.OPTIONAL_CC_KEYS:
        cc[key] = value
    return data


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
    songs.append(cast("SongValue", load_song(SURGE_DIR / "demo.yaml", SONG_FORMATS)))
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

    for node in result.nodes:
        assert node.status is report.AnalysisStatus.ANALYZED, node.detail
        assert node.defects == ()
    assert result.mix is not None
    assert result.mix.defects == (), result.mix.peak_dbfs
