"""`analyze-song`: mix a song's rendered stems and report defects an agent can read.

Every analysis keeps a numbered record, even a failed one. Stems come from the song's track names,
so a stem left by a removed or renamed track cannot enter the mix.
"""

import argparse
from collections.abc import Mapping
from pathlib import Path

from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    EXIT_SONG_BUSY,
    align_rows,
    fail,
    load_composition,
)
from poser.cli.workspace import WorkspaceError, fill_path_options, song_id
from poser.core.analysis import analyze_song, report_json
from poser.core.loading import load_song
from poser.core.ports import ConfigError, SongBusyError
from poser.core.report import (
    MixReport,
    NodeReport,
    Report,
    SectionError,
    SectionReport,
    report_failed,
)
from poser.core.song import SongError

PROG = "analyze-song"
MIX_DETAIL_NAME = "mix"


def exit_code(report: Report) -> int:
    return EXIT_SOME_FAILED if report_failed(report) else EXIT_ALL_PASS


def section_details(owner: str, sections: Mapping[str, SectionReport]) -> list[str]:
    """One line per section result that fails the analysis."""
    lines: list[str] = []
    for name, section in sections.items():
        if isinstance(section, SectionError):
            lines.append(f"{owner}: section {name}: {section.error}")
        elif section.defects:
            lines.append(f"{owner}: section {name}: {', '.join(section.defects)}")
    return lines


def format_table(report: Report) -> str:
    def number(value: float | None) -> str:
        return "-" if value is None else f"{value:.1f}"

    def row(name: str, status: str, values: NodeReport | MixReport) -> tuple[str, ...]:
        clipped = "-" if values.clipped_samples is None else str(values.clipped_samples)
        gain = number(values.gain_db) if isinstance(values, NodeReport) else "-"
        return (
            name,
            status,
            gain,
            number(values.peak_dbfs),
            number(values.integrated_lufs),
            clipped,
            ", ".join(values.defects) or "-",
        )

    rows = [("NAME", "STATUS", "GAIN DB", "PEAK DBFS", "LUFS", "CLIPPED", "DEFECTS")]
    details: list[str] = []
    for node in report.nodes:
        rows.append(row(node.name, str(node.status), node))
        if node.detail:
            details.append(f"{node.name}: {node.detail}")
        details += section_details(node.name, node.sections)
    # Brackets cannot appear in a node name, so this row cannot be mistaken for a node.
    if report.mix is None:
        rows.append(("[mix]", "not_written", "-", "-", "-", "-", "-"))
    else:
        rows.append(row("[mix]", "written", report.mix))
        details += section_details(MIX_DETAIL_NAME, report.mix.sections)
    return "\n".join([*align_rows(rows), *details])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    parser.add_argument("song", type=Path, help="song file (YAML, or a script defining build())")
    parser.add_argument("--config", type=Path, help="workspace config (default: poser.toml)")
    parser.add_argument("--output-dir", type=Path, help="where results are")
    parser.add_argument("--sounds-dir", type=Path, help="saved sound files")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
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
        record_writer = composition.record_writer()
        tools = composition.analysis_tools()
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
        report = analyze_song(
            loaded,
            Path(args.song).stem,
            renders,
            song,
            instruments,
            audio_files,
            record_writer,
            tools,
        )
    except SongError as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG
    except SongBusyError as error:
        fail(PROG, error)
        return EXIT_SONG_BUSY
    print(report_json(report) if args.json else format_table(report), end="" if args.json else "\n")
    return exit_code(report)
