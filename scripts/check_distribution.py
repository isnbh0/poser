"""Plugin-free packaging smoke test, not real installation or audio acceptance.

Run with a freshly installed environment's Python -I, from outside the checkout.
The empty VST3 directory is only a path fixture; no command loads a plugin.
"""

import importlib
import os
import subprocess
import sys
import sysconfig
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path

# A release script that runs outside any studio, so no scratch port exists here.
from tempfile import TemporaryDirectory  # noqa: TID251


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(name: str, *args: str, cwd: Path) -> str:
    """Run the installed console script `name` as a shell would: a launcher `.exe` on Windows and
    a script with a shebang elsewhere, found in this environment's scripts folder."""
    scripts = Path(sysconfig.get_path("scripts"))
    # Built rather than searched for: on Windows `shutil.which` looks in the working directory
    # first, even when given a path.
    executable = scripts / (f"{name}.exe" if sys.platform == "win32" else name)
    require(executable.is_file(), f"No console script {name} in {scripts}")
    # The script's interpreter runs without -I, so drop the variables that would redirect its
    # imports or set its encoding: each command writes the pipe as UTF-8 itself.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    result = subprocess.run(
        [str(executable), *args],
        cwd=cwd,
        env=environment,
        check=False,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(f"$ {name} {' '.join(args)}\n{result.stdout}", flush=True)
    result.check_returncode()
    return result.stdout


def main() -> None:
    checkout = Path(__file__).resolve().parents[1]
    require(bool(sys.flags.isolated), "Run this script with Python -I")
    require(sys.prefix != sys.base_prefix, "Use a fresh virtual environment")
    require(not Path.cwd().resolve().is_relative_to(checkout), "Run outside the checkout")
    module = importlib.import_module("poser")
    origin = module.__file__
    if origin is None:
        raise RuntimeError("Missing poser module file")
    installed = Path(origin).resolve()
    require(installed.is_relative_to(Path(sys.prefix).resolve()), "poser is outside the venv")
    require(not installed.is_relative_to(checkout), "poser imported from the checkout")
    print(f"Plugin-free packaging smoke (not audio acceptance): {installed}", flush=True)

    resources = files("poser")
    for resource in (
        "scaffold/agents.md",
        "scaffold/claude.md",
        "scaffold/gitignore",
        "scaffold/guide.md",
        "scaffold/advice.md",
        "reference_setups/surge/README.md",
        "reference_setups/surge/poser.toml",
        "reference_setups/surge/demo.yaml",
        "adapters/pedalboard/guide.md",
    ):
        require(bool(resources.joinpath(resource).read_bytes()), f"Missing resource: {resource}")

    with TemporaryDirectory(prefix="poser-distribution-") as directory:
        scratch = Path(directory)
        entries = [
            entry
            for entry in distribution("poser").entry_points
            if entry.group == "console_scripts"
        ]
        require(bool(entries), "No console entrypoints installed")
        for entry in entries:
            require(callable(entry.load()), f"Entrypoint is not callable: {entry.name}")
            run(entry.name, "--help", cwd=scratch)

        # Deliberately invalid as an instrument: these directories only test path handling.
        plugin = scratch / "fixture-only.vst3"
        presets = scratch / "empty-presets"
        plugin.mkdir()
        presets.mkdir()
        guides: dict[str, str] = {}
        for advice in ("off", "on"):
            studio = scratch / f"studio-{advice}"
            run(
                "init-workspace",
                str(studio),
                "--setup",
                "surge",
                "--plugin-path",
                str(plugin),
                "--presets-path",
                str(presets),
                "--advice",
                advice,
                cwd=scratch,
            )
            run("update-guide", "--check", cwd=studio)
            listing = run("list-sounds", cwd=studio)
            require("surge" in listing, "Default sound missing")
            output = studio / "demo.png"
            run("piano-roll", "songs/demo.yaml", "--output", str(output), cwd=studio)
            require(output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), "Missing PNG output")
            guides[advice] = (studio / "POSER.md").read_text(encoding="utf-8")
        require(guides["off"] != guides["on"], "Advice did not change the guide")
    print("Plugin-free distribution smoke passed; no plugin or audio was tested.")


if __name__ == "__main__":
    main()
