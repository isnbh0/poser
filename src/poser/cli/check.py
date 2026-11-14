"""`check-instruments`: check that each instrument in poser.toml loads and plays every note.

Repeatability is informational: exact sample-value equality with no tolerance, not a pass
requirement. Each instrument renders a short pattern twice. A plugin that crashes, hangs, or
opens a license dialog fails its own row without stopping the others.
"""

import argparse
import json
from pathlib import Path

from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    RENDER_TIMEOUT_S,
    align_rows,
    fail,
    load_composition,
)
from poser.cli.workspace import WorkspaceError, fill_path_options
from poser.core.checking import CheckResult, check_instrument
from poser.core.checks import CheckAnalysis, CheckFailure, MeasuredStatus
from poser.core.jsonvalue import JsonValue
from poser.core.ports import ConfigError

PROG = "check-instruments"
YES_NO = {True: "yes", False: "no"}


def exit_code(results: list[CheckResult]) -> int:
    all_pass = all(
        isinstance(result.outcome, CheckAnalysis) and result.outcome.status is MeasuredStatus.PASS
        for result in results
    )
    return EXIT_ALL_PASS if all_pass else EXIT_SOME_FAILED


def result_to_data(result: CheckResult) -> dict[str, JsonValue]:
    """Return one check as JSON data, with null for each measurement a failure has none of."""
    data: dict[str, JsonValue] = {
        "name": str(result.name),
        "adapter": str(result.adapter),
        "status": str(result.status),
    }
    match result.outcome:
        case CheckAnalysis() as analysis:
            data.update(
                peak_dbfs=analysis.peak_dbfs,
                note_peaks_dbfs=list(analysis.note_peaks_dbfs),
                clipped=analysis.clipped,
                repeatable=analysis.repeatable,
                max_difference_dbfs=analysis.max_difference_dbfs,
                detail=None,
            )
        case CheckFailure() as failure:
            data.update(
                peak_dbfs=None,
                note_peaks_dbfs=None,
                clipped=None,
                repeatable=None,
                max_difference_dbfs=None,
                detail=failure.detail,
            )
    return data


def format_table(results: list[CheckResult]) -> str:
    rows = [("INSTRUMENT", "ADAPTER", "STATUS", "PEAK DBFS", "REPEATABLE")]
    details = []
    for result in results:
        match result.outcome:
            case CheckAnalysis() as analysis:
                peak, repeatable = f"{analysis.peak_dbfs:.1f}", YES_NO[analysis.repeatable]
            case CheckFailure() as failure:
                peak, repeatable = "-", "-"
                details.append(f"{result.name}: {failure.detail}")
        rows.append((result.name, result.adapter, str(result.status), peak, repeatable))
    return "\n".join([*align_rows(rows), *details])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    parser.add_argument("--config", type=Path, help="workspace config (default: poser.toml)")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)

    try:
        fill_path_options(args)
        instruments = load_composition(args).instruments()
    except (WorkspaceError, ConfigError) as error:
        fail(PROG, error)
        return EXIT_BAD_CONFIG

    results = [
        check_instrument(instrument, name, RENDER_TIMEOUT_S)
        for name, instrument in instruments.items()
    ]
    if args.json:
        print(json.dumps([result_to_data(result) for result in results], indent=2))
    else:
        print(format_table(results))
    return exit_code(results)
