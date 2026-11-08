"""Exit codes, table layout, and timeouts shared by poser's commands."""

from collections.abc import Sequence

EXIT_ALL_PASS = 0
EXIT_SOME_FAILED = 1
EXIT_BAD_CONFIG = 2
# Another render-song or analyze-song holds the song's lock.
EXIT_SONG_BUSY = 3

# How long one worker may run a plugin before it counts as timed out.
RENDER_TIMEOUT_S = 120.0


def align_rows(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    ]
