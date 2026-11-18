"""poser's code and its tests name the encoding of every text they read or write, and the newline
of every text they write, so their text bytes are the same whatever the platform's defaults. On
Windows, text I/O without an encoding uses the ANSI code page, and text written without a newline
gets CRLF."""

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CHECKED = (
    REPO / "src" / "poser",
    REPO / "packages" / "poser-scsynth" / "src",
    REPO / "scripts",
    REPO / "tests",
)
WRITING_MODES = frozenset("wax+")
SUBPROCESS_TEXT_CALLS = frozenset(
    f"subprocess.{name}" for name in ("run", "Popen", "check_output", "check_call", "call")
)
TEMPORARY_FILES = frozenset(
    f"tempfile.{name}" for name in ("NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile")
)
# Always text, and taking an `encoding` since Python 3.11.
SUBPROCESS_OUTPUT_CALLS = frozenset(("subprocess.getoutput", "subprocess.getstatusoutput"))
# Each opens its file in text mode with no newline, so it writes the platform's line ending.
LOGGING_FILE_HANDLERS = frozenset(
    ("FileHandler", "RotatingFileHandler", "TimedRotatingFileHandler", "WatchedFileHandler")
)
# Unbound methods of `Path`, called with the path first.
UNBOUND_PATH_METHODS = frozenset(
    f"pathlib.Path.{name}" for name in ("read_text", "write_text", "open")
)


@dataclass(frozen=True)
class Names:
    """What the names in a file stand for. `bound` maps each name an import binds, and each name
    assigned a dotted name rooted at one, such as `write_text = Path.write_text`, to its dotted
    name. `modules` holds the dotted names `import` statements name, such as `logging` and
    `logging.handlers` for `import logging.handlers`; any other object may have methods."""

    bound: dict[str, str]
    modules: frozenset[str]

    def dotted(self, node: ast.expr) -> str | None:
        """Return the dotted name of a chain of attributes rooted at a bound name, or None."""
        attributes: list[str] = []
        while isinstance(node, ast.Attribute):
            attributes.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name) or node.id not in self.bound:
            return None
        return ".".join([self.bound[node.id], *reversed(attributes)])


def _names(tree: ast.Module) -> Names:
    bound: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                modules.update(".".join(parts[: end + 1]) for end in range(len(parts)))
                if alias.asname:
                    bound[alias.asname] = alias.name
                else:
                    bound[parts[0]] = parts[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    names = Names(bound, frozenset(modules))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            dotted = names.dotted(node.value)
            if dotted is not None and len(targets) == 1 and isinstance(targets[0], ast.Name):
                bound[targets[0].id] = dotted
    return names


def _called(func: ast.expr, names: Names) -> tuple[str, bool]:
    """Return the dotted name a call targets and whether it is a method of an object that is not
    a module. A method's name is `.attr`; an unbound method of `Path` keeps its dotted name."""
    if isinstance(func, ast.Name):
        return names.bound.get(func.id, func.id), False
    if isinstance(func, ast.Attribute):
        if func.attr in ("read_text", "write_text"):
            return f".{func.attr}", True
        receiver = names.dotted(func.value)
        if receiver is not None:
            dotted = f"{receiver}.{func.attr}"
            if receiver in names.modules or dotted in UNBOUND_PATH_METHODS:
                return dotted, False
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


def _call_problem(call: ast.Call, names: Names) -> str | None:
    name, is_method = _called(call.func, names)
    if name in UNBOUND_PATH_METHODS:
        # Checked as the bound call, without the path that comes first.
        name, is_method = f".{name.rsplit('.', 1)[1]}", True
        call = ast.Call(call.func, call.args[1:], call.keywords)
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
        or name in SUBPROCESS_OUTPUT_CALLS
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
    elif name.rsplit(".", 1)[-1] in LOGGING_FILE_HANDLERS:
        problem = (
            f"{name.lstrip('.')}() writes the platform's newline, which it cannot name; give a "
            "logging.StreamHandler a stream opened with an encoding and a newline"
        )
    elif name == "logging.basicConfig" and "filename" in {k.arg for k in call.keywords}:
        problem = (
            "logging.basicConfig(filename=...) writes the platform's newline, which it cannot "
            "name; pass a stream opened with an encoding and a newline"
        )
    elif name == "os.popen":
        problem = "os.popen() cannot name an encoding; use subprocess.run with one"
    elif name in SUBPROCESS_OUTPUT_CALLS:
        problem = f"{name}() names no encoding" if _missing(call, ("encoding",)) else None
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
    naming its encoding, or writes text without naming its newline.

    It sees a call by the name it is made through: an import, an assignment of a dotted name, or a
    method name. It does not see a text writer named by a string, such as a handler class in
    `logging.config.dictConfig`, or a callable passed in as an argument.
    """
    names = _names(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    return sorted(
        (call.lineno, problem) for call in calls if (problem := _call_problem(call, names))
    )


def _problems(source: str) -> list[tuple[int, str]]:
    prelude = "import io\nimport logging\nimport os\nimport subprocess\nimport tempfile\n"
    return text_io_problems(ast.parse(prelude + source))


def test_text_io_names_encoding_and_newline() -> None:
    files = sorted(path for root in CHECKED for path in root.rglob("*.py"))
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
        'logging.FileHandler(p, encoding="utf-8")',
        "import logging.handlers\nlogging.handlers.RotatingFileHandler(p)",
        "from logging.handlers import TimedRotatingFileHandler\nTimedRotatingFileHandler(p)",
        "from logging import handlers\nhandlers.WatchedFileHandler(p)",
        'logging.basicConfig(filename=p, encoding="utf-8")',
        'os.popen(command, "r")',
        "from os import popen\npopen(command)",
        "subprocess.getoutput(command)",
        "from subprocess import getstatusoutput\ngetstatusoutput(command)",
        "from pathlib import Path\nPath.write_text(p, s)",
        'from pathlib import Path\nPath.write_text(p, s, encoding="utf-8")',
        "from pathlib import Path\nwrite_text = Path.write_text\nwrite_text(p, s)",
        "from pathlib import Path\nread_text = Path.read_text\nread_text(p)",
        'from pathlib import Path\nPath.open(p, "w", encoding="utf-8")',
        "import pathlib\npathlib.Path.open(p)",
        'from m import GUIDE\nGUIDE.open("w", encoding="utf-8")',
        "from m import GUIDE\nGUIDE.open()",
        'import m\nm.GUIDE.open("a", encoding="utf-8")',
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
        "logging.StreamHandler(stream)",
        "logging.basicConfig(stream=stream)",
        'subprocess.getoutput(command, encoding="utf-8")',
        'from pathlib import Path\nPath.write_text(p, s, encoding="utf-8", newline="\\n")',
        (
            "from pathlib import Path\nwrite_text = Path.write_text\n"
            'write_text(p, s, encoding="utf-8", newline="\\n")'
        ),
        'from pathlib import Path\nPath.open(p, "rb")',
        'from m import GUIDE\nGUIDE.open("rb")',
        'from m import GUIDE\nGUIDE.open("w", encoding="utf-8", newline="\\n")',
        'import gzip\ngzip.open(p, "rt", encoding="utf-8")',
    ],
)
def test_accepts_each_safe_form(source: str) -> None:
    assert _problems(source) == []
