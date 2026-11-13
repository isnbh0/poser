"""The import rules of `src/poser`: the core's dependencies, its layers, and no cycles."""

import ast
import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
POSER = SRC / "poser"
CORE = POSER / "core"
ADAPTERS = POSER / "adapters"

PORTS = "poser.core.ports"
USE_CASES = (
    "poser.core.session",
    "poser.core.loading",
    "poser.core.rendering",
    "poser.core.checking",
    "poser.core.capturing",
    "poser.core.analysis",
    "poser.core.delivery",
    "poser.core.interpreting",
)
DYNAMIC_IMPORT_LOADERS = {"poser.adapters.loading", "poser.adapters.isolation"}
ADAPTER_LIBRARIES = ("pedalboard", "mido", "yaml", "matplotlib", "scipy", "pyloudnorm")
CLI = POSER / "cli"
COMPOSE = POSER / "compose.py"
PLUGIN_LOADERS = {"poser/adapters/pedalboard/worker.py"}
FCNTL_USERS = {
    "poser/adapters/filestore.py",
    "poser/adapters/pedalboard/instrument.py",
    "poser/adapters/workspace_init.py",
}
LIBRARY_HOMES = {
    "pyloudnorm": {"poser/adapters/measuring.py"},
    "scipy": {"poser/adapters/measuring.py"},
    "matplotlib": {"poser/adapters/spectrogram.py", "poser/adapters/piano_roll.py"},
}


def source_files(root: Path = POSER) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def module_name(path: Path) -> str:
    """Return the dotted name of a module file under `src/`."""
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


MODULES = {module_name(path) for path in source_files()}


def _import_targets(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Return each import in the file as (module, names), failing on a relative import.

    `import a.b` is `("a.b", ())`; `from a.b import c, d` is `("a.b", ("c", "d"))`.
    """
    targets: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            targets.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                pytest.fail(f"{path}:{node.lineno} uses a relative import")
            targets.append((node.module, tuple(alias.name for alias in node.names)))
    return targets


def imports(path: Path) -> set[str]:
    """Return every absolute module name the file imports, anywhere in it."""
    return {module for module, _ in _import_targets(path)}


def edges(path: Path, modules: set[str] = MODULES) -> set[str]:
    """Return the `poser` modules the file loads: `from a.b import c` loads `a.b.c` when that is a
    module, and `a.b` otherwise."""
    loaded: set[str] = set()
    for module, names in _import_targets(path):
        if module.split(".", 1)[0] != "poser":
            continue
        submodules = {f"{module}.{name}" for name in names} & modules
        loaded |= submodules
        if not names or len(submodules) < len(names):
            loaded.add(module)
    return loaded


def core_allows(name: str) -> bool:
    """Return whether a core module may import the module `name`."""
    top = name.split(".", 1)[0]
    return (
        top in sys.stdlib_module_names
        or top == "numpy"
        or name == "poser.core"
        or name.startswith("poser.core.")
    )


def layer(module: str) -> str:
    if module == PORTS:
        return "ports"
    if module in USE_CASES:
        return "use case"
    return "model"


def layer_allows(source: str, target: str) -> bool:
    """Return whether core module `source` may import core module `target`."""
    match layer(target):
        case "model":
            return True
        case "ports":
            return layer(source) == "use case"
        case _:
            return layer(source) == "use case" and (
                USE_CASES.index(target) < USE_CASES.index(source)
            )


def find_cycle(graph: Mapping[str, set[str]]) -> list[str] | None:
    """Return one cycle in the graph as `[a, b, a]`, or None when it has none."""
    done: set[str] = set()
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        if node in path:
            return [*path[path.index(node) :], node]
        if node in done:
            return None
        path.append(node)
        for successor in sorted(graph.get(node, set())):
            if cycle := visit(successor):
                return cycle
        path.pop()
        done.add(node)
        return None

    for node in sorted(graph):
        if cycle := visit(node):
            return cycle
    return None


def has_dynamic_import(path: Path) -> bool:
    """Return whether the file calls `importlib.import_module` or `__import__`, or imports
    `import_module` from `importlib`."""
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            if any(alias.name == "import_module" for alias in node.names):
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "__import__":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "__import__":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "import_module":
                return True
    return False


def core_modules() -> list[tuple[str, Path]]:
    return [(module_name(path), path) for path in source_files(CORE)]


def test_core_imports_only_stdlib_numpy_and_core() -> None:
    refused = [
        f"{module} imports {name}"
        for module, path in core_modules()
        for name in sorted(imports(path))
        if not core_allows(name)
    ]
    assert not refused


def test_every_use_case_exists() -> None:
    assert set(USE_CASES) <= MODULES


def file_system_imports(path: Path) -> list[str]:
    """Return the file's imports of fcntl and shutil, which only adapters may use."""
    return [name for name in sorted(imports(path)) if name.split(".", 1)[0] in {"fcntl", "shutil"}]


def test_core_never_imports_fcntl_or_shutil() -> None:
    refused = [
        f"{module} imports {name}"
        for module, path in core_modules()
        for name in file_system_imports(path)
    ]
    assert not refused


def test_core_layers() -> None:
    refused = [
        f"{module} ({layer(module)}) imports {target} ({layer(target)})"
        for module, path in core_modules()
        for target in sorted(edges(path))
        if target.startswith("poser.core.") and not layer_allows(module, target)
    ]
    assert not refused


def test_no_import_cycles() -> None:
    graph = {module_name(path): edges(path) for path in source_files()}
    cycle = find_cycle(graph)
    assert cycle is None, " -> ".join(cycle)


def test_package_inits_hold_no_imports() -> None:
    for path in source_files():
        if path.name == "__init__.py" and path.parent != POSER:
            assert not _import_targets(path), f"{path} imports"
    api_edges = edges(POSER / "__init__.py")
    assert api_edges
    for target in api_edges:
        assert target.startswith("poser.core."), target
        assert layer(target) == "model", target
    # The packaged showcase is editable user song code, not a library implementation.
    showcase = POSER / "reference_setups" / "surge" / "showcase.py"
    assert edges(showcase) == {"poser"}
    for path in source_files():
        if path != showcase:
            assert "poser" not in edges(path), f"{module_name(path)} imports poser itself"


def test_dynamic_imports_only_in_loaders() -> None:
    found = {module_name(path) for path in source_files() if has_dynamic_import(path)}
    assert found <= DYNAMIC_IMPORT_LOADERS


def test_boundary_check_can_fail(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text("import yaml\nfrom poser.adapters import song_formats\n")
    found = imports(module)
    assert found == {"yaml", "poser.adapters"}
    assert not any(core_allows(name) for name in found)
    assert edges(module, {"poser.adapters.song_formats"}) == {"poser.adapters.song_formats"}

    assert not layer_allows("poser.core.song", PORTS)
    assert not layer_allows(PORTS, "poser.core.loading")
    assert not layer_allows("poser.core.loading", "poser.core.rendering")
    assert layer_allows("poser.core.rendering", "poser.core.loading")

    locking = tmp_path / "locking.py"
    locking.write_text("import os\nimport shutil\nfrom fcntl import flock\n")
    assert file_system_imports(locking) == ["fcntl", "shutil"]

    assert find_cycle({"a": {"b"}, "b": {"a"}}) == ["a", "b", "a"]
    assert find_cycle({"a": {"b"}, "b": set()}) is None

    calls = tmp_path / "calls.py"
    calls.write_text("import importlib\nimportlib.import_module('x')\n")
    assert has_dynamic_import(calls)
    builtin = tmp_path / "builtin.py"
    builtin.write_text("__import__('x')\n")
    assert has_dynamic_import(builtin)
    attribute = tmp_path / "attribute.py"
    attribute.write_text("import builtins\nbuiltins.__import__('x')\n")
    assert has_dynamic_import(attribute)
    named = tmp_path / "named.py"
    named.write_text("from importlib import import_module\n")
    assert has_dynamic_import(named)

    command = tmp_path / "command.py"
    command.write_text("from poser.cli import common\nfrom poser import compose\n")
    assert {"poser.cli.common", "poser.compose"} <= {
        name for name in adapter_edges(command) if adapter_refuses(name)
    }
    assert not adapter_refuses("poser.adapters.isolation")
    assert is_adapter("poser.adapters")
    assert is_adapter("poser.adapters.wav")
    assert not is_adapter("poser.adaptersx")

    plugins = tmp_path / "plugins.py"
    plugins.write_text("import mido\nfrom pedalboard import load_plugin\nimport pedalboard.io\n")
    assert sorted(name for name in imports(plugins) if is_plugin_code(name)) == [
        "mido",
        "pedalboard",
    ]

    relative = tmp_path / "relative.py"
    relative.write_text("from . import x\n")
    with pytest.raises(pytest.fail.Exception):
        imports(relative)


def adapter_refuses(name: str) -> bool:
    """Return whether an adapter may not import the module `name`: a command or the composition."""
    return any(
        name == banned or name.startswith(f"{banned}.") for banned in ("poser.cli", "poser.compose")
    )


def adapter_edges(path: Path) -> set[str]:
    """Return every module the file imports, and every `poser` module it loads."""
    return imports(path) | edges(path)


def test_adapters_never_import_commands_or_composition() -> None:
    refused = [
        f"{module_name(path)} imports {name}"
        for path in source_files(ADAPTERS)
        for name in sorted(adapter_edges(path))
        if adapter_refuses(name)
    ]
    assert not refused


def is_plugin_code(module: str) -> bool:
    """Return whether importing `module` loads plugin code or `mido`: `pedalboard.io` reads audio
    files and loads no plugin."""
    top = module.split(".", 1)[0]
    if top in {"mido", "pedalboard_native"}:
        return True
    return top == "pedalboard" and not (
        module == "pedalboard.io" or module.startswith("pedalboard.io.")
    )


def files_importing(test: Callable[[str], bool]) -> set[str]:
    return {
        path.relative_to(SRC).as_posix()
        for path in source_files()
        if any(test(name) for name in imports(path))
    }


def is_adapter(name: str) -> bool:
    return name == "poser.adapters" or name.startswith("poser.adapters.")


def test_commands_import_no_adapter() -> None:
    refused = [
        f"{module_name(path)} imports {name}"
        for path in source_files(CLI)
        for name in sorted(adapter_edges(path))
        if is_adapter(name)
    ]
    assert source_files(CLI)
    assert not refused


def test_composition_imports_only_the_adapter_loader() -> None:
    found = {name for name in adapter_edges(COMPOSE) if is_adapter(name)}
    assert found == {"poser.adapters.loading"}


def test_importing_the_composition_loads_no_adapter_library() -> None:
    script = "import json, sys\nimport poser.compose\nprint(json.dumps(sorted(sys.modules)))\n"
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    loaded = json.loads(result.stdout)
    assert "poser.compose" in loaded
    assert not [
        name
        for name in loaded
        if any(name == lib or name.startswith(f"{lib}.") for lib in ADAPTER_LIBRARIES)
    ]


def test_only_plugin_workers_load_plugins() -> None:
    assert files_importing(is_plugin_code) == PLUGIN_LOADERS


def test_analysis_libraries_stay_in_their_adapters() -> None:
    for library, homes in LIBRARY_HOMES.items():
        found = files_importing(lambda name, top=library: name.split(".", 1)[0] == top)
        assert found == homes, f"{library} is imported in {sorted(found)}"


def test_only_locking_modules_import_fcntl() -> None:
    found = files_importing(lambda name: name.split(".", 1)[0] == "fcntl")
    assert found == FCNTL_USERS


def test_only_the_wav_adapter_reads_audio_files() -> None:
    readers = {
        path.relative_to(SRC).as_posix()
        for path in source_files()
        for module, names in _import_targets(path)
        if module == "pedalboard.io"
        or module.startswith("pedalboard.io.")
        or (module == "pedalboard" and "io" in names)
    }
    assert readers == {"poser/adapters/wav.py"}


def test_importing_poser_loads_no_adapter_library() -> None:
    statements = "".join(f"import {module}\n" for module, _ in core_modules())
    script = f"import json, sys\nimport poser\n{statements}print(json.dumps(sorted(sys.modules)))\n"
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    loaded = json.loads(result.stdout)
    assert "poser.core.song" in loaded
    assert not [
        name
        for name in loaded
        if any(name == lib or name.startswith(f"{lib}.") for lib in ADAPTER_LIBRARIES)
    ]
