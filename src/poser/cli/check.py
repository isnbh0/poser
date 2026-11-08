"""`check-instruments`: check that each instrument in poser.toml loads, sounds, and repeats.

Each instrument renders a short pattern twice. A plugin that crashes, hangs, or opens a license
dialog fails its own row without stopping the others.
"""

import argparse
import json
from dataclasses import asdict
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
from poser.core.checks import CheckStatus
from poser.core.ports import ConfigError

PROG = "check-instruments"
YES_NO = {None: "-", True: "yes", False: "no"}


def exit_code(results: list[CheckResult]) -> int:
    all_pass = all(result.status is CheckStatus.PASS for result in results)
    return EXIT_ALL_PASS if all_pass else EXIT_SOME_FAILED


def format_table(results: list[CheckResult]) -> str:
    rows = [("INSTRUMENT", "ADAPTER", "STATUS", "PEAK DBFS", "REPEATABLE")]
    details = []
    for result in results:
        peak = "-" if result.peak_dbfs is None else f"{result.peak_dbfs:.1f}"
        rows.append(
            (result.name, result.adapter, str(result.status), peak, YES_NO[result.repeatable])
        )
        if result.detail:
            details.append(f"{result.name}: {result.detail}")
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
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        print(format_table(results))
    return exit_code(results)
