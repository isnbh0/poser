"""Tests for `compose_guide`, which builds a studio's `POSER.md` from its parts."""

from pathlib import Path

import pytest
from workspaces import make_workspace

from poser.adapters.chords import GUIDE as CHORDS_GUIDE
from poser.adapters.tonal import GUIDE as TONAL_GUIDE
from poser.adapters.tonal import KEY_GUIDE
from poser.compose import Composition, load
from poser.core.guide import (
    FIRST_INSTRUMENT_PLACEHOLDER,
    FRAMEWORKS_HEADING,
    GENERATED,
    INSTRUMENTS_HEADING,
    FrameworkListing,
    InstrumentNote,
    compose_guide,
)
from poser.core.names import Identifier

CORE = "# Composing\n\nWrite songs.\n\n"
SERUM = InstrumentNote(Identifier("serum"), "pedalboard", "A wavetable synth.")
PLAIN = InstrumentNote(Identifier("drums"), "my_adapters:drums", None)


def required_guide(composition: Composition) -> tuple[str, ...]:
    """The text of the guide's required blocks: every block but advice."""
    return tuple(block.text for block in composition.guide_blocks() if not block.advice)


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


TONAL = (Identifier("tonal"),)
CHORDS = (Identifier("chords"),)
WESTERN_VOCABULARIES = (Identifier("chords"), Identifier("key"))
NO_VOCABULARIES = "No vocabularies are configured; every mark layer is text."


@pytest.mark.parametrize(
    ("listing", "text"),
    [
        (
            FrameworkListing(
                "western",
                TONAL,
                default=True,
                vocabulary_preset="western",
                vocabularies=WESTERN_VOCABULARIES,
                vocabulary_default=True,
            ),
            (
                "Preset: `western` (the default), poser's current recommendation. "
                "Frameworks: `tonal`. To pin them, list them in `[adapters] frameworks`. "
                "Vocabularies: `chords`, `key`, from the `western` preset (the default); to pin "
                "them, list them in `[adapters] vocabularies`."
            ),
        ),
        (
            FrameworkListing("neutral", (), vocabulary_preset="neutral"),
            (
                "Preset: `neutral`. No frameworks are configured; `read-song` reads nothing. "
                + NO_VOCABULARIES
            ),
        ),
        (
            FrameworkListing(None, TONAL, vocabularies=CHORDS),
            (
                "Frameworks: `tonal` (listed in poser.toml). Vocabularies: `chords` (listed in "
                "poser.toml)."
            ),
        ),
        (
            FrameworkListing(None, TONAL, vocabulary_preset="neutral"),
            "Frameworks: `tonal` (listed in poser.toml). " + NO_VOCABULARIES,
        ),
    ],
    ids=["default-preset", "neutral", "listed", "listed-frameworks-neutral-vocabularies"],
)
def test_the_frameworks_block_follows_the_instruments(listing: FrameworkListing, text: str) -> None:
    guide = compose_guide(CORE, [SERUM], ["## Store\n"], frameworks=listing)

    assert f"{INSTRUMENTS_HEADING}\n\n- `serum` (pedalboard): A wavetable synth.\n\n" in guide
    assert f"A wavetable synth.\n\n{FRAMEWORKS_HEADING}\n\n{text}\n\n## Store\n" in guide


def test_no_listing_leaves_out_the_frameworks_block() -> None:
    assert FRAMEWORKS_HEADING not in compose_guide(CORE, [SERUM], [])


@pytest.mark.parametrize(
    ("setting", "has_tonal"),
    [("", True), ('frameworks = "neutral"\n', False), ('frameworks = ["tonal"]\n', True)],
    ids=["default", "neutral", "listed"],
)
def test_the_tonal_guide_comes_with_the_tonal_framework(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, has_tonal: bool
) -> None:
    workspace = make_workspace(tmp_path / "studio", monkeypatch)
    if setting:
        workspace.write_config(workspace.config.read_text() + "[adapters]\n" + setting)
    composition = load(workspace.config, workspace.root / "sounds", workspace.renders)

    assert (TONAL_GUIDE in required_guide(composition)) is has_tonal


@pytest.mark.parametrize(
    ("setting", "has_chords"),
    [("", True), ('vocabularies = "neutral"\n', False), ('vocabularies = ["chords"]\n', True)],
    ids=["default", "neutral", "listed"],
)
def test_the_chords_guide_comes_with_the_chords_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, has_chords: bool
) -> None:
    workspace = make_workspace(tmp_path / "studio", monkeypatch)
    if setting:
        workspace.write_config(workspace.config.read_text() + "[adapters]\n" + setting)
    composition = load(workspace.config, workspace.root / "sounds", workspace.renders)

    assert (CHORDS_GUIDE in required_guide(composition)) is has_chords
    listing = composition.framework_listing()
    assert (
        listing.vocabularies
        == {
            "": WESTERN_VOCABULARIES,
            'vocabularies = "neutral"\n': (),
            'vocabularies = ["chords"]\n': CHORDS,
        }[setting]
    )


@pytest.mark.parametrize(
    ("setting", "has_key"),
    [
        ("", True),
        ('vocabularies = "neutral"\n', False),
        ('vocabularies = ["chords"]\n', False),
        ('vocabularies = ["key"]\n', True),
    ],
    ids=["default", "neutral", "chords-only", "key-only"],
)
def test_the_key_guide_comes_with_the_key_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, has_key: bool
) -> None:
    workspace = make_workspace(tmp_path / "studio", monkeypatch)
    if setting:
        workspace.write_config(workspace.config.read_text() + "[adapters]\n" + setting)
    composition = load(workspace.config, workspace.root / "sounds", workspace.renders)

    assert (KEY_GUIDE in required_guide(composition)) is has_key
    assert (Identifier("key") in composition.framework_listing().vocabularies) is has_key
