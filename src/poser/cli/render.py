"""`render-song`: render a song to one stem per track.

A stem whose inputs are unchanged since it was rendered is reused, unless `--force` is given.
"""

import argparse
import json
from pathlib import Path

from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    EXIT_SONG_BUSY,
    RENDER_TIMEOUT_S,
    align_rows,
    fail,
    load_composition,
)
from poser.cli.workspace import WorkspaceError, fill_path_options, song_id
from poser.core.jsonvalue import JsonValue
from poser.core.loading import load_song
from poser.core.ports import ConfigError, SongBusyError
from poser.core.rendering import TrackResult, render_song
from poser.core.song import SongError
from poser.core.statuses import StemStatus

PROG = "render-song"


def result_to_data(result: TrackResult) -> dict[str, JsonValue]:
    return {
        "track": str(result.track),
        "status": str(result.status),
        "peak_dbfs": result.peak_dbfs,
        "stem": None if result.stem is None else str(result.stem),
        "detail": result.detail,
        "cached": result.cached,
    }


def exit_code(results: list[TrackResult]) -> int:
    all_rendered = all(result.status is StemStatus.RENDERED for result in results)
    return EXIT_ALL_PASS if all_rendered else EXIT_SOME_FAILED


def format_table(results: list[TrackResult]) -> str:
    rows = [("TRACK", "STATUS", "CACHED", "PEAK DBFS", "STEM")]
    details = []
    for result in results:
        peak = "-" if result.peak_dbfs is None else f"{result.peak_dbfs:.1f}"
        cached = "yes" if result.cached else "-"
        stem = "-" if result.stem is None else str(result.stem)
        rows.append((result.track, str(result.status), cached, peak, stem))
        if result.detail:
            details.append(f"{result.track}: {result.detail}")
    return "\n".join([*align_rows(rows), *details])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    parser.add_argument("song", type=Path, help="song file (YAML, or a script defining build())")
    parser.add_argument("--config", type=Path, help="workspace config (default: poser.toml)")
    parser.add_argument("--output-dir", type=Path, help="where results go")
    parser.add_argument("--sounds-dir", type=Path, help="saved sound files")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    parser.add_argument(
        "--force", action="store_true", help="render every track, even one whose stem is current"
    )
    args = parser.parse_args(argv)

    try:
        base = fill_path_options(args)
        # Before loading, so a refused song's script never runs.
        song = song_id(base, args.song)
        composition = load_composition(args)
        formats = composition.song_formats()
        instruments = composition.instruments()
        audio_files = composition.audio_files()
        renders = composition.render_store()
    except (WorkspaceError, ConfigError) as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG
    try:
        renders.song(song)  # Before loading, for the same reason.
    except ValueError as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG

    try:
        loaded = load_song(args.song, formats)
        results = render_song(
            loaded,
            renders,
            song,
            instruments,
            audio_files,
            timeout_s=RENDER_TIMEOUT_S,
            force=args.force,
        )
    except SongError as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG
    except SongBusyError as error:
        fail(PROG, error)
        return EXIT_SONG_BUSY
    if args.json:
        print(json.dumps([result_to_data(result) for result in results], indent=2))
    else:
        print(format_table(results))
    return exit_code(results)
