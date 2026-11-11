"""Tests for `compose_guide`, which builds a studio's `POSER.md` from its parts."""

import pytest

from poser.core.guide import (
    FIRST_INSTRUMENT_PLACEHOLDER,
    GENERATED,
    INSTRUMENTS_HEADING,
    InstrumentNote,
    compose_guide,
)
from poser.core.names import Identifier

CORE = "# Composing\n\nWrite songs.\n\n"
SERUM = InstrumentNote(Identifier("serum"), "pedalboard", "A wavetable synth.")
PLAIN = InstrumentNote(Identifier("drums"), "my_adapters:drums", None)


def test_blocks_come_in_order_with_one_blank_line_between() -> None:
    guide = compose_guide(CORE, [SERUM, PLAIN], ["## Store\n\nFiles.\n", "\n## Plots\n\nPNGs."])

    assert guide == (
        f"{GENERATED}\nAdvice: on\n"
        "\n"
        "# Composing\n\nWrite songs.\n"
        "\n"
        f"{INSTRUMENTS_HEADING}\n"
        "\n"
        "- `serum` (pedalboard): A wavetable synth.\n"
        "- `drums` (my_adapters:drums)\n"
        "\n"
        "## Store\n\nFiles.\n"
        "\n"
        "## Plots\n\nPNGs.\n"
    )
    assert guide.endswith("PNGs.\n")
    assert not guide.endswith("\n\n")


def test_a_repeated_part_is_added_once() -> None:
    guide = compose_guide(CORE, [SERUM], ["## Store\n", "## Plots\n", "## Store\n\n"])

    assert guide.count("## Store") == 1
    assert guide.index("## Store") < guide.index("## Plots")


def test_a_multi_line_note_is_indented_under_its_item() -> None:
    note = InstrumentNote(Identifier("serum"), "pedalboard", "First line.\n\nMore.\n  Deeper.\n")

    guide = compose_guide(CORE, [note], [])

    assert guide.endswith(
        f"{INSTRUMENTS_HEADING}\n\n- `serum` (pedalboard): First line.\n\n  More.\n    Deeper.\n"
    )


def test_no_instruments_leaves_the_heading() -> None:
    assert compose_guide(CORE, [], []).endswith(f"Write songs.\n\n{INSTRUMENTS_HEADING}\n")


def test_the_first_instrument_fills_the_core_placeholder() -> None:
    guide = compose_guide('sound="{first_instrument}"\n', [SERUM], [])

    assert 'sound="serum"' in guide


def test_the_placeholder_takes_the_first_instrument_in_config_order() -> None:
    guide = compose_guide('sound="{first_instrument}"\n', [PLAIN, SERUM], [])

    assert 'sound="drums"' in guide
    assert 'sound="serum"' not in guide
    assert FIRST_INSTRUMENT_PLACEHOLDER not in guide


def test_no_instruments_leaves_the_placeholder() -> None:
    guide = compose_guide('sound="{first_instrument}"\n', [], [])

    assert 'sound="{first_instrument}"' in guide


@pytest.mark.parametrize("part", ["Store\n", "# Store\n", "### Store\n", ""])
def test_a_part_without_a_section_heading_is_refused(part: str) -> None:
    with pytest.raises(ValueError, match="must start with a '## ' heading"):
        compose_guide(CORE, [SERUM], [part])
