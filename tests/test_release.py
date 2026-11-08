"""The files that make poser a releasable package: license, metadata, README, and type marker."""

import re
import tomllib

from helpers import REPO_DIR

PROJECT = tomllib.loads((REPO_DIR / "pyproject.toml").read_text())["project"]


def test_license_is_gpl_3() -> None:
    text = (REPO_DIR / "LICENSE").read_text()

    # The GNU text centers its title, so it starts with spaces.
    assert text.lstrip().startswith("GNU GENERAL PUBLIC LICENSE")
    assert "Version 3, 29 June 2007" in text


def test_project_metadata() -> None:
    assert PROJECT["license"] == "GPL-3.0-or-later"
    assert PROJECT["license-files"] == ["LICENSE"]
    assert PROJECT["readme"] == "README.md"
    assert PROJECT["urls"]["Repository"] == "https://github.com/isnbh0/poser"


def test_readme_commands_are_project_scripts() -> None:
    commands = re.findall(r"uv run ([\w-]+)", (REPO_DIR / "README.md").read_text())

    assert set(commands) <= set(PROJECT["scripts"])
    assert "init-workspace" in commands


def test_package_is_marked_typed() -> None:
    assert (REPO_DIR / "src" / "poser" / "py.typed").is_file()
