"""The files that make poser a releasable package: license, metadata, README, and type marker."""

import base64
import json
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from importlib import metadata
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

import pytest
import yaml
from doc_blocks import (
    ONBOARDING,
    SCENARIOS,
    Scenario,
    console_output_encoding_command,
    discover_document,
    discover_examples,
    validate_fences,
)
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

PROJECT = tomllib.loads((REPO_DIR / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_license_is_gpl_3() -> None:
    text = (REPO_DIR / "LICENSE").read_text(encoding="utf-8")

    # The GNU text centers its title, so it starts with spaces.
    assert text.lstrip().startswith("GNU GENERAL PUBLIC LICENSE")
    assert "Version 3, 29 June 2007" in text


def test_project_metadata() -> None:
    assert PROJECT["license"] == "GPL-3.0-or-later"
    assert PROJECT["license-files"] == ["LICENSE"]
    assert PROJECT["readme"] == "README.md"
    assert PROJECT["urls"]["Repository"] == "https://github.com/isnbh0/poser"


def test_readme_commands_are_project_scripts() -> None:
    commands = re.findall(r"uv run ([\w-]+)", (REPO_DIR / "README.md").read_text(encoding="utf-8"))

    assert set(commands) <= set(PROJECT["scripts"])


def test_onboarding_local_links_resolve_to_project_files() -> None:
    for name in ONBOARDING:
        document = REPO_DIR / name
        assert document.is_file(), document
        links = re.findall(r"!?\[[^]]*\]\(([^)]+)\)", document.read_text(encoding="utf-8"))
        for link in links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            target = (document.parent / unquote(parsed.path)).resolve()
            assert target.is_file(), (document, link)


def test_onboarding_commands_are_project_scripts() -> None:
    # Contributors run the test suite with pytest from the dev dependency group.
    tools = set(PROJECT["scripts"]) | {"pytest"}
    for name in ONBOARDING:
        document = REPO_DIR / name
        commands = re.findall(r"uv run ([\w-]+)", document.read_text(encoding="utf-8"))
        assert set(commands) <= tools, document


def section(document: Path, heading: str) -> str:
    """Read one level-two section, including any subordinate sections."""
    text = document.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.MULTILINE)
    assert match is not None, (document, heading)
    return re.split(r"^## ", text[match.end() :], maxsplit=1, flags=re.MULTILINE)[0]


def setup_chain() -> str:
    text = section(REPO_DIR / "docs/setup.md", "Create and verify")
    (block,) = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)
    return block


def test_all_runnable_doc_fences_have_powershell_mirrors() -> None:
    examples = discover_examples(REPO_DIR)
    assert {e.id for e in examples} == {s.id for s in SCENARIOS}


def test_inline_console_encoding_command_is_exact_and_unambiguous() -> None:
    approved = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new()"
    source = (REPO_DIR / "docs/setup.md").read_text(encoding="utf-8")
    assert source.count(f"`{approved}`") == 1
    assert console_output_encoding_command(source) == approved


def test_doc_fence_discovery_is_total() -> None:
    text = "# Guide\n```bash\nfirst &&\nsecond\n```\n```powershell\nfirst\nsecond\n```\n"
    fences = discover_document("new.md", text)
    scenario = Scenario("first", "new.md", ("Guide",))
    (example,) = validate_fences(fences, exceptions=(), scenarios=(scenario,))
    assert example.powershell == "first\nsecond\n"
    with pytest.raises(ValueError, match=r"bad.md:\d+"):
        discover_document("bad.md", "```bash\ncommand\n")  # Unclosed.
    with pytest.raises(ValueError, match=r"bad.md:\d+"):
        discover_document("bad.md", "```\ncommand\n```\n")  # Unlabelled.
    with pytest.raises(ValueError, match=r"bad.md:\d+.*mirror"):
        validate_fences(
            discover_document("bad.md", "```bash\none\n```\n"), exceptions=(), scenarios=()
        )
    pair = discover_document("x.md", "```bash\nx && y\n```\n```powershell\nx\n```\n")
    with pytest.raises(ValueError, match="command drift"):
        validate_fences(pair, exceptions=(), scenarios=(Scenario("x", "x.md", ()),))


@pytest.mark.parametrize("search", ["search-devices", "search-levels", "search-modes"])
def test_doc_search_mirrors_preserve_search_semantics(tmp_path: Path, search: str) -> None:
    examples = {example.id: example for example in discover_examples(REPO_DIR)}
    example = examples[search]
    fixtures = {
        "songs/direct file.yaml": (
            "CON\naux.txt\n'conin$'\nconout$!\nbar_dbfs\nminor major\n"
            "auxiliary recon _nul com10 lpt0 xconin$ conin$Suffix conout\n"
            "BAR_DBFS Minor MAJOR\nprefixbar_dbfssuffix\nminority\nno candidates\n"
        ),
        "songs/nested/deep file.yml": "prn; lpt9\nbar_dbfs major\nconin$Suffix\n",
        "songs/.hidden.yaml": "NUL\nminor bar_dbfs\n",
        "poser.toml": "COM1\nbar_dbfs\nmajor\n",
        "notes with spaces.txt": "aux\nbar_dbfs\nmajor\n",
    }
    expected = {
        "search-devices": {
            ("songs/direct file.yaml", 1),
            ("songs/direct file.yaml", 2),
            ("songs/direct file.yaml", 3),
            ("songs/direct file.yaml", 4),
            ("songs/nested/deep file.yml", 1),
            ("songs/.hidden.yaml", 1),
            ("poser.toml", 1),
        },
        "search-levels": {
            ("songs/direct file.yaml", 5),
            ("songs/direct file.yaml", 9),
            ("songs/nested/deep file.yml", 2),
            ("songs/.hidden.yaml", 2),
            ("poser.toml", 2),
            ("notes with spaces.txt", 2),
        },
        "search-modes": {
            ("songs/direct file.yaml", 6),
            ("songs/direct file.yaml", 10),
            ("songs/nested/deep file.yml", 2),
            ("songs/.hidden.yaml", 2),
        },
    }[search]
    for name, body in fixtures.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="\n")
    if sys.platform == "win32":
        shell = shutil.which("powershell.exe")
        assert shell is not None, "Windows PowerShell 5.1 is required, not pwsh"
        # A dotfile alone is not hidden on Windows: exercise the actual Hidden attribute too.
        attrib = shutil.which("attrib.exe")
        assert attrib is not None
        subprocess.run(
            [attrib, "+H", str(tmp_path / "songs/.hidden.yaml")],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        # Capture MatchInfo properties, never PowerShell's host-formatted console table.
        script = (
            # Module autoload can serialize first-use progress to stderr as CLIXML in 5.1.
            "$ProgressPreference = 'SilentlyContinue'\n"
            "$ErrorActionPreference = 'Stop'\n"
            "if ($PSVersionTable.PSVersion.Major -ne 5) "
            "{ throw 'requires Windows PowerShell 5.1' }\n"
            f"$matchesFound = @({example.powershell.strip()})\n"
            "$rows = @($matchesFound | ForEach-Object {\n"
            "  @{path = $_.Path; line = $_.LineNumber}\n"
            "})\nConvertTo-Json -InputObject $rows -Compress\n"
        )
        command = [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
        ]
    else:
        shell = shutil.which("bash")
        if shell is None or shutil.which("grep") is None:
            pytest.skip("documented bash search requires bash and grep")
        assert example.bash is not None
        command = [shell, "-c", example.bash]
    for positive in (True, False):
        if not positive:
            for name in fixtures:
                # r+ can overwrite a Windows Hidden file without trying to recreate it.
                with (tmp_path / name).open("r+", encoding="utf-8", newline="\n") as stream:
                    stream.write("nothing to find\n")
                    stream.truncate()
        result = subprocess.run(
            command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False
        )
        assert not result.stderr, result.stderr
        if sys.platform == "win32":
            assert result.returncode == 0  # Select-String returns success even without matches.
            rows = json.loads(result.stdout)
            actual = {
                (Path(row["path"]).relative_to(tmp_path).as_posix(), row["line"]) for row in rows
            }
        else:
            assert result.returncode == (
                0 if positive else 1
            )  # grep 1 means no matches, not error.
            actual = set()
            for line in result.stdout.splitlines():
                path, number, _ = line.split(":", 2)
                actual.add((Path(path).as_posix(), int(number)))
        assert actual == (expected if positive else set())


REQUIRED_REAL_NODES = [
    "tests/test_pedalboard_real.py::test_each_instrument_meets_the_instrument_contract",
    "tests/test_pedalboard_real.py::test_a_render_has_exactly_the_performance_s_frames",
    "tests/test_pedalboard_real.py::test_a_windows_plugin_loads_from_its_binary_as_from_its_bundle",
    "tests/test_pedalboard_real.py::test_surge_restored_preset_keeps_its_first_note",
    "tests/test_pedalboard_real.py::test_surge_factory_presets_take_effect_headlessly",
    "tests/test_pedalboard_real.py::test_each_effect_renders_as_many_frames_as_it_is_given",
    "tests/test_render_real.py::test_demo_song_renders_aligned_audible_stems",
    "tests/test_analyze_real.py::test_demo_song_mixes_and_analyzes",
    "tests/test_checking_real.py::test_each_instrument_returns_a_known_status_with_finite_numbers",
    "tests/test_songs_real.py::test_song_renders_without_defects[demo.yaml]",
    "tests/test_render_real.py::test_demo_song_renders_from_a_non_ascii_directory",
    "tests/test_pedalboard_real.py::test_a_plugin_loads_from_a_non_ascii_path",
    "tests/test_pedalboard_real.py::test_a_captured_state_reaches_the_render",
    "tests/test_render_real.py::test_mod_wheel_changes_a_preset_that_maps_it",
    "tests/test_render_real.py::test_controller_change_lands_within_a_block_of_its_time",
]


@pytest.mark.parametrize("job", ["windows", "windows-py314"])
def test_windows_jobs_require_the_same_real_plugin_nodes(job: str) -> None:
    """Both Windows jobs run every required real-plugin node; none drops out of a list silently."""
    workflow = yaml.safe_load(
        (REPO_DIR / ".github/workflows/check.yml").read_text(encoding="utf-8")
    )
    node = re.compile(r'"(tests/test_\w+_real\.py::[\w\[\].]+)"')
    # The real-plugin step is the one that lists the most nodes; others name one or two.
    listed = max(
        (set(node.findall(step.get("run", ""))) for step in workflow["jobs"][job]["steps"]),
        key=len,
    )
    assert listed == set(REQUIRED_REAL_NODES)


def test_windows_long_paths_command_matches_the_documented_command() -> None:
    """The enable branch executes the documented command, not a CI-only paraphrase."""
    prerequisites = section(REPO_DIR / "docs/setup.md", "Prerequisites")
    (documented,) = re.findall(r"```powershell\n(.*?)```", prerequisites, re.DOTALL)
    workflow = yaml.safe_load(
        (REPO_DIR / ".github/workflows/check.yml").read_text(encoding="utf-8")
    )
    (step,) = [
        step
        for step in workflow["jobs"]["windows"]["steps"]
        if step.get("name") == "Long paths off and on (required)"
    ]
    (enable,) = re.findall(
        r"(?m)^ *if \(\$Value -eq 1\) \{\n(.*?)^ *\} else \{", step["run"], re.DOTALL
    )
    commands = [
        line.strip()
        for line in enable.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert commands == documented.strip().splitlines()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="runs the bash block with /bin/sh; CI runs the PowerShell block on Windows",
)
@pytest.mark.parametrize(
    "failure",
    [
        "init",
        "cd",
        "python",
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
    owned.write_text("owner's existing song\n", encoding="utf-8", newline="\n")
    config = studio / "poser.toml"
    config.write_text("# owner's configuration\n", encoding="utf-8", newline="\n")
    before = {path: path.read_bytes() for path in (owned, config)}
    fake_uv = """uv() {
    printf 'uv %s\\n' "$*"
    case "$1" in
        init|python|add) step=$1 ;;
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
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    lines = result.stdout.splitlines()
    steps = [
        "init",
        "python",
        "add",
        "init-workspace",
        "check-instruments",
        "render-song",
        "analyze-song",
    ]
    stop = {
        "init": 1,
        "cd": 1,
        "python": 2,
        "add": 3,
        "init-workspace": 4,
        "check-instruments": 5,
        "render-song": 6,
        "analyze-song": 7,
        "none": 7,
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

    text = (REPO_DIR / "packages/poser-scsynth/README.md").read_text(encoding="utf-8")
    example, windows = re.findall(r"```toml\n(.*?)```", text, re.DOTALL)
    options = tomllib.loads(example)["instruments"]["sc"]
    assert options.pop("adapter") == "poser_scsynth.instrument:create"
    config = from_options("sc", options, tmp_path)
    assert config.scsynth is not None
    assert config.scsynth.name == "scsynth"
    # The Windows table selects the same adapter, and its path keeps its backslashes and names an
    # absolute path there.
    windows_options = tomllib.loads(windows)["instruments"]["sc"]
    assert windows_options["adapter"] == "poser_scsynth.instrument:create"
    engine = PureWindowsPath(windows_options["scsynth"])
    assert engine.is_absolute()
    assert engine.parts[-2:] == ("SuperCollider-3.14.1", "scsynth.exe")


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
