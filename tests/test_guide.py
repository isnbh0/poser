"""The agent guide, `AGENTS.md`, agrees with the code it describes."""

import dataclasses
import re
import tomllib
from enum import StrEnum
from pathlib import Path

import pytest
import yaml

from poser import analyze, music, render, song
from poser.plugin_check import DEFAULT_CONFIG, PROJECT_DIR, load_config
from poser.song import Song, SongError, load_song, parse_song

GUIDE_PATH = PROJECT_DIR / "AGENTS.md"
GUIDE = GUIDE_PATH.read_text()
LINES = GUIDE.splitlines()
FENCE = re.compile(r"^```([a-z]+)\n(.*?)^```$", re.MULTILINE | re.DOTALL)
BACKTICKED = re.compile(r"`([^`\n]+)`")
INT_RANGE = re.compile(r"^(-?\d+) to (-?\d+)$")
MUSIC_HELPERS = ("pitch", "scale", "degree", "chord", "swing", "humanize", "notes")


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


def python_songs(tmp_path: Path) -> list[Song]:
    songs = []
    for index, body in enumerate(blocks("python")):
        path = tmp_path / f"example{index}" / "example.py"
        path.parent.mkdir()
        path.write_text(body)
        songs.append(load_song(path))
    return songs


def test_guide_is_short_and_claude_imports_it() -> None:
    assert len(LINES) < 200
    assert (PROJECT_DIR / "CLAUDE.md").read_text() == "@AGENTS.md\n"


def test_python_examples_load(tmp_path: Path) -> None:
    assert python_songs(tmp_path)


def test_yaml_examples_parse() -> None:
    for body in blocks("yaml"):
        parse_song(yaml.safe_load(body))


def test_commands_are_project_scripts() -> None:
    scripts = tomllib.loads((PROJECT_DIR / "pyproject.toml").read_text())["project"]["scripts"]
    commands = re.findall(r"uv run ([\w-]+)", GUIDE)
    assert set(commands) <= set(scripts)
    assert {"render-song", "analyze-song", "list-sounds"} <= set(commands)


@pytest.mark.parametrize("enum", [render.TrackStatus, analyze.TrackStatus, analyze.Defect])
def test_statuses_and_defects_are_named(enum: type[StrEnum]) -> None:
    assert {member.value for member in enum} <= backticked()


def test_report_fields_exist() -> None:
    fields = {
        field.name
        for cls in (analyze.Report, analyze.TrackReport, analyze.MixReport)
        for field in dataclasses.fields(cls)
    }
    names = set(first_column("Reading the analysis"))
    assert names <= fields
    assert {"run", "song_sha256", "track_key", "sound_key", "bands_db", "bar_dbfs"} <= names


def test_bands_are_named_with_their_ranges() -> None:
    text = " ".join(GUIDE.split())
    for name, (low, high) in analyze.BANDS.items():
        assert f"`{name}` {low:g}–{high:g} Hz" in text


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


def with_key(key: str, value: int) -> dict[str, object]:
    """Return a valid one-track song with `key` set to `value` wherever its key sets place it."""
    note: dict[str, object] = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}
    cc: dict[str, object] = {"beat": 0, "control": 1, "value": 0}
    track: dict[str, object] = {
        "name": "lead",
        "engine": "pedalboard",
        "sound": "Serum",
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
        match = INT_RANGE.match(row[-1])
        if match:
            ranges.append((name, int(match.group(1)), int(match.group(2))))
    return ranges


def test_song_key_table_has_integer_ranges() -> None:
    names = {name for name, _, _ in integer_ranges()}
    assert names == {"pitch", "velocity", "gain", "control", "value"}


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


@pytest.mark.real_plugins
def test_sound_names_resolve(tmp_path: Path) -> None:
    specs = load_config(DEFAULT_CONFIG)
    prefixes = tuple(f"{spec.name}:" for spec in specs)
    spans = {span for span in backticked() if span.startswith(prefixes)}
    songs = python_songs(tmp_path) + [parse_song(yaml.safe_load(b)) for b in blocks("yaml")]
    sounds = {track.sound for example in songs for track in example.tracks}
    assert spans
    assert sounds
    for name in sorted(spans | sounds):
        resolved = render.resolve_sound(name, specs, tmp_path / "sounds")
        assert not isinstance(resolved, render.Outcome), f"{name}: {resolved}"


@pytest.mark.real_plugins
def test_python_example_renders_without_defects(tmp_path: Path) -> None:
    specs = load_config(DEFAULT_CONFIG)
    (example,) = python_songs(tmp_path)
    song_path = tmp_path / "example0" / "example.py"
    output_dir = tmp_path / "renders"
    render.render_song(example, song_path.stem, specs, output_dir)

    report = analyze.analyze_song(example, song_path, specs, output_dir)

    for track in report.tracks:
        assert track.status is analyze.TrackStatus.ANALYZED, track.detail
        assert track.defects == []
    assert report.mix is not None
    assert report.mix.defects == [], report.mix.peak_dbfs
