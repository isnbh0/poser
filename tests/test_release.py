"""The files that make poser a releasable package: license, metadata, README, and type marker."""

import re
import shlex
import subprocess
import tomllib
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

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

    assert set(commands) == set(PROJECT["scripts"])
    assert {"check-instruments", "update-guide"} <= set(commands)


ONBOARDING = (
    REPO_DIR / "README.md",
    REPO_DIR / "docs/setup.md",
    REPO_DIR / "docs/bootstrap.md",
    REPO_DIR / "src/poser/reference_setups/surge/README.md",
    REPO_DIR / "packages/poser-scsynth/README.md",
)


def test_onboarding_local_links_resolve_to_project_files() -> None:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],  # noqa: S607
        cwd=REPO_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    project_files = set(result.stdout.splitlines())
    for document in ONBOARDING:
        links = re.findall(r"!?\[[^]]*\]\(([^)]+)\)", document.read_text())
        for link in links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (document.parent / unquote(parsed.path)).resolve()
            assert target.is_file(), (document, link)
            assert str(target.relative_to(REPO_DIR)) in project_files, (document, link)


def test_onboarding_commands_and_shared_guide_link() -> None:
    setup = (REPO_DIR / "docs/setup.md").read_text()
    bootstrap = (REPO_DIR / "docs/bootstrap.md").read_text()
    assert PROJECT["urls"]["Repository"] + "/blob/main/docs/setup.md" in bootstrap
    assert "../src/poser/reference_setups/surge/README.md" in setup
    for document in ONBOARDING:
        commands = re.findall(r"uv run ([\w-]+)", document.read_text())
        assert set(commands) <= set(PROJECT["scripts"]), document
    blocks = re.findall(r"```bash\n(.*?)```", setup, re.DOTALL)
    assert len(blocks) == 1
    lexer = shlex.shlex(blocks[0], posix=True, punctuation_chars="&")
    lexer.whitespace_split = True
    tokens = list(lexer)
    commands: list[list[str]] = [[]]
    for part in tokens:
        if part == "&&":
            commands.append([])
        else:
            commands[-1].append(part)
    assert ["uv", "run", "init-workspace", "--setup", "surge"] in commands
    for name in ("check-instruments", "render-song", "analyze-song"):
        assert any(command[:3] == ["uv", "run", name] for command in commands)


def test_setup_command_chain_stops_and_retains_working_directory(tmp_path: Path) -> None:
    setup = (REPO_DIR / "docs/setup.md").read_text()
    (block,) = re.findall(r"```bash\n(.*?)```", setup, re.DOTALL)
    fake_uv = """uv() {
    printf 'uv %s\\n' "$*"
    if [ "$1" = init ]; then
        [ "$1" = "$FAIL" ] && return 1
        [ "$FAIL" = cd ] || mkdir my-studio
    fi
    [ "$2" = init-workspace ] && [ "$FAIL" = workspace ] && return 1
    return 0
}
"""
    for failure in ("init", "cd", "workspace", "none"):
        parent = tmp_path / failure
        parent.mkdir()
        script = f"FAIL={failure}\n{fake_uv}\n{block}\nprintf 'cwd=%s\\n' \"$PWD\"\n"
        result = subprocess.run(
            ["/bin/sh", "-c", script], cwd=parent, capture_output=True, text=True, check=True
        )
        lines = result.stdout.splitlines()
        in_studio = failure in ("workspace", "none")
        assert lines[-1] == f"cwd={parent / 'my-studio' if in_studio else parent}"
        assert any("check-instruments" in line for line in lines) == (failure == "none")
        assert any("uv add" in line for line in lines) == in_studio


def test_scsynth_readme_config_supplies_engine_path(tmp_path: Path) -> None:
    from poser_scsynth.config import from_options

    text = (REPO_DIR / "packages/poser-scsynth/README.md").read_text()
    (example,) = re.findall(r"```toml\n(.*?)```", text, re.DOTALL)
    options = tomllib.loads(example)["instruments"]["sc"]
    assert options.pop("adapter") == "poser_scsynth.instrument:create"
    config = from_options("sc", options, tmp_path)
    assert config.scsynth is not None
    assert config.scsynth.name == "scsynth"


def test_installed_entry_points_load() -> None:
    scripts = [
        entry
        for entry in metadata.distribution("poser").entry_points
        if entry.group == "console_scripts"
    ]

    assert {entry.name for entry in scripts} == set(PROJECT["scripts"])
    for entry in scripts:
        assert callable(entry.load()), entry.name


def test_makefile_checks_instruments() -> None:
    targets = re.findall(r"^([\w-]+):", (REPO_DIR / "Makefile").read_text(), re.MULTILINE)

    assert "check-instruments" in targets
    assert "check-plugins" not in targets


def test_packaged_reference_and_neutral_resources() -> None:
    surge = files("poser.reference_setups.surge")
    for name in ("poser.toml", "demo.yaml", "README.md"):
        assert surge.joinpath(name).is_file(), name
    scaffold = files("poser").joinpath("scaffold")
    for name in ("guide.md", "agents.md", "claude.md", "gitignore"):
        assert scaffold.joinpath(name).is_file(), name
    for name in ("poser.toml", "demo.yaml", "onboarding.txt"):
        assert not scaffold.joinpath(name).is_file(), name


def test_package_is_marked_typed() -> None:
    assert (REPO_DIR / "src" / "poser" / "py.typed").is_file()


def test_scsynth_distribution_declares_its_host_and_runtime_libraries() -> None:
    requirements = metadata.requires("poser-scsynth")
    assert requirements is not None
    assert "poser==0.1.0" in requirements
    assert any(requirement.startswith("numpy>=") for requirement in requirements)
    assert any(requirement.startswith("scipy>=") for requirement in requirements)
