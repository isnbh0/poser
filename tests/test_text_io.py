"""Product code names the encoding of every text it reads or writes, and the newline of every text
it writes, so its text bytes are the same whatever the platform's defaults. On Windows, text I/O
without an encoding uses the ANSI code page, and text written without a newline gets CRLF."""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PRODUCT = (REPO / "src" / "poser", REPO / "packages" / "poser-scsynth" / "src", REPO / "scripts")
WRITING_MODES = frozenset("wax+")
SUBPROCESS_TEXT_CALLS = frozenset(
    f"subprocess.{name}" for name in ("run", "Popen", "check_output", "check_call", "call")
)
TEMPORARY_FILES = frozenset(
    f"tempfile.{name}" for name in ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile")
)


def _imported_names(tree: ast.Module) -> dict[str, str]:
    """Map each name bound by an import to the dotted name it stands for."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _called(func: ast.expr, imported: dict[str, str]) -> tuple[str, bool]:
    """Return the dotted name a call targets and whether it is a method of an object that is not
    an imported module. A method's name is `.attr`."""
    if isinstance(func, ast.Name):
        return imported.get(func.id, func.id), False
    if isinstance(func, ast.Attribute):
        if func.attr in ("read_text", "write_text"):
            return f".{func.attr}", True
        if isinstance(func.value, ast.Name) and func.value.id in imported:
            return f"{imported[func.value.id]}.{func.attr}", False
        return f".{func.attr}", True
    return "", True


def _mode(call: ast.Call, position: int, default: str) -> str | None:
    """Return the call's mode, its default when absent, or None when it is not a constant."""
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    node = call.args[position] if len(call.args) > position else keywords.get("mode")
    if node is None:
        return default
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _missing(call: ast.Call, names: tuple[str, ...]) -> list[str]:
    """Return the names the call does not give, counting a keyword given as None as not given."""
    given = {
        keyword.arg
        for keyword in call.keywords
        if not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None)
    }
    return [name for name in names if name not in given]


def _open_problem(call: ast.Call, label: str, position: int, default: str) -> str | None:
    mode = _mode(call, position, default)
    if mode is None:
        return f"{label} has a mode that is not a string constant"
    if "b" in mode:
        return None
    needed = ("encoding", "newline") if WRITING_MODES & set(mode) else ("encoding",)
    missing = _missing(call, needed)
    return f"{label} in text mode names no {' or '.join(missing)}" if missing else None


def _is_true(node: ast.expr) -> bool:
    return not (isinstance(node, ast.Constant) and not node.value)


def _call_problem(call: ast.Call, imported: dict[str, str]) -> str | None:
    name, is_method = _called(call.func, imported)
    problem = _named_problem(call, name, is_method)
    unseen = any(keyword.arg is None for keyword in call.keywords)
    if problem is None and unseen and _is_text_io(name, is_method):
        problem = f"{name}() passes keywords through **, which the check cannot see"
    return problem


def _is_text_io(name: str, is_method: bool) -> bool:
    return (
        name in (".read_text", ".write_text", "open", "io.open", "os.fdopen", "io.TextIOWrapper")
        or (name == ".open" and is_method)
        or name in TEMPORARY_FILES
        or name in SUBPROCESS_TEXT_CALLS
        or (name.endswith(".open") and not is_method and name != "os.open")
    )


def _named_problem(call: ast.Call, name: str, is_method: bool) -> str | None:
    problem: str | None = None
    if name == ".read_text":
        missing = _missing(call, ("encoding",))
        problem = "read_text names no encoding" if missing else None
    elif name == ".write_text":
        missing = _missing(call, ("encoding", "newline"))
        problem = f"write_text names no {' or '.join(missing)}" if missing else None
    elif name in ("open", "io.open"):
        problem = _open_problem(call, f"{name}()", 1, "r")
    elif name == "os.fdopen":
        problem = _open_problem(call, "os.fdopen()", 1, "r")
    elif name == ".open" and is_method:
        problem = _open_problem(call, ".open()", 0, "r")
    elif name.endswith(".open") and not is_method and name != "os.open":
        problem = _open_problem(call, f"{name}()", 1, "r")
    elif name in TEMPORARY_FILES:
        problem = _open_problem(call, f"{name}()", 0, "w+b")
    elif name == "io.TextIOWrapper":
        # The encoding is the second positional argument or a keyword.
        if len(call.args) < 2 and _missing(call, ("encoding",)):
            problem = "io.TextIOWrapper() names no encoding"
    elif name in SUBPROCESS_TEXT_CALLS:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        # An `errors` argument puts the call in text mode too.
        flags = ("text", "universal_newlines", "errors")
        text = any(_is_true(keywords[flag]) for flag in flags if flag in keywords)
        if text and _missing(call, ("encoding",)):
            problem = f"{name}() in text mode names no encoding"
    return problem


def text_io_problems(tree: ast.Module) -> list[tuple[int, str]]:
    """Return the line and the reason for each call in `tree` that reads or writes text without
    naming its encoding, or writes text without naming its newline."""
    imported = _imported_names(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    return sorted(
        (call.lineno, problem) for call in calls if (problem := _call_problem(call, imported))
    )


def _problems(source: str) -> list[tuple[int, str]]:
    prelude = "import io\nimport os\nimport subprocess\nimport tempfile\n"
    return text_io_problems(ast.parse(prelude + source))


def test_product_text_io_names_encoding_and_newline() -> None:
    files = sorted(path for root in PRODUCT for path in root.rglob("*.py"))
    assert files
    found = [
        f"{path.relative_to(REPO)}:{line}: {reason}"
        for path in files
        for line, reason in text_io_problems(ast.parse(path.read_bytes(), filename=str(path)))
    ]
    assert found == []


@pytest.mark.parametrize(
    "source",
    [
        "p.read_text()",
        'p.write_text(s, encoding="utf-8")',
        "open(p)",
        'open(p, "w", encoding="utf-8")',
        'open(p, mode, encoding="utf-8", newline="\\n")',
        'p.open("a", encoding="utf-8")',
        'os.fdopen(fd, "w")',
        'tempfile.NamedTemporaryFile("w", encoding="utf-8")',
        "io.TextIOWrapper(stream)",
        "subprocess.run(command, text=True)",
        "from subprocess import check_output\ncheck_output(command, universal_newlines=True)",
        'io.open(p, "r+", newline="")',
        'open(p, "w", **options)',
        "from m import GUIDE\nGUIDE.read_text()",
        "p.read_text(encoding=None)",
        'subprocess.run(command, errors="replace")',
    ],
)
def test_flags_each_unsafe_form(source: str) -> None:
    assert len(_problems(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        'p.read_text(encoding="utf-8")',
        'p.read_text(encoding="locale", errors="replace")',
        'p.write_text(s, encoding="utf-8", newline="\\n")',
        'open(p, "rb")',
        'open(p, encoding="utf-8")',
        "os.open(p, os.O_RDONLY)",
        'p.open("rb")',
        'os.fdopen(fd, "wb")',
        "tempfile.NamedTemporaryFile()",
        "subprocess.run(command)",
        'subprocess.run(command, text=True, encoding="utf-8")',
        "subprocess.run(command, text=False)",
        'io.TextIOWrapper(stream, encoding="utf-8")',
    ],
)
def test_accepts_each_safe_form(source: str) -> None:
    assert _problems(source) == []
