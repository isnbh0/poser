"""The files that make poser a releasable package: license, metadata, README, and type marker."""

import json
import re
import shlex
import subprocess
import tomllib
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from helpers import REPO_DIR

from poser.core.report import (
    BANDS,
    AnalyzedNode,
    CompleteReport,
    LevelSpan,
    Measurements,
    MixReport,
    report_to_data,
)

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


ONBOARDING = (
    REPO_DIR / "README.md",
    REPO_DIR / "docs/setup.md",
    REPO_DIR / "docs/bootstrap.md",
    REPO_DIR / "docs/ablation.md",
    REPO_DIR / "docs/showcase.md",
    REPO_DIR / "src/poser/reference_setups/surge/README.md",
    REPO_DIR / "packages/poser-scsynth/README.md",
)


def test_onboarding_local_links_resolve_to_project_files() -> None:
    for document in ONBOARDING:
        assert document.is_file(), document
        links = re.findall(r"!?\[[^]]*\]\(([^)]+)\)", document.read_text())
        for link in links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (document.parent / unquote(parsed.path)).resolve()
            assert target.is_file(), (document, link)


def test_onboarding_commands_are_project_scripts() -> None:
    for document in ONBOARDING:
        commands = re.findall(r"uv run ([\w-]+)", document.read_text())
        assert set(commands) <= set(PROJECT["scripts"]), document


def section(document: Path, heading: str) -> str:
    """Read one level-two section, including any subordinate sections."""
    text = document.read_text()
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.MULTILINE)
    assert match is not None, (document, heading)
    return re.split(r"^## ", text[match.end() :], maxsplit=1, flags=re.MULTILINE)[0]


def setup_chain() -> str:
    text = section(REPO_DIR / "docs/setup.md", "Create and verify")
    (block,) = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    return block


@pytest.mark.parametrize(
    "failure",
    [
        "init",
        "cd",
        "add",
        "init-workspace",
        "check-instruments",
        "render-song",
        "analyze-song",
        "none",
    ],
)
def test_setup_command_chain_stops_and_retains_working_directory(
    tmp_path: Path, failure: str
) -> None:
    studio = tmp_path / "my-studio"
    studio.mkdir()
    owned = studio / "songs" / "existing.yaml"
    owned.parent.mkdir()
    owned.write_text("owner's existing song\n")
    config = studio / "poser.toml"
    config.write_text("# owner's configuration\n")
    before = {path: path.read_bytes() for path in (owned, config)}
    fake_uv = """uv() {
    printf 'uv %s\\n' "$*"
    case "$1" in
        init|add) step=$1 ;;
        run) step=$2 ;;
        *) return 99 ;;
    esac
    [ "$step" = "$FAIL" ] && return 1
    return 0
}
cd() {
    [ "$FAIL" = cd ] && return 1
    command cd "$@"
}
"""
    script = (
        f"FAIL={shlex.quote(failure)}\n{fake_uv}\n{setup_chain()}\n"
        'status=$?\nprintf \'status=%s\\ncwd=%s\\n\' "$status" "$PWD"\n'
    )
    result = subprocess.run(
        ["/bin/sh", "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    lines = result.stdout.splitlines()
    steps = ["init", "add", "init-workspace", "check-instruments", "render-song", "analyze-song"]
    stop = {
        "init": 1,
        "cd": 1,
        "add": 2,
        "init-workspace": 3,
        "check-instruments": 4,
        "render-song": 5,
        "analyze-song": 6,
        "none": 6,
    }[failure]
    words = [line.split() for line in lines[:-2]]
    assert [w[2] if w[1] == "run" else w[1] for w in words] == steps[:stop]
    assert lines[-2] == f"status={0 if failure == 'none' else 1}"
    assert lines[-1] == f"cwd={tmp_path if failure in ('init', 'cd') else studio}"
    assert {path: path.read_bytes() for path in before} == before


def test_readme_analysis_example_matches_serialized_report() -> None:
    demo = section(REPO_DIR / "README.md", "Surge demo")
    (example,) = re.findall(r"```json\n(.*?)```", demo, re.DOTALL)
    documented = json.loads(example)
    bands = dict.fromkeys(BANDS, -30.0)
    measurements = Measurements(-6.0, -14.0, 0, bands, (-12.0,))
    node = AnalyzedNode(
        "lead", "track", (), 0.0, "0" * 64, "1" * 64, measurements, (), "images/node-0.png"
    )
    mix = MixReport("audio/mix.wav", measurements, "images/mix.png", ())
    serialized = report_to_data(
        CompleteReport("example", 1, "2" * 64, (LevelSpan(0, 4),), (node,), mix)
    )
    assert isinstance(serialized, dict)
    assert isinstance(documented, dict)
    assert documented["status"] == serialized["status"] == "complete"
    assert set(documented) <= set(serialized)
    serialized_nodes = serialized["nodes"]
    assert isinstance(serialized_nodes, list)
    serialized_node = serialized_nodes[0]
    assert isinstance(serialized_node, dict)
    serialized_mix = serialized["mix"]
    assert isinstance(serialized_mix, dict)
    assert isinstance(documented["nodes"], list)
    assert documented["nodes"]
    pairs = [(documented["mix"], serialized_mix)]
    for documented_node in documented["nodes"]:
        assert documented_node["status"] == "analyzed"
        pairs.append((documented_node, serialized_node))
    for documented_part, serialized_part in pairs:
        assert isinstance(documented_part, dict)
        assert set(documented_part) <= set(serialized_part)
        documented_values = documented_part["measurements"]
        serialized_values = serialized_part["measurements"]
        assert isinstance(documented_values, dict)
        assert isinstance(serialized_values, dict)
        assert set(documented_values) <= set(serialized_values)


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


def test_packaged_reference_and_neutral_resources() -> None:
    surge = files("poser.reference_setups.surge")
    for name in ("poser.toml", "demo.yaml", "README.md"):
        assert surge.joinpath(name).is_file(), name
    scaffold = files("poser").joinpath("scaffold")
    for name in ("guide.md", "advice.md", "agents.md", "claude.md", "gitignore"):
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
