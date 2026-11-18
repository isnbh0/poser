"""Every command writes its output as UTF-8, whatever the platform's code page. On Windows, a
redirected stdout otherwise uses the ANSI code page, and text outside it fails to encode."""

import os
import subprocess
import sys
import tomllib
from importlib import import_module
from pathlib import Path

import pytest

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


def write_reading_fixture(directory: Path) -> tuple[Path, Path]:
    """Write a marked song and fake VST3 config; reading them never loads a plugin."""
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "poser.toml"
    config.write_text(
        "[instruments.x]\nadapter = 'pedalboard'\npath = '/nonexistent/X.vst3'\n",
        encoding="utf-8",
        newline="\n",
    )
    song = directory / "song.yaml"
    song.write_text(
        "bpm: 120\n"
        f"marks:\n  parts:\n    - {{beat: 0, end: 4, text: {MARK}}}\n"
        "tracks:\n"
        "  - {name: keys, sound: x, notes: [{beat: 0, pitch: 60, length: 1, velocity: 100}]}\n",
        encoding="utf-8",
        newline="\n",
    )
    return song, config


def _run_read_song(
    song: Path,
    config: Path,
    encoding: str,
    *,
    error: bool = False,
    undecorated: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    entrypoint = "main.__wrapped__" if undecorated else "main"
    entry = (
        "import codecs, sys\n"
        f"expected = codecs.lookup({encoding!r}).name\n"
        "for stream in (sys.stdout, sys.stderr):\n"
        "    assert codecs.lookup(stream.encoding).name == expected, stream.encoding\n"
        "assert sys.stdout.errors == 'strict', sys.stdout.errors\n"
        "assert sys.stderr.errors == 'backslashreplace', sys.stderr.errors\n"
        "from poser.cli.read import main\n"
        f"raise SystemExit({entrypoint}(sys.argv[1:]))\n"
    )
    args = [str(song), "--config", str(config)]
    if error:
        args += ["--framework", MARK]
    return subprocess.run(
        [sys.executable, "-c", entry, *args],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONIOENCODING": encoding},
    )


def _assert_utf8_reading(
    result: subprocess.CompletedProcess[bytes], *, error: bool = False
) -> None:
    stdout = result.stdout.decode("utf-8", errors="strict")
    stderr = result.stderr.decode("utf-8", errors="strict")
    assert result.returncode == (2 if error else 0), stderr
    expected = (
        f"read-song: no framework {MARK!r} is configured; configured: tonal"
        if error
        else f"  beats 0-1: {MARK}"
    )
    # Printed lines end in the platform's newline, CRLF on Windows.
    assert expected in (stderr if error else stdout).splitlines()


@pytest.mark.parametrize("encoding", ["ascii", "cp1252"])
def test_read_song_prints_utf8_under_a_narrow_code_page(tmp_path: Path, encoding: str) -> None:
    """Forced narrow codecs on redirected streams are locale stand-ins, not desktop consoles."""
    song, config = write_reading_fixture(tmp_path)
    _assert_utf8_reading(_run_read_song(song, config, encoding))


def test_read_song_errors_are_utf8_under_cp1252(tmp_path: Path) -> None:
    song, config = write_reading_fixture(tmp_path)
    _assert_utf8_reading(_run_read_song(song, config, "cp1252", error=True), error=True)


@pytest.mark.parametrize("encoding", ["ascii", "cp1252"])
@pytest.mark.parametrize("error", [False, True], ids=["stdout", "stderr"])
def test_console_utf8_oracles_reject_undecorated_commands(
    tmp_path: Path, encoding: str, error: bool
) -> None:
    song, config = write_reading_fixture(tmp_path)
    _assert_utf8_reading(_run_read_song(song, config, encoding, error=error), error=error)
    result = _run_read_song(song, config, encoding, error=error, undecorated=True)
    if error:
        assert result.returncode == 2
        expected = f"read-song: no framework {MARK!r} is configured; configured: tonal"
        assert result.stderr.splitlines() == [expected.encode(encoding, errors="backslashreplace")]
        assert rb"\ud55c\uae00" in result.stderr
    else:
        assert result.returncode == 1
        # The actual marks print failed to encode, not an import or configuration operation.
        assert b"UnicodeEncodeError:" in result.stderr
        assert b"print(format_marks(authored, bars))" in result.stderr
    with pytest.raises((AssertionError, UnicodeDecodeError)):
        _assert_utf8_reading(result, error=error)
