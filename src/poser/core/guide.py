"""Compose the generated POSER.md reference from core, instrument, and adapter guidance."""

from collections.abc import Sequence
from dataclasses import dataclass

from poser.core.names import Identifier

GENERATED = (
    "<!-- Written by `uv run update-guide` from poser and poser.toml. "
    "Edit AGENTS.md for studio instructions. -->"
)
INSTRUMENTS_HEADING = "## Instruments here"
FIRST_INSTRUMENT_PLACEHOLDER = '"{first_instrument}"'


@dataclass(frozen=True)
class InstrumentNote:
    name: Identifier
    adapter: str  # The short name or import path from poser.toml.
    guide: str | None


def _instrument_item(note: InstrumentNote) -> str:
    item = f"- `{note.name}` ({note.adapter})"
    if note.guide is None or not note.guide.strip():
        return item
    first, *rest = note.guide.strip().splitlines()
    lines = [f"{item}: {first}"]
    lines += [f"  {line}" if line.strip() else "" for line in rest]
    return "\n".join(lines)


def compose_guide(core: str, instruments: Sequence[InstrumentNote], parts: Sequence[str]) -> str:
    """Return the workspace guide: GENERATED, the core guide, the instruments, then each part.

    Blocks are separated by single blank lines, and the guide ends in one newline. The core
    guide's `"{first_instrument}"` placeholder becomes the first instrument's name, in quotes;
    with no instruments it stays as written. A part equal to an earlier one is skipped, so two
    instruments of one adapter add its part once. Raises ValueError for a part that does not
    start with a `## ` heading.
    """
    if instruments:
        core = core.replace(FIRST_INSTRUMENT_PLACEHOLDER, f'"{instruments[0].name}"')
    listing = "\n".join([INSTRUMENTS_HEADING, "", *map(_instrument_item, instruments)]).strip()
    blocks = [GENERATED, core.strip(), listing]
    seen: set[str] = set()
    for part in parts:
        text = part.strip()
        if not text.startswith("## "):
            raise ValueError(f"a guide part must start with a '## ' heading, got {text[:40]!r}")
        if text not in seen:
            seen.add(text)
            blocks.append(text)
    return "\n\n".join(blocks) + "\n"
