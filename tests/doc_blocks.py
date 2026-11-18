"""Discover and validate the documented shell examples (also used by Windows CI)."""

import argparse
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

ONBOARDING = (
    "README.md",
    "docs/setup.md",
    "docs/bootstrap.md",
    "docs/ablation.md",
    "docs/showcase.md",
    "src/poser/reference_setups/surge/README.md",
    "packages/poser-scsynth/README.md",
)

LANGUAGES = {
    "bash": "bash",
    "sh": "bash",
    "shell": "bash",
    "shellscript": "bash",
    "zsh": "bash",
    "powershell": "powershell",
    "pwsh": "powershell",
    "ps": "powershell",
    "ps1": "powershell",
    "psm1": "powershell",
}
# Unknown labels fail closed: an unlisted shell alias must not become an unchecked data block.
DATA_LANGUAGES = {"json", "python", "toml", "text", "yaml", "yml"}


@dataclass(frozen=True)
class Fence:
    document: str
    headings: tuple[str, ...]
    line: int
    language: str
    body: str

    @property
    def location(self) -> str:
        return f"{self.document}:{self.line} ({' > '.join(self.headings) or '<preamble>'})"


@dataclass(frozen=True)
class Scenario:
    id: str
    document: str
    headings: tuple[str, ...]
    ordinal: int = 1
    runner: Literal["studio", "long-paths"] = "studio"

    @property
    def identity(self) -> tuple[str, tuple[str, ...], int]:
        return self.document, self.headings, self.ordinal


@dataclass(frozen=True)
class ExceptionRule:
    scenario: Scenario
    bash: str | None
    powershell: str
    reason: str


@dataclass(frozen=True)
class Example:
    id: str
    document: str
    headings: tuple[str, ...]
    line: int
    bash: str | None
    powershell: str
    runner: Literal["studio", "long-paths"]
    exception: str | None


SETUP = "docs/setup.md"
UPGRADE = "docs/upgrading.md"
SCENARIOS = (
    Scenario("registry", SETUP, ("Create a studio", "Prerequisites"), runner="long-paths"),
    Scenario("setup-create", SETUP, ("Create a studio", "Create and verify")),
    Scenario("setup-compose", SETUP, ("Create a studio", "Compose your first song (optional)")),
    Scenario(
        "setup-export",
        SETUP,
        ("Create a studio", "Compose your first song (optional)", "Final handoff"),
    ),
    Scenario("upgrade", UPGRADE, ("Upgrade a studio",)),
    Scenario("upgrade-songs", UPGRADE, ("Upgrade a studio",), ordinal=2),
    Scenario("search-devices", UPGRADE, ("Upgrade a studio", "Names Windows cannot hold")),
    Scenario("search-levels", UPGRADE, ("Upgrade a studio", "Song meter")),
    Scenario("search-modes", UPGRADE, ("Upgrade a studio", "Mark checks and key modes")),
    Scenario(
        "showcase-export", "docs/showcase.md", ("Surge showcase", "Instrument and reproduction")
    ),
    Scenario(
        "custom-path",
        "src/poser/reference_setups/surge/README.md",
        ("Surge XT reference setup", "Install or locate Surge XT", "Other locations"),
    ),
)
_BY_ID = {scenario.id: scenario for scenario in SCENARIOS}
EXCEPTIONS = (
    ExceptionRule(
        _BY_ID["registry"],
        None,
        "New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' "
        "-Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force\n",
        "Windows-only registry command; the long-paths step guards and executes it verbatim.",
    ),
    ExceptionRule(
        _BY_ID["custom-path"],
        "uv run init-workspace --setup surge --plugin-path '/path/to/Surge XT.vst3'\n",
        "uv run init-workspace --setup surge --plugin-path 'D:\\Plugins\\Surge XT.vst3'\n",
        "Platform-specific plugin path placeholders; CI substitutes the installed Surge bundle.",
    ),
    ExceptionRule(
        _BY_ID["upgrade-songs"],
        "find songs \\( -name '*.yaml' -o -name '*.yml' -o -name '*.py' \\) "
        "-exec uv run read-song {} \\; \\\n  > /dev/null\n",
        "Get-ChildItem songs -Recurse -File | "
        "Where-Object { $_.Extension -in '.yaml', '.yml', '.py' } "
        "| ForEach-Object { uv run read-song $_.FullName > $null }\n",
        "Recursive song loading uses each shell's loop and null sink; CI checks two broken songs.",
    ),
    ExceptionRule(
        _BY_ID["search-devices"],
        "grep -rniwE 'con|prn|aux|nul|conin\\$|conout\\$|com[1-9]|lpt[1-9]' songs poser.toml\n",
        "@(Get-ChildItem songs -Recurse -File -Force; Get-Item poser.toml) | "
        "Select-String -Pattern "
        "'(?<!\\w)(?:con|prn|aux|nul|conin\\$|conout\\$|com[1-9]|lpt[1-9])(?!\\w)'\n",
        "Case-insensitive recursive/direct-file candidate search with literal-dollar boundaries.",
    ),
    ExceptionRule(
        _BY_ID["search-levels"],
        "grep -rn bar_dbfs .\n",
        "Get-ChildItem . -Recurse -File -Force | "
        "Select-String -Pattern 'bar_dbfs' -CaseSensitive\n",
        "Case-sensitive recursive candidate search of all studio text, including hidden files.",
    ),
    ExceptionRule(
        _BY_ID["search-modes"],
        'grep -rnE "minor|major" songs\n',
        "Get-ChildItem songs -Recurse -File -Force | Select-String -Pattern 'minor|major' "
        "-CaseSensitive\n",
        "Case-sensitive recursive candidate search using each shell's regex alternation.",
    ),
)


def corpus_paths(root: Path) -> tuple[Path, ...]:
    """One sorted, deduplicated inventory; callers supply the repository explicitly."""
    return tuple(sorted({*(root / name for name in ONBOARDING), *(root / "docs").glob("*.md")}))


def discover_document(document: str, text: str) -> tuple[Fence, ...]:
    """Parse fenced blocks and ATX heading ancestry, retaining command text and locations."""
    headings: list[tuple[int, str]] = []
    found: list[Fence] = []
    section_number = 0
    shell_sections: dict[tuple[str, ...], int] = {}
    opening: tuple[str, int, str, int, tuple[str, ...]] | None = None
    body: list[str] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if opening is not None:
            marker, indent, language, start, ancestry = opening
            closing = re.fullmatch(r"\s*(`{3,}|~{3,})\s*", line)
            if closing and closing[1][0] == marker[0] and len(closing[1]) >= len(marker):
                if language in LANGUAGES:
                    found.append(
                        Fence(
                            document, ancestry, start, LANGUAGES[language], "\n".join(body) + "\n"
                        )
                    )
                opening = None
                body = []
            elif re.match(r"\s*[`~]{3,}\s*\S+", line):
                raise ValueError(
                    f"{document}:{line_number}: malformed nested/unclosed fence at {start}"
                )
            else:
                # Markdown list indentation belongs to the fence, not the command.
                body.append(line[indent:] if line[:indent].isspace() else line)
            continue
        if re.match(r"\s*(?:>\s*|[-+*]\s+|\d+[.)]\s+)+(?:`{3,}|~{3,})", line):
            raise ValueError(f"{document}:{line_number}: unsupported fence container prefix")
        heading = re.fullmatch(r"\s*(#{1,6})\s+(.+?)(?:\s+#+)?\s*", line)
        if heading:
            section_number += 1
            level = len(heading[1])
            headings = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, heading[2]))
            continue
        fence = re.fullmatch(r"(\s*)(`{3,}|~{3,})\s*([^\s]*)\s*", line)
        if fence:
            marker = fence.group(2)
            label = fence.group(3)
            assert isinstance(marker, str)
            assert isinstance(label, str)
            language = label.lower()
            ancestry = tuple(title for _, title in headings)
            location = f"{document}:{line_number} ({' > '.join(ancestry)})"
            if not language:
                raise ValueError(f"{location}: orphan or unlabelled fence")
            if language not in LANGUAGES and language not in DATA_LANGUAGES:
                raise ValueError(f"{location}: unsupported fence label {language!r}")
            if language in LANGUAGES:
                previous_section = shell_sections.setdefault(ancestry, section_number)
                if previous_section != section_number:
                    raise ValueError(f"{location}: ambiguous repeated shell heading ancestry")
            opening = marker, len(fence.group(1)), language, line_number, ancestry
        elif re.match(r"\s*[`~]{3,}", line):
            raise ValueError(f"{document}:{line_number}: malformed fence")
    if opening is not None:
        raise ValueError(f"{document}:{opening[3]} ({' > '.join(opening[4])}): unclosed fence")
    return tuple(found)


def validate_fences(
    fences: tuple[Fence, ...],
    *,
    exceptions: tuple[ExceptionRule, ...] = EXCEPTIONS,
    scenarios: tuple[Scenario, ...] = SCENARIOS,
) -> tuple[Example, ...]:
    """Require adjacent one-to-one mirrors and an explicit execution disposition for every pair."""
    dispositions = {scenario.identity: scenario for scenario in scenarios}
    rules = {rule.scenario.identity: rule for rule in exceptions}
    if len(dispositions) != len(scenarios) or len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("duplicate/ambiguous execution disposition")
    if len(rules) != len(exceptions):
        raise ValueError("duplicate/overbroad exception")
    grouped: dict[tuple[str, tuple[str, ...]], list[Fence]] = defaultdict(list)
    for fence in fences:
        grouped[fence.document, fence.headings].append(fence)
    examples: list[Example] = []
    consumed = set()
    used_dispositions = set()
    for (document, headings), group in grouped.items():
        index = 0
        ordinal = 0
        while index < len(group):
            ordinal += 1
            first = group[index]
            bash = None
            if first.language == "bash":
                bash = first.body
                index += 1
                if index == len(group) or group[index].language != "powershell":
                    raise ValueError(f"{first.location}: missing/ambiguous PowerShell mirror")
            powershell = group[index]
            index += 1
            identity = document, headings, ordinal
            rule = rules.get(identity)
            if rule is not None:
                if (bash, powershell.body) != (rule.bash, rule.powershell):
                    raise ValueError(f"{first.location}: stale exception: exact content changed")
                if not rule.reason.strip():
                    raise ValueError(f"{first.location}: exception needs a reason")
                consumed.add(identity)
            elif bash is None:
                raise ValueError(f"{first.location}: unpaired PowerShell fence")
            elif powershell.body.splitlines() != [
                line.removesuffix(" &&") for line in bash.splitlines()
            ]:
                raise ValueError(
                    f"{first.location}: command drift from PowerShell at {powershell.line}"
                )
            scenario = dispositions.get(identity)
            if scenario is None:
                raise ValueError(f"{powershell.location}: no execution disposition")
            used_dispositions.add(identity)
            if (scenario.runner == "long-paths") != (scenario.id == "registry"):
                raise ValueError(f"{powershell.location}: only registry may use long-paths")
            if scenario.id == "registry" and (bash is not None or rule is None):
                raise ValueError(
                    f"{powershell.location}: registry requires its Windows-only exception"
                )
            examples.append(
                Example(
                    scenario.id,
                    document,
                    headings,
                    powershell.line,
                    bash,
                    powershell.body,
                    scenario.runner,
                    rule.reason if rule else None,
                )
            )
    if unused := rules.keys() - consumed:
        raise ValueError(f"unused/stale exceptions: {sorted(unused)}")
    if unused := dispositions.keys() - used_dispositions:
        raise ValueError(f"unused/stale execution dispositions: {sorted(unused)}")
    return tuple(examples)


def discover_examples(root: Path) -> tuple[Example, ...]:
    fences = tuple(
        fence
        for path in corpus_paths(root)
        for fence in discover_document(
            path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        )
    )
    return validate_fences(fences)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--json", action="store_true", required=True)
    args = parser.parse_args()
    try:
        examples = discover_examples(args.root.resolve())
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps([asdict(example) for example in examples]))


if __name__ == "__main__":
    main()
