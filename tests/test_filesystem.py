"""The file-system facility's contract, tested on whatever `native()` returns on this machine.

Every backend passes this file unchanged on its own platform, so it uses only portable
mechanisms: threads, interpreter subprocesses from `start_python`, and `Popen.kill()`.
"""

import os
import threading
import time
from pathlib import Path

import pytest
from helpers import assert_mode
from interpreter import start_python
from links import LINK_KINDS, make_link
from paths import longest_created, make_directory, make_file, path_of_length

from poser.adapters.filesystem import FileSystem, native
from poser.core.paths import is_link
from poser.core.ports import UnsupportedPlatformError

try:
    native()
except UnsupportedPlatformError as unsupported:
    pytest.skip(str(unsupported), allow_module_level=True)

DEADLINE_S = 5.0
PAUSE_S = 0.2


@pytest.fixture
def fs() -> FileSystem:
    return native()


def snapshot(fs: FileSystem, root: Path) -> dict[str, tuple[str, str]]:
    """Return every entry under `root` by relative path: its kind and its contents, or a link's
    resolved target. Links are not followed."""
    found: dict[str, tuple[str, str]] = {}
    for path in sorted(root.iterdir()):
        if is_link(path):
            found[path.name] = ("link", str(path.resolve()))
        elif path.is_dir():
            found[path.name] = ("dir", "")
            found.update({f"{path.name}/{k}": v for k, v in snapshot(fs, path).items()})
        else:
            found[path.name] = ("file", path.read_text(encoding="utf-8"))
    return found


# Locks


def takes_lock_at_once(fs: FileSystem, path: Path) -> bool:
    """Return whether `fs.lock(path, wait=False)` took the lock or raised BlockingIOError.

    The attempt runs in a daemon thread, so a backend that waits when told not to fails the test
    instead of hanging the suite.
    """
    outcome: list[bool | Exception] = []

    def attempt() -> None:
        try:
            with fs.lock(path, wait=False):
                outcome.append(True)
        except BlockingIOError:
            outcome.append(False)
        except Exception as error:  # noqa: BLE001 - re-raised in the test's thread below.
            outcome.append(error)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(timeout=DEADLINE_S)
    assert not thread.is_alive(), "lock(wait=False) waited instead of raising BlockingIOError"
    [result] = outcome
    if isinstance(result, Exception):
        raise result
    return result


def test_a_held_lock_excludes_another_until_it_exits(fs: FileSystem, tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with fs.lock(path, wait=True):
        assert path.is_file()
        assert not takes_lock_at_once(fs, path)
    assert takes_lock_at_once(fs, path)


def test_a_waiting_lock_is_taken_once_the_holder_exits(fs: FileSystem, tmp_path: Path) -> None:
    path = tmp_path / "lock"
    held, release, acquired = threading.Event(), threading.Event(), threading.Event()

    def hold() -> None:
        with fs.lock(path, wait=True):
            held.set()
            release.wait(timeout=DEADLINE_S)

    def wait() -> None:
        with fs.lock(path, wait=True):
            acquired.set()

    holder = threading.Thread(target=hold, daemon=True)
    waiter = threading.Thread(target=wait, daemon=True)
    holder.start()
    try:
        assert held.wait(timeout=DEADLINE_S)
        waiter.start()
        assert not acquired.wait(timeout=PAUSE_S)
    finally:
        release.set()
    assert acquired.wait(timeout=DEADLINE_S)
    holder.join(timeout=DEADLINE_S)
    waiter.join(timeout=DEADLINE_S)


HOLDER = """
import sys, time
from pathlib import Path
from poser.adapters.filesystem import native

with native().lock(Path(sys.argv[1]), wait=True):
    Path(sys.argv[2]).write_text("ready")
    time.sleep(60)
"""


def test_a_lock_ends_when_its_process_is_killed(fs: FileSystem, tmp_path: Path) -> None:
    path, ready = tmp_path / "lock", tmp_path / "ready"
    with start_python("-c", HOLDER, str(path), str(ready)) as child:
        try:
            deadline = time.monotonic() + DEADLINE_S
            while not ready.exists():
                assert child.poll() is None, "the holder ended before it locked"
                assert time.monotonic() < deadline, "the holder never locked"
                time.sleep(0.01)
            assert not takes_lock_at_once(fs, path)
        finally:
            child.kill()
            child.wait()
    deadline = time.monotonic() + DEADLINE_S
    while not takes_lock_at_once(fs, path):
        assert time.monotonic() < deadline, "the lock outlived its process"
        time.sleep(0.05)


# Sync


def test_sync_accepts_a_written_file_and_a_directory(fs: FileSystem, tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_bytes(b"data")
    fs.sync(file)
    fs.sync(tmp_path)
    assert file.read_bytes() == b"data"


# Links


def make_target(root: Path, name: str = "a") -> Path:
    target = root / "history" / name
    target.mkdir(parents=True)
    (target / "report.json").write_text(name, encoding="utf-8", newline="\n")
    return target


def test_make_link_leads_to_its_target_and_creates_nothing_else(
    fs: FileSystem, tmp_path: Path
) -> None:
    target = make_target(tmp_path)
    link = tmp_path / "latest"
    before = {path.name for path in tmp_path.iterdir()}
    fs.make_link(link, "history/a")
    assert {path.name for path in tmp_path.iterdir()} == before | {"latest"}
    assert is_link(link)
    assert link.resolve() == target.resolve()
    assert (link / "report.json").read_text(encoding="utf-8") == "a"


@pytest.mark.parametrize("existing", ["link", "directory", "file"])
def test_make_link_refuses_anything_already_there(
    fs: FileSystem, tmp_path: Path, existing: str
) -> None:
    make_target(tmp_path, "a")
    make_target(tmp_path, "b")
    link = tmp_path / "latest"
    if existing == "link":
        fs.make_link(link, "history/b")
    elif existing == "directory":
        link.mkdir()
        (link / "kept").write_text("mine", encoding="utf-8", newline="\n")
    else:
        link.write_text("mine", encoding="utf-8", newline="\n")
    before = snapshot(fs, tmp_path)
    with pytest.raises(FileExistsError):
        fs.make_link(link, "history/a")
    assert snapshot(fs, tmp_path) == before


def test_remove_link_leaves_its_target_untouched(fs: FileSystem, tmp_path: Path) -> None:
    target = make_target(tmp_path)
    link = tmp_path / "latest"
    fs.make_link(link, "history/a")
    fs.remove_link(link)
    assert not is_link(link)
    assert not link.exists(follow_symlinks=False)
    assert {path.name: path.read_text(encoding="utf-8") for path in target.iterdir()} == {
        "report.json": "a"
    }


# Publication


def make_source(root: Path) -> Path:
    source = root / "source"
    (source / "inner").mkdir(parents=True)
    (source / "inner" / "file").write_text("contents", encoding="utf-8", newline="\n")
    return source


def test_publish_directory_moves_a_directory_and_its_contents(
    fs: FileSystem, tmp_path: Path
) -> None:
    source = make_source(tmp_path)
    destination = tmp_path / "destination"
    fs.publish_directory(source, destination)
    assert not source.exists()
    assert snapshot(fs, destination) == {"inner": ("dir", ""), "inner/file": ("file", "contents")}


@pytest.mark.parametrize("existing", ["empty directory", "directory", "file"])
def test_publish_directory_refuses_an_existing_destination(
    fs: FileSystem, tmp_path: Path, existing: str
) -> None:
    source = make_source(tmp_path)
    destination = tmp_path / "destination"
    if existing == "file":
        destination.write_text("mine", encoding="utf-8", newline="\n")
    else:
        destination.mkdir()
        if existing == "directory":
            (destination / "kept").write_text("mine", encoding="utf-8", newline="\n")
    before = snapshot(fs, tmp_path)
    with pytest.raises(FileExistsError):
        fs.publish_directory(source, destination)
    assert snapshot(fs, tmp_path) == before


def test_rename_directory_moves_a_directory_and_its_contents(
    fs: FileSystem, tmp_path: Path
) -> None:
    source = make_source(tmp_path)
    destination = tmp_path / "destination"
    fs.rename_directory(source, destination)
    assert not source.exists()
    assert snapshot(fs, destination) == {"inner": ("dir", ""), "inner/file": ("file", "contents")}


def test_rename_directory_refuses_a_directory_holding_anything(
    fs: FileSystem, tmp_path: Path
) -> None:
    source = make_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "kept").write_text("mine", encoding="utf-8", newline="\n")
    before = snapshot(fs, tmp_path)
    with pytest.raises(FileExistsError):
        fs.rename_directory(source, destination)
    assert snapshot(fs, tmp_path) == before


# Replacing and removing


def write(path: Path, text: str, mode: int | None = None) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    if mode is not None:
        path.chmod(mode)
    return path


@pytest.mark.parametrize("mode", [0o644, 0o444])
def test_replace_file_replaces_a_file_even_a_read_only_one(
    fs: FileSystem, tmp_path: Path, mode: int
) -> None:
    source = write(tmp_path / "new", "new", 0o644)
    destination = write(tmp_path / "old", "old", mode)
    fs.replace_file(source, destination)
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "new"
    assert_mode(destination, 0o644)


def test_a_failed_replace_file_leaves_a_read_only_destination_as_it_was(
    fs: FileSystem, tmp_path: Path
) -> None:
    destination = write(tmp_path / "old", "old", 0o444)
    with pytest.raises(FileNotFoundError):
        fs.replace_file(tmp_path / "missing", destination)
    assert destination.read_text(encoding="utf-8") == "old"
    assert_mode(destination, 0o444)


@pytest.mark.parametrize("mode", [0o644, 0o444])
def test_remove_file_removes_a_file_even_a_read_only_one(
    fs: FileSystem, tmp_path: Path, mode: int
) -> None:
    path = write(tmp_path / "file", "data", mode)
    fs.remove_file(path)
    assert list(tmp_path.iterdir()) == []


def test_remove_file_of_nothing_raises_file_not_found(fs: FileSystem, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fs.remove_file(tmp_path / "missing")


@pytest.mark.parametrize("kind", LINK_KINDS)
def test_remove_tree_removes_read_only_files_and_enters_no_link(
    fs: FileSystem, tmp_path: Path, kind: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside / "kept", "mine", 0o444)
    tree = make_source(tmp_path)
    write(tree / "inner" / "read-only", "data", 0o444)
    make_link(kind, tree / "inner" / "link", outside)
    fs.remove_tree(tree)
    assert not tree.exists(follow_symlinks=False)
    assert snapshot(fs, outside) == {"kept": ("file", "mine")}


def test_remove_tree_of_nothing_raises_file_not_found(fs: FileSystem, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fs.remove_tree(tmp_path / "missing")


@pytest.mark.parametrize("fill", ["d", "\u20ac", "\U0001d11e"], ids=["ascii", "euro", "g-clef"])
def test_the_path_limit_is_where_the_system_refuses(
    fs: FileSystem, tmp_path: Path, fill: str
) -> None:
    """The longest directory and file paths this process creates, of `fill` characters, reach
    the reported limit, and for an exact limit one character more passes it. `d` is one byte and
    one UTF-16 unit, `€` three bytes and one unit, and `𝄞` four bytes and two units, so a limit
    counted in the wrong unit fails. The Windows workflow sets POSER_EXPECT_LONG_PATHS to the
    long-paths switch it set."""
    limit = fs.path_limit()
    if (expected := os.environ.get("POSER_EXPECT_LONG_PATHS")) is not None:
        assert (limit.lift is None) == (expected == "1"), limit
    for name, create, most in (
        ("d", make_directory, limit.directory),
        ("f", make_file, limit.file),
    ):
        base = tmp_path / name
        longest = longest_created(base, create, most + 100, fill)
        lengths = [
            limit.unit.length(str(path_of_length(base, length, fill)))
            for length in (longest, longest + 1)
        ]
        fits = lengths[0] <= most < lengths[1] if limit.exact else most <= lengths[0]
        assert fits, (name, lengths, most, limit.exact)
