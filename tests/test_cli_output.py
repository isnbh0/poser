"""Every command writes its output as UTF-8, whatever the platform's code page. On Windows, a
redirected stdout otherwise uses the ANSI code page, and text outside it fails to encode."""

import os
import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path

from poser.cli.common import COMMANDS

PROJECT = tomllib.loads(
    (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
)["project"]
MARK = "café 한글"


def test_every_console_script_is_a_command() -> None:
    scripts = PROJECT["scripts"]
    assert scripts
    for name, target in scripts.items():
        module, function = target.split(":")
        assert getattr(import_module(module), function) in COMMANDS, name


def test_read_song_prints_utf8_under_a_narrow_code_page(tmp_path: Path) -> None:
    """`PYTHONIOENCODING=ascii` stands in, on every platform, for a code page without the mark's
    characters."""
    config = tmp_path / "poser.toml"
    config.write_text(
        "[instruments.x]\nadapter = 'pedalboard'\npath = '/nonexistent/X.vst3'\n",
        encoding="utf-8",
        newline="\n",
    )
    song = tmp_path / "song.yaml"
    song.write_text(
        "bpm: 120\n"
        f"marks:\n  parts:\n    - {{beat: 0, end: 4, text: {MARK}}}\n"
        "tracks:\n"
        "  - {name: keys, sound: x, notes: [{beat: 0, pitch: 60, length: 1, velocity: 100}]}\n",
        encoding="utf-8",
        newline="\n",
    )
    entry = "import sys; from poser.cli.read import main; raise SystemExit(main(sys.argv[1:]))"
    result = subprocess.run(
        [sys.executable, "-c", entry, str(song), "--config", str(config)],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert f"beats 0-1: {MARK}\n" in result.stdout.decode("utf-8")
