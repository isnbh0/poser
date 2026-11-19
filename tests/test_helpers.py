"""Tests for the shared test helpers in `tests/helpers.py` that need no plugins."""

from pathlib import Path

import pytest
from helpers import require_instrument

from poser.compose import load

CONFIG = """
[instruments.surge]
adapter = "pedalboard"
path = "/Plugins/Surge XT.vst3"
"""


def test_require_instrument_returns_the_named_entry_or_skips(tmp_path: Path) -> None:
    config = tmp_path / "poser.toml"
    config.write_text(CONFIG, encoding="utf-8", newline="\n")
    composition = load(config, tmp_path / "sounds", tmp_path / "renders")

    (surge,) = composition.config.instruments
    assert require_instrument(composition, "surge") is surge
    with pytest.raises(pytest.skip.Exception, match="'serum'"):
        require_instrument(composition, "serum")
