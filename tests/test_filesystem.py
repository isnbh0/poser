"""The file-system facility's contract, tested on whatever `native()` returns on this machine.

Every backend passes this file unchanged on its own platform, so it uses only portable
mechanisms: threads, `sys.executable` subprocesses, and `Popen.kill()`.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from poser.adapters.filesystem import FileSystem, native
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
        if fs.is_link(path):
            found[path.name] = ("link", str(path.resolve()))
        elif path.is_dir():
            found[path.name] = ("dir", "")
            found.update({f"{path.name}/{k}": v for k, v in snapshot(fs, path).items()})
        else:
            found[path.name] = ("file", path.read_text())
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
    with subprocess.Popen([sys.executable, "-c", HOLDER, str(path), str(ready)]) as child:
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
    (target / "report.json").write_text(name)
    return target


def test_make_link_leads_to_its_target_and_creates_nothing_else(
    fs: FileSystem, tmp_path: Path
) -> None:
    target = make_target(tmp_path)
    link = tmp_path / "latest"
    before = {path.name for path in tmp_path.iterdir()}
    fs.make_link(link, "history/a")
    assert {path.name for path in tmp_path.iterdir()} == before | {"latest"}
    assert fs.is_link(link)
    assert link.resolve() == target.resolve()
    assert (link / "report.json").read_text() == "a"


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
        (link / "kept").write_text("mine")
    else:
        link.write_text("mine")
    before = snapshot(fs, tmp_path)
    with pytest.raises(FileExistsError):
        fs.make_link(link, "history/a")
    assert snapshot(fs, tmp_path) == before


def test_is_link_is_false_for_a_directory_a_file_and_a_missing_path(
    fs: FileSystem, tmp_path: Path
) -> None:
    (tmp_path / "directory").mkdir()
    (tmp_path / "file").write_text("x")
    for name in ("directory", "file", "missing"):
        assert not fs.is_link(tmp_path / name), name


def test_remove_link_leaves_its_target_untouched(fs: FileSystem, tmp_path: Path) -> None:
    target = make_target(tmp_path)
    link = tmp_path / "latest"
    fs.make_link(link, "history/a")
    fs.remove_link(link)
    assert not fs.is_link(link)
    assert not link.exists(follow_symlinks=False)
    assert {path.name: path.read_text() for path in target.iterdir()} == {"report.json": "a"}


# Publication


def make_source(root: Path) -> Path:
    source = root / "source"
    (source / "inner").mkdir(parents=True)
    (source / "inner" / "file").write_text("contents")
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
        destination.write_text("mine")
    else:
        destination.mkdir()
        if existing == "directory":
            (destination / "kept").write_text("mine")
    before = snapshot(fs, tmp_path)
    with pytest.raises(FileExistsError):
        fs.publish_directory(source, destination)
    assert snapshot(fs, tmp_path) == before
