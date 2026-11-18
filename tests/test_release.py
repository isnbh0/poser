"""The files that make poser a releasable package: license, metadata, README, and type marker."""

import base64
import json
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import asdict, replace
from importlib import metadata
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote, urlsplit

import pytest
import yaml
from doc_blocks import (
    EXCEPTIONS,
    ONBOARDING,
    SCENARIOS,
    Scenario,
    console_output_encoding_command,
    corpus_paths,
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
    for name in ONBOARDING:
        document = REPO_DIR / name
        commands = re.findall(r"uv run ([\w-]+)", document.read_text(encoding="utf-8"))
        assert set(commands) <= set(PROJECT["scripts"]), document


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


@pytest.mark.parametrize(
    ("document", "text"),
    [
        ("setup", section(REPO_DIR / "docs/setup.md", "Create and verify")),
        ("upgrade", (REPO_DIR / "docs/upgrading.md").read_text(encoding="utf-8").split("\n## ")[0]),
    ],
)
def test_powershell_blocks_mirror_the_bash_chains(document: str, text: str) -> None:
    """Each `&&` chain a user runs has a PowerShell block of the same commands, one per line,
    since Windows PowerShell 5.1 has no `&&`. CI runs the PowerShell blocks on Windows."""
    bash = re.findall(r"```bash\n(.*?)```", text, re.DOTALL)[0]
    powershell = re.findall(r"```powershell\n(.*?)```", text, re.DOTALL)[0]
    assert powershell.splitlines() == [line.removesuffix(" &&") for line in bash.splitlines()], (
        document
    )


def test_all_runnable_doc_fences_have_powershell_mirrors() -> None:
    paths = corpus_paths(REPO_DIR)
    assert paths == tuple(sorted(set(paths)))
    assert set(REPO_DIR.glob("docs/*.md")) <= set(paths)
    assert {REPO_DIR / name for name in ONBOARDING} <= set(paths)
    examples = discover_examples(REPO_DIR)
    fences = tuple(
        fence
        for path in paths
        for fence in discover_document(
            path.relative_to(REPO_DIR).as_posix(), path.read_text(encoding="utf-8")
        )
    )
    assert {(e.document, e.line) for e in examples} == {
        (f.document, f.line) for f in fences if f.language == "powershell"
    }
    assert {e.id for e in examples} == {s.id for s in SCENARIOS}
    result = subprocess.run(
        [sys.executable, str(REPO_DIR / "tests/doc_blocks.py"), "--root", str(REPO_DIR), "--json"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == json.loads(json.dumps([asdict(e) for e in examples]))


def test_inline_console_encoding_command_is_exact_and_unambiguous(tmp_path: Path) -> None:
    approved = "[Console]::OutputEncoding = [Text.UTF8Encoding]::new()"
    source = (REPO_DIR / "docs/setup.md").read_text(encoding="utf-8")
    assert f"`{approved}`" in source
    assert console_output_encoding_command(source) == approved
    assert console_output_encoding_command(f"Use `{approved}` before piping.") == approved
    assert console_output_encoding_command(f"Use ``{approved}`` before piping.") == approved
    assert console_output_encoding_command(f"Use ```{approved}``` before piping.") == approved
    # Property mentions/getters are not assignments and cannot supply a missing setter.
    mention = "Read `[Console]::OutputEncoding` and `$encoding = [Console]::OutputEncoding`."
    assert console_output_encoding_command(mention + f"\n`{approved}`") == approved
    wrong = "[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(1252)"
    duplicates = (
        f"`{approved}`\n`{approved}`",  # Identical duplicates are still ambiguous.
        f"`{approved}`\n`{wrong}`",  # Discover changed values before exact validation.
        f"`{wrong}`\n`{approved}`",  # Order cannot hide a conflicting assignment.
        f"`{approved}`\n`{approved.lower()}`",
        f"`{approved}`\n`{wrong.replace('[Console]', '[System.Console]')}`",
        f"`{approved}`\n`{wrong.replace('[Console]', '[system.console]')}`",
        f"`{approved}`\n`[ System . Console ] :: OutputEncoding = $other`",
        f"`{approved}; {wrong}`",  # Two assignments inside a single inline span.
        f"`{approved}`\n``{wrong}``",
        f"`{approved}`\n`[Console]::OutputEncoding =\n[Text.Encoding]::GetEncoding(1252)`",
        f"`{approved}`\r\n``[System.Console]::OutputEncoding =\r\n[Text.Encoding]::ASCII``",
        f'`{approved}`\n``{wrong}; Write-Output "`n"``',
        f'`{approved}`\n``{wrong}; Write-Output "`n"`r``',
        f'`{approved}`\n```{wrong}; Write-Output "``n"```',
        f'`{approved}`\n``{wrong}; Write-Output "```n"``',
    )
    for text in ("", mention, *duplicates):
        with pytest.raises(ValueError, match="expected exactly one inline"):
            console_output_encoding_command(text)
    changed = (
        wrong,
        approved.replace("new()", "new($true)"),
        approved.lower(),
        approved.replace("[Console]", "[System.Console]"),
        approved.replace(" = ", "="),
        " " + approved,
        approved + "; Write-Output 'extra'",
    )
    for command in changed:
        with pytest.raises(ValueError, match="changed from approved text"):
            console_output_encoding_command(f"`{command}`")

    cli = [sys.executable, str(REPO_DIR / "tests/doc_blocks.py")]
    mode = "--console-output-encoding"
    result = subprocess.run(
        [*cli, "--root", str(REPO_DIR), mode],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert result.stdout == approved + "\n"
    assert not result.stderr
    # The CLI must read the supplied root, not use a literal or the checkout's valid docs.
    setup = tmp_path / "docs/setup.md"
    setup.parent.mkdir()
    for text in (f"`{approved}`", "", *duplicates, *(f"`{c}`" for c in changed)):
        setup.write_text(text, encoding="utf-8", newline="\n")
        result = subprocess.run(
            [*cli, "--root", str(tmp_path), mode],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if text == f"`{approved}`":
            assert result.returncode == 0, result.stderr
            assert result.stdout == approved + "\n"
        else:
            assert result.returncode == 1, (text, result.stderr)
            assert not result.stdout
            assert "inline Console.OutputEncoding" in result.stderr
    for args in ([mode], ["--root", str(REPO_DIR)], ["--root", str(REPO_DIR), "--json", mode]):
        result = subprocess.run(
            [*cli, *args], capture_output=True, text=True, encoding="utf-8", check=False
        )
        assert result.returncode == 2, result.stderr
        assert not result.stdout


def test_doc_fence_discovery_is_total() -> None:
    text = (
        "# Guide\r\n## New section\r\n"
        "  ```sh\r\n  first &&\r\n  second\r\n  ```\r\n"
        "  ```ps1\r\n  first\r\n  second\r\n  ```\r\n"
        "~~~shell\r\nthird\r\n~~~\r\n~~~pwsh\r\nthird\r\n~~~\r\n"
        "### Final handoff\r\n```zsh\r\nfourth\r\n```\r\n"
        "```PowerShell\r\nfourth\r\n```\r\n"
        '```json\r\n{"not": "a command"}\r\n```\r\n'
    )
    scenarios = (
        Scenario("first", "new.md", ("Guide", "New section")),
        Scenario("second", "new.md", ("Guide", "New section"), ordinal=2),
        Scenario("third", "new.md", ("Guide", "New section", "Final handoff")),
    )
    fences = discover_document("new.md", text)
    examples = validate_fences(fences, exceptions=(), scenarios=scenarios)
    assert len(examples) == 3
    assert fences[0].line == 3
    assert fences[0].body == "first &&\nsecond\n"
    assert examples[2].headings == ("Guide", "New section", "Final handoff")
    invalid = (
        "```bash\ncommand\n",  # Unclosed.
        "```bash\ncommand\n```~\n",  # Mixed markers cannot close a fence.
        "```bash extra\ncommand\n```\n",  # Malformed label.
        "```bash\ncommand\n```powershell\ncommand\n```\n",  # Missing closer.
        "```\ncommand\n```\n",  # An unlabelled shell cannot disappear from inventory.
        "```console\n$ command\n```\n",  # Explicitly rejected runnable alias.
        "```cmd\ncommand\n```\n",
        "```shell-script\ncommand\n```\n",  # Unknown aliases fail closed.
        "```unclassified\ncommand\n```\n",
        "> ```bash\n> command\n> ```\n",  # Unsupported containers cannot hide commands.
        "- ```sh\n  command\n  ```\n",
        "1. > ```pwsh\n   > command\n   > ```\n",
        "# Same\n```bash\none\n```\n# Same\n```ps1\none\n```\n",  # Cross-section pairing.
        "# Same\n## Child\n```sh\none\n```\n# Same\n## Child\n```ps\none\n```\n",
    )
    for markdown in invalid:
        with pytest.raises(ValueError, match=r"bad.md:\d+"):
            discover_document("bad.md", markdown)
    unpaired = (
        "```bash\none\n```\n",
        "```powershell\none\n```\n",
        "```bash\none\n```\n```bash\ntwo\n```\n```powershell\none\n```\n",
        "```bash\none\n```\n## Different\n```powershell\none\n```\n",
    )
    for markdown in unpaired:
        with pytest.raises(ValueError, match=r"bad.md:\d+.*(?:mirror|unpaired)"):
            validate_fences(discover_document("bad.md", markdown), exceptions=(), scenarios=())
    # Can-fail mutations run the actual validator, not a parallel imitation of its rules.
    all_fences = tuple(
        fence
        for path in corpus_paths(REPO_DIR)
        for fence in discover_document(
            path.relative_to(REPO_DIR).as_posix(), path.read_text(encoding="utf-8")
        )
    )
    ablation = (REPO_DIR / "docs/ablation.md").read_text(encoding="utf-8")
    extra = discover_document("docs/ablation.md", ablation + "\n## New\n```bash\nuncovered\n```\n")
    with pytest.raises(ValueError, match=r"docs/ablation.md:\d+.*New.*mirror"):
        validate_fences(all_fences + extra)
    with pytest.raises(ValueError, match=r"docs/ablation.md:\d+.*container prefix"):
        discover_document("docs/ablation.md", ablation + "\n> ```bash\n> uncovered\n> ```\n")
    ordinary = next(
        f for f in all_fences if f.language == "powershell" and "uv init --bare" in f.body
    )
    with pytest.raises(ValueError, match="missing/ambiguous PowerShell mirror"):
        validate_fences(tuple(f for f in all_fences if f != ordinary))
    with pytest.raises(ValueError, match="command drift"):
        validate_fences(
            tuple(
                replace(f, body=f.body + "unexpected\n") if f == ordinary else f for f in all_fences
            )
        )
    compose = next(s for s in SCENARIOS if s.id == "setup-compose")
    with pytest.raises(ValueError, match="unused/stale execution dispositions"):
        validate_fences(
            tuple(
                f
                for f in all_fences
                if (f.document, f.headings) != (compose.document, compose.headings)
            )
        )
    # Only a terminal ` &&` is removed, never other whitespace or shell syntax.
    pair = discover_document("x.md", "```bash\nx && y\n```\n```powershell\nx\n```\n")
    with pytest.raises(ValueError, match="command drift"):
        validate_fences(pair, exceptions=(), scenarios=(Scenario("x", "x.md", ()),))


def test_doc_fence_exceptions_are_exact_and_consumed() -> None:
    for rule in EXCEPTIONS:
        scenario = rule.scenario
        prefix = "\n".join(
            f"{'#' * level} {title}" for level, title in enumerate(scenario.headings, 1)
        )
        # Each reviewed pair is checked in isolation, with its real identity and exact bodies.
        markdown = prefix + "\n"
        if rule.bash is not None:
            markdown += f"```bash\n{rule.bash}```\n"
        markdown += f"```powershell\n{rule.powershell}```\n"
        isolated = replace(scenario, ordinal=1)
        exact = replace(rule, scenario=isolated)
        fences = discover_document(scenario.document, markdown)
        assert validate_fences(fences, exceptions=(exact,), scenarios=(isolated,))
        for index in range(len(fences)):
            changed = tuple(
                replace(f, body=f.body + "unrelated-command\n") if i == index else f
                for i, f in enumerate(fences)
            )
            with pytest.raises(ValueError, match="stale exception"):
                validate_fences(changed, exceptions=(exact,), scenarios=(isolated,))
        moved = tuple(replace(f, headings=("Unreviewed section",)) for f in fences)
        with pytest.raises(ValueError, match=r"(?:unpaired|command drift)"):
            validate_fences(moved, exceptions=(exact,), scenarios=(isolated,))
        with pytest.raises(ValueError, match="unused/stale exceptions"):
            validate_fences((), exceptions=(exact,), scenarios=(isolated,))
        with pytest.raises(ValueError, match="duplicate/overbroad exception"):
            validate_fences(fences, exceptions=(exact, exact), scenarios=(isolated,))
        with pytest.raises(ValueError, match="exception needs a reason"):
            validate_fences(fences, exceptions=(replace(exact, reason=""),), scenarios=(isolated,))


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


def test_windows_console_pipeline_uses_the_documented_setting() -> None:
    workflow = yaml.safe_load(
        (REPO_DIR / ".github/workflows/check.yml").read_text(encoding="utf-8")
    )
    (step,) = [
        step
        for step in workflow["jobs"]["windows"]["steps"]
        if step.get("name") == "UTF-8 output through the documented PowerShell setting (required)"
    ]
    assert step["shell"] == "powershell"  # Windows PowerShell 5.1, not pwsh.
    assert not step.get("continue-on-error", False)
    assert step.get("if") == "${{ !cancelled() }}"
    script = step["run"]
    assert script.isascii(), "the expected Unicode text must come from the shared fixture"
    assert "from test_cli_output import MARK, write_reading_fixture" in script
    assert "song, config = write_reading_fixture(Path(sys.argv[1]))" in script
    assert "ensure_ascii=True" in script
    assert '"mark": MARK' in script or "'mark': MARK" in script
    assert "ConvertFrom-Json -InputObject ($fixture -join" in script
    assert '$expectedLine = "  beats 0-1: $($info.mark)"' in script
    # The source command is extracted and executed, never replaced by a literal UTF-8 setter.
    assert re.findall(r"(?m)^\s*\$encodingCommand = (.+)$", script) == [
        "uv run python tests/doc_blocks.py --root $env:GITHUB_WORKSPACE --console-output-encoding",
        '$encodingCommand -join "`n"',
    ]
    assert "Invoke-Expression $encodingCommand" in script
    assert "--json" not in script
    assert "UTF8Encoding" not in script
    assert not re.search(r"(?<![\w:])\$OutputEncoding\b", script, re.IGNORECASE)
    assert re.findall(r"(?m)^\s*\[Console\]::OutputEncoding = (.+)$", script) == [
        "[Text.Encoding]::GetEncoding(1252)",
        "$originalEncoding",
    ]
    assert "function Test-ExpectedLine([object[]]$Lines, [string]$Expected)" in script
    assert "return ($Lines.Count -eq 1 -and $Lines[0].Line -ceq $Expected)" in script
    for name in ("wrong", "right"):
        # Capture native status immediately after the actual native/cmdlet pipeline, not $?.
        assert re.search(
            rf"\${name} = @\(uv run read-song \$info.song --config \$info.config \|\s*"
            r"Select-String -SimpleMatch \$expectedLine -CaseSensitive\)\n"
            r"\s*\$status = \$LASTEXITCODE\n"
            r"\s*if \(\$status -ne 0\) \{ throw ",
            script,
        )
    negative = "if (Test-ExpectedLine -Lines $wrong -Expected $expectedLine)"
    positive = "if (-not (Test-ExpectedLine -Lines $right -Expected $expectedLine))"
    assert (
        script.index("try {")
        < script.index("$env:PYTHONIOENCODING = 'cp1252'")
        < script.index("[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(1252)")
        < script.index("[Console]::OutputEncoding.CodePage -ne 1252")
        < script.index("$wrong = @(")
        < script.index(negative)
        < script.index("Invoke-Expression $encodingCommand")
        < script.index("[Console]::OutputEncoding.CodePage -ne 65001")
        < script.index("$right = @(")
        < script.index(positive)
        < script.index("} finally {")
    )
    assert re.search(re.escape(negative) + r'\s*\{\s*throw "', script)
    assert re.search(re.escape(positive) + r'\s*\{\s*throw "', script)
    before, body = script.split("try {", 1)
    assert "$originalEncoding = [Console]::OutputEncoding" in before
    assert (
        "$originalPythonEncoding = "
        "[Environment]::GetEnvironmentVariable('PYTHONIOENCODING', 'Process')" in before
    )
    finally_body = body.split("} finally {", 1)[1]
    assert "[Console]::OutputEncoding = $originalEncoding" in finally_body
    assert "if ($null -eq $originalPythonEncoding)" in finally_body
    assert "Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue" in finally_body
    assert "$env:PYTHONIOENCODING = $originalPythonEncoding" in finally_body


def test_windows_requires_headless_preset_selection_and_rejects_bypasses() -> None:
    workflow = yaml.safe_load(
        (REPO_DIR / ".github/workflows/check.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["windows"]
    assert job.get("continue-on-error", False) is False
    (step,) = [
        item
        for item in job["steps"]
        if item.get("name") == "Surge XT and Surge XT Effects through Pedalboard (required)"
    ]
    node = "tests/test_pedalboard_real.py::test_surge_factory_presets_take_effect_headlessly"
    required = [
        "test_each_instrument_meets_the_instrument_contract",
        "test_a_render_has_exactly_the_performance_s_frames",
        "test_a_windows_plugin_loads_from_its_binary_as_from_its_bundle",
        "test_surge_restored_preset_keeps_its_first_note",
        "test_surge_factory_presets_take_effect_headlessly",
        "test_each_effect_renders_as_many_frames_as_it_is_given",
    ]

    def require(candidate: dict[str, object]) -> None:
        assert candidate.get("continue-on-error", False) is False
        assert candidate.get("if") == "${{ !cancelled() }}"
        script = candidate["run"]
        assert isinstance(script, str)
        # Passing-test metrics must be visible; default pytest capture hides that evidence.
        assert "uv run pytest -m real_plugins -v -s -rs --durations=0" in script
        assert re.findall(r'"tests/test_pedalboard_real.py::(\w+)"', script) == required
        assert re.search(
            r"Tee-Object -Variable output\n\s*\$status = \$LASTEXITCODE\n"
            r'\s*if \(\$status -ne 0\) \{ throw "the real-plugin tests failed" \}\n'
            r'\s*if \(\$output -match "skipped"\) \{ throw "a real-plugin test skipped" \}',
            script,
        )

    require(step)
    script = step["run"]
    mutations = (
        {**step, "run": script.replace(f'"{node}"', '"omitted-headless-node"')},
        {**step, "run": script.replace("-v -s -rs", "-v -rs")},
        {**step, "run": script.replace("$status -ne 0", "$status -eq 0")},
        {**step, "run": script.replace('$output -match "skipped"', "$false")},
        {**step, "run": script.replace("$status = $LASTEXITCODE", "$status = 0")},
        {
            **step,
            "run": script.replace(
                "$status = $LASTEXITCODE", "$ignored = $?\n$status = $LASTEXITCODE"
            ),
        },
        {**step, "continue-on-error": True},
        {**step, "if": "${{ success() }}"},
    )
    for changed in mutations:
        assert changed != step
        with pytest.raises(AssertionError):
            require(changed)


def test_windows_doc_execution_covers_discovered_examples() -> None:
    examples = discover_examples(REPO_DIR)
    fences = tuple(
        fence
        for path in corpus_paths(REPO_DIR)
        for fence in discover_document(
            path.relative_to(REPO_DIR).as_posix(), path.read_text(encoding="utf-8")
        )
    )
    for scenario in SCENARIOS:
        with pytest.raises(ValueError, match="no execution disposition"):
            validate_fences(fences, scenarios=tuple(s for s in SCENARIOS if s != scenario))
    new = discover_document(
        "new.md", "# New\n```bash\nnew-command\n```\n```ps1\nnew-command\n```\n"
    )
    with pytest.raises(ValueError, match=r"new.md:5.*New.*no execution disposition"):
        validate_fences(fences + new)
    registry = next(s for s in SCENARIOS if s.id == "registry")
    with pytest.raises(ValueError, match="only registry may use long-paths"):
        validate_fences(
            fences,
            scenarios=tuple(replace(s, runner="studio") if s == registry else s for s in SCENARIOS),
        )
    workflow = yaml.safe_load(
        (REPO_DIR / ".github/workflows/check.yml").read_text(encoding="utf-8")
    )
    (step,) = [
        s
        for s in workflow["jobs"]["windows"]["steps"]
        if s.get("name") == "A Surge XT studio through the documented PowerShell (required)"
    ]
    assert step["shell"] == "powershell"
    script = step["run"]
    assert "doc_blocks.py" in script
    assert "--root" in script
    assert "--json" in script
    assert "ConvertFrom-Json" in script
    assert "Get-Blocks" not in script
    assert set(re.findall(r"Invoke-Example\s+'([^']+)'", script)) == {
        e.id for e in examples if e.runner == "studio"
    }
    assert [e.id for e in examples if e.runner == "long-paths"] == ["registry"]


@pytest.mark.skipif(
    sys.platform != "win32", reason="executes Windows PowerShell 5.1 JSON conversion"
)
@pytest.mark.parametrize("nest_array", [False, True], ids=["workflow", "reject-nested-regression"])
def test_windows_inventory_json_is_a_flat_example_array(nest_array: bool) -> None:
    shell = shutil.which("powershell.exe")
    assert shell is not None, "Windows PowerShell 5.1 is required, not pwsh"
    examples = discover_examples(REPO_DIR)
    workflow = yaml.safe_load(
        (REPO_DIR / ".github/workflows/check.yml").read_text(encoding="utf-8")
    )
    step = next(
        s
        for s in workflow["jobs"]["windows"]["steps"]
        if s.get("name") == "A Surge XT studio through the documented PowerShell (required)"
    )
    # Exercise the workflow's actual conversion line: @(<ConvertFrom-Json>) nests arrays in 5.1.
    (conversion,) = [
        line.strip() for line in step["run"].splitlines() if line.strip().startswith("$examples = ")
    ]
    if nest_array:
        conversion = conversion.replace("$examples = ", "$examples = @(", 1) + ")"
    inventory = json.dumps([asdict(example) for example in examples])
    script = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$ErrorActionPreference = 'Stop'\n"
        "if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'requires Windows PowerShell 5.1' }\n"
        f"$inventory = @'\n{inventory}\n'@\n{conversion}\n"
        f"if ($examples.Count -ne {len(examples)}) "
        "{ throw 'inventory is nested or incomplete' }\n"
        "foreach ($example in $examples) {\n"
        "  if ($example -is [array] -or $example.powershell -isnot [string]) "
        "{ throw 'expected one example object with a command string' }\n"
        "}\n"
        "$found = @($examples | Where-Object { $_.id -eq 'setup-create' "
        "-and $_.runner -eq 'studio' })\n"
        "if ($found.Count -ne 1 -or -not "
        "$found[0].powershell.Contains('git+https://github.com/isnbh0/poser')) "
        "{ throw 'scenario lookup did not select a single example' }\n"
    )
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            base64.b64encode(script.encode("utf-16-le")).decode("ascii"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if nest_array:
        assert result.returncode != 0
        assert "inventory is nested or incomplete" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert not result.stderr, result.stderr


def test_windows_checks_precede_the_installed_plugin_isolation_suite() -> None:
    workflow = yaml.safe_load(
        (REPO_DIR / ".github/workflows/check.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["windows"]["steps"]
    names = [step.get("name") for step in steps]
    checks = names.index("Format, lint, and types (required)")
    assert (
        checks
        < names.index("Install Surge XT (required)")
        < names.index("Plugin-free suite (required)")
    )
    assert "Type-check the Windows backends (required)" not in names
    assert steps[checks]["run"].splitlines() == [
        "$PSNativeCommandUseErrorActionPreference = $true",
        "uv run ruff format --check .",
        "uv run ruff check .",
        "uv run ty check",
    ]


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
        ["/bin/sh", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
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

    text = (REPO_DIR / "packages/poser-scsynth/README.md").read_text(encoding="utf-8")
    example, windows = re.findall(r"```toml\n(.*?)```", text, re.DOTALL)
    options = tomllib.loads(example)["instruments"]["sc"]
    assert options.pop("adapter") == "poser_scsynth.instrument:create"
    config = from_options("sc", options, tmp_path)
    assert config.scsynth is not None
    assert config.scsynth.name == "scsynth"
    # The Windows line keeps its backslashes and names an absolute path there.
    engine = PureWindowsPath(tomllib.loads(windows)["scsynth"])
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
