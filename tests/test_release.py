"""The files that make poser a releasable package: license, metadata, README, and type marker."""

import hashlib
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

from poser.core.report import BANDS, AnalysisStatus, MixReport, NodeReport, Report, report_to_data

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
    REPO_DIR / "docs/publication-checks.md",
    REPO_DIR / "docs/publication-evidence.md",
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
    commands = [
        "uv init --bare --python 3.13 my-studio",
        "uv add git+https://github.com/isnbh0/poser",
        "uv run init-workspace --setup surge",
        "uv run check-instruments",
        "uv run render-song songs/demo.yaml",
        "uv run analyze-song songs/demo.yaml",
    ]
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
    assert lines[:-2] == commands[:stop]
    assert lines[-2] == f"status={0 if failure == 'none' else 1}"
    assert lines[-1] == f"cwd={tmp_path if failure in ('init', 'cd') else studio}"
    assert {path: path.read_bytes() for path in before} == before


def test_readme_analysis_example_matches_serialized_report() -> None:
    demo = section(REPO_DIR / "README.md", "Surge demo")
    (example,) = re.findall(r"```json\n(.*?)```", demo, re.DOTALL)
    documented = json.loads(example)
    bands = dict.fromkeys(BANDS, -30.0)
    node = NodeReport(
        name="lead",
        status=AnalysisStatus.ANALYZED,
        node_key="0" * 64,
        sound_key="1" * 64,
        kind="track",
        upstream=(),
        peak_dbfs=-6.0,
        integrated_lufs=-14.0,
        clipped_samples=0,
        bands_db=bands,
        bar_dbfs=(-12.0,),
        spectrogram="lead.png",
    )
    mix = MixReport(
        path="mix.wav",
        peak_dbfs=-6.0,
        integrated_lufs=-14.0,
        clipped_samples=0,
        bands_db=bands,
        bar_dbfs=(-12.0,),
        spectrogram="mix.png",
        defects=(),
    )
    serialized = report_to_data(Report("example", 1, "2" * 64, (node,), mix))
    assert isinstance(serialized, dict)
    assert isinstance(documented, dict)
    assert "nodes" in documented
    assert "tracks" not in documented
    assert set(documented) <= set(serialized)
    serialized_nodes = serialized["nodes"]
    assert isinstance(serialized_nodes, list)
    serialized_node = serialized_nodes[0]
    assert isinstance(serialized_node, dict)
    assert isinstance(documented["nodes"], list)
    assert documented["nodes"]
    for documented_node in documented["nodes"]:
        assert isinstance(documented_node, dict)
        assert set(documented_node) <= set(serialized_node)
        assert documented_node["status"] in {status.value for status in AnalysisStatus}
    serialized_mix = serialized["mix"]
    assert isinstance(serialized_mix, dict)
    assert isinstance(documented["mix"], dict)
    assert set(documented["mix"]) <= set(serialized_mix)


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


def test_showcase_source_and_preview_match_recorded_hashes() -> None:
    evidence = json.loads((REPO_DIR / "docs/audio/surge-showcase.json").read_text())
    for field in ("source", "preview"):
        path = REPO_DIR / evidence[field]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence[f"{field}_sha256"]


def test_package_is_marked_typed() -> None:
    assert (REPO_DIR / "src" / "poser" / "py.typed").is_file()


def test_scsynth_distribution_declares_its_host_and_runtime_libraries() -> None:
    requirements = metadata.requires("poser-scsynth")
    assert requirements is not None
    assert "poser==0.1.0" in requirements
    assert any(requirement.startswith("numpy>=") for requirement in requirements)
    assert any(requirement.startswith("scipy>=") for requirement in requirements)
