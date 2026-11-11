"""Sound selection filters entries, never the selected instruments' diagnostics."""

import json
from pathlib import Path

import pytest
from workspaces import make_workspace

from poser.cli.sounds import list_main
from poser.core.ports import SoundListing

CONFIG = """
[instruments.synth]
adapter = "{module}:instrument"
presets = ["lead", "pad"]
[instruments.keys]
adapter = "{module}:instrument"
presets = ["bell"]
"""


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["--match", "LEAD"], ["synth:lead"]),
        (["--match", "PRESET"], ["synth:lead", "synth:pad", "keys:bell"]),
        (["--match", "absent"], []),
        (["--match", ""], ["synth", "synth:lead", "synth:pad", "keys", "keys:bell"]),
        (
            ["--instrument", "keys", "--instrument", "synth", "--instrument", "keys"],
            ["synth", "synth:lead", "synth:pad", "keys", "keys:bell"],
        ),
    ],
)
def test_filters_keep_json_shape_and_listing_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected: list[str],
) -> None:
    make_workspace(tmp_path, monkeypatch, CONFIG)
    monkeypatch.chdir(tmp_path)
    assert list_main(["--json", *arguments]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["sound"] for row in rows] == expected
    assert all(set(row) == {"sound", "description"} for row in rows)


def test_unselected_instrument_is_not_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = make_workspace(tmp_path, monkeypatch, CONFIG)
    monkeypatch.chdir(tmp_path)

    def no_scan() -> None:
        pytest.fail("unselected instrument scanned")

    workspace.module.SETUP["keys"] = lambda instrument: setattr(instrument, "sounds", no_scan)
    assert list_main(["--instrument", "synth", "--match", "pad"]) == 0
    assert "synth:pad" in capsys.readouterr().out
    assert list_main(["--instrument", "Synth"]) == 2
    assert "unknown instrument" in capsys.readouterr().err


def test_selected_problems_survive_empty_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = make_workspace(
        tmp_path, monkeypatch, CONFIG + 'problems = ["cannot read presets"]\n'
    )
    monkeypatch.chdir(tmp_path)
    workspace.module.SETUP["keys"] = lambda instrument: setattr(
        instrument,
        "sounds",
        lambda: SoundListing((), ("cannot read presets",), ("unsupported files",)),
    )
    assert list_main(["--instrument", "keys", "--match", "absent", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []
    assert "keys: cannot read presets" in captured.err
    assert "note: keys: unsupported files" in captured.err
    assert list_main(["--instrument", "synth", "--match", "absent"]) == 0
