"""The file-system facility's contract, tested on whatever `native()` returns on this machine.

Every backend passes this file unchanged on its own platform, so it uses only portable
mechanisms: threads, interpreter subprocesses from `start_python`, and `Popen.kill()`.
"""

import errno
import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from helpers import assert_mode
from interpreter import start_python
from links import LINK_KINDS, make_link
from paths import longest_created, make_directory, make_file, path_of_length

from poser.adapters import filesystem
from poser.adapters.filesystem import LINK_PROBE_PREFIX, FileSystem, native, require_workspace_links
from poser.core.paths import is_link
from poser.core.ports import ConfigError, UnsupportedPlatformError

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


# Workspace link capabilities


@pytest.fixture
def probe_directory(fs: FileSystem, tmp_path: Path) -> Path:
    directory = tmp_path / "workspace"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside / "sentinel", "external owner")
    write(directory / "owner", "owner contents", 0o444)
    write(directory / f"{LINK_PROBE_PREFIX}owner-file", "owner file")
    abandoned = directory / f"{LINK_PROBE_PREFIX}owner-directory"
    abandoned.mkdir()
    write(abandoned / "sentinel", "owner directory")
    fs.make_link(directory / f"{LINK_PROBE_PREFIX}owner-link", "../outside")
    return directory


def inject_probe_failure(
    fs: FileSystem,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    failure: BaseException,
) -> list[str]:
    """Inject primitive failures, not a simulated filesystem format or a private probe helper."""
    calls: list[str] = []
    make_directory_link, hard_link = fs.make_link, os.link

    def directory_link(link: Path, target: str) -> None:
        calls.append("directory links")
        if capability == "directory links":
            raise failure
        make_directory_link(link, target)

    def file_link(source: Path, destination: Path) -> None:
        if (
            destination.parent.name.startswith(LINK_PROBE_PREFIX)
            and destination.name == "hard-link"
        ):
            calls.append("hard links")
            if capability == "hard links":
                raise failure
        hard_link(source, destination)

    monkeypatch.setattr(fs, "make_link", directory_link)
    monkeypatch.setattr(os, "link", file_link)
    return calls


def test_workspace_link_probe_succeeds_without_artifacts(
    fs: FileSystem, tmp_path: Path, probe_directory: Path
) -> None:
    before = snapshot(fs, tmp_path)
    owner = probe_directory / "owner"
    metadata = owner.stat()
    require_workspace_links(fs, probe_directory)
    assert snapshot(fs, tmp_path) == before
    after = owner.stat()
    assert os.path.samestat(metadata, after)
    assert (after.st_mode, after.st_mtime_ns) == (metadata.st_mode, metadata.st_mtime_ns)


@pytest.mark.parametrize("capability", ["directory links", "hard links"])
def test_workspace_link_probe_refuses_missing_capability(
    fs: FileSystem,
    tmp_path: Path,
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
) -> None:
    """These injected primitive failures are stand-ins, not native exFAT/FAT coverage."""
    before = snapshot(fs, tmp_path)
    failure = OSError(errno.EACCES, "injected primitive refusal")
    calls = inject_probe_failure(fs, monkeypatch, capability, failure)
    with pytest.raises(ConfigError) as caught:
        require_workspace_links(fs, probe_directory)
    assert str(probe_directory) in str(caught.value)
    assert f"cannot use required {capability}" in str(caught.value)
    assert str(failure) in str(caught.value)
    assert caught.value.__cause__ is failure
    assert calls == (
        ["directory links"]
        if capability == "directory links"
        else ["directory links", "hard links"]
    )
    assert snapshot(fs, tmp_path) == before


@pytest.mark.parametrize(
    ("capability", "substitute"),
    [
        ("directory links", "noop"),
        ("directory links", "ordinary-directory"),
        ("directory links", "wrong-target"),
        ("directory links", "wrong-sentinel"),
        ("directory links", "remove-noop"),
        ("directory links", "remove-target-replaced"),
        ("directory links", "remove-sentinel-changed"),
        ("hard links", "noop"),
        ("hard links", "copy"),
        ("hard links", "symlink"),
        ("hard links", "wrong-file"),
        ("hard links", "wrong-sentinel"),
    ],
)
def test_workspace_link_probe_rejects_noop_or_wrong_object(
    fs: FileSystem,
    tmp_path: Path,
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    substitute: str,
) -> None:
    """Successful-return stand-ins must not satisfy the real result checks."""
    before = snapshot(fs, tmp_path)
    make_directory_link, remove_directory_link, hard_link = fs.make_link, fs.remove_link, os.link

    def directory_link(link: Path, target: str) -> None:
        if substitute == "noop":
            return
        if substitute == "ordinary-directory":
            shutil.copytree(link.parent / target, link)
            return
        if substitute == "wrong-target":
            wrong = link.parent / "wrong-target"
            shutil.copytree(link.parent / target, wrong)
            make_directory_link(link, wrong.name)
            return
        make_directory_link(link, target)
        (link / "sentinel").write_bytes(b"wrong sentinel")

    def remove_link(link: Path) -> None:
        if substitute == "remove-noop":
            return
        target = link.resolve()
        sentinel = (target / "sentinel").read_bytes()
        remove_directory_link(link)
        if substitute == "remove-target-replaced":
            target.rename(target.with_name("original-target"))
            target.mkdir()
            (target / "sentinel").write_bytes(sentinel)
        else:
            (target / "sentinel").write_bytes(b"changed sentinel")

    def file_link(source: Path, destination: Path) -> None:
        if not (
            destination.parent.name.startswith(LINK_PROBE_PREFIX)
            and destination.name == "hard-link"
        ):
            hard_link(source, destination)
        elif substitute == "copy":
            shutil.copyfile(source, destination)
        elif substitute == "symlink":
            destination.symlink_to(source)
        elif substitute == "wrong-file":
            wrong = source.with_name("other-file")
            shutil.copyfile(source, wrong)
            hard_link(wrong, destination)
        elif substitute == "wrong-sentinel":
            hard_link(source, destination)
            source.write_bytes(b"wrong sentinel")
        else:
            assert substitute == "noop"

    if capability == "hard links":
        monkeypatch.setattr(os, "link", file_link)
    elif substitute.startswith("remove-"):
        monkeypatch.setattr(fs, "remove_link", remove_link)
    else:
        monkeypatch.setattr(fs, "make_link", directory_link)
    with pytest.raises(ConfigError) as caught:
        require_workspace_links(fs, probe_directory)
    assert f"cannot use required {capability}" in str(caught.value)
    assert str(probe_directory) in str(caught.value)
    assert isinstance(caught.value.__cause__, OSError)
    assert snapshot(fs, tmp_path) == before


def test_workspace_link_probe_allocation_failure_is_not_a_capability_verdict(
    fs: FileSystem, tmp_path: Path, probe_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(fs, tmp_path)
    failure = OSError(errno.ENOSPC, "injected allocation failure")
    allocations: list[tuple[str, Path]] = []
    operations: list[Path] = []

    def allocate(*, prefix: str, dir: Path) -> str:  # noqa: A002 - mkdtemp's keyword.
        allocations.append((prefix, dir))
        raise failure

    monkeypatch.setattr(filesystem.tempfile, "mkdtemp", allocate)
    monkeypatch.setattr(fs, "make_link", lambda link, target: operations.append(link))
    monkeypatch.setattr(fs, "remove_tree", operations.append)
    with pytest.raises(ConfigError) as caught:
        require_workspace_links(fs, probe_directory)
    assert allocations == [(LINK_PROBE_PREFIX, probe_directory)]
    assert operations == []
    assert str(probe_directory) in str(caught.value)
    assert "cannot check" in str(caught.value)
    assert "cannot use required" not in str(caught.value)
    assert str(failure) in str(caught.value)
    assert caught.value.__cause__ is failure
    assert snapshot(fs, tmp_path) == before


@pytest.mark.parametrize("stage", ["lstat", "is_link"])
@pytest.mark.parametrize("failure_kind", ["interrupt", "permission"])
def test_workspace_link_probe_retains_unverified_namespace_on_ownership_failure(
    fs: FileSystem,
    tmp_path: Path,
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    failure_kind: str,
) -> None:
    before = snapshot(fs, tmp_path)
    failure = (
        KeyboardInterrupt("injected ownership interruption")
        if failure_kind == "interrupt"
        else PermissionError(errno.EACCES, "injected ownership inspection failure")
    )
    allocated: list[Path] = []
    removals: list[Path] = []
    mkdtemp, lstat, check_link = filesystem.tempfile.mkdtemp, Path.lstat, filesystem.is_link

    def allocate(*, prefix: str, dir: Path) -> str:  # noqa: A002 - mkdtemp's keyword.
        result = mkdtemp(prefix=prefix, dir=dir)
        allocated.append(Path(result))
        return result

    def inspect(path: Path) -> os.stat_result:
        if path in allocated:
            raise failure
        return lstat(path)

    def inspect_link(path: Path) -> bool:
        if path in allocated:
            raise failure
        return check_link(path)

    with monkeypatch.context() as patch:
        patch.setattr(filesystem.tempfile, "mkdtemp", allocate)
        patch.setattr(fs, "remove_tree", removals.append)
        if stage == "lstat":
            patch.setattr(Path, "lstat", inspect)
        else:
            patch.setattr(filesystem, "is_link", inspect_link)
        expected = KeyboardInterrupt if failure_kind == "interrupt" else ConfigError
        with pytest.raises(expected) as caught:
            require_workspace_links(fs, probe_directory)
    [probe] = allocated
    assert removals == []
    assert probe.parent == probe_directory
    assert probe.name.startswith(LINK_PROBE_PREFIX)
    assert probe.is_dir()
    assert list(probe.iterdir()) == []
    if failure_kind == "interrupt":
        assert caught.value is failure
        assert any(str(probe) in note and "inspect" in note for note in failure.__notes__)
    else:
        message = str(caught.value)
        assert str(probe) in message
        assert str(failure) in message
        assert "not removed" in message
        assert "inspect" in message
        assert caught.value.__cause__ is failure
    after = snapshot(fs, tmp_path)
    assert {name: after[name] for name in before} == before


@pytest.mark.parametrize("capability", ["directory links", "hard links"])
@pytest.mark.parametrize("cleanup_fails", [False, True], ids=["cleaned", "cleanup-error"])
def test_workspace_link_probe_cleans_up_on_interrupt(
    fs: FileSystem,
    tmp_path: Path,
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    cleanup_fails: bool,
) -> None:
    before = snapshot(fs, tmp_path)
    interrupt = KeyboardInterrupt("injected probe interruption")
    cleanup = OSError(errno.EACCES, "injected cleanup failure")
    removed: list[Path] = []
    remove_tree = fs.remove_tree
    inject_probe_failure(fs, monkeypatch, capability, interrupt)

    def clean(path: Path) -> None:
        removed.append(path)
        if cleanup_fails:
            raise cleanup
        remove_tree(path)

    monkeypatch.setattr(fs, "remove_tree", clean)
    with pytest.raises(KeyboardInterrupt) as caught:
        require_workspace_links(fs, probe_directory)
    assert caught.value is interrupt
    [probe] = removed
    assert probe.parent == probe_directory
    assert probe.name.startswith(LINK_PROBE_PREFIX)
    if cleanup_fails:
        assert probe.is_dir()
        assert any(str(probe) in note and str(cleanup) in note for note in interrupt.__notes__)
        after = snapshot(fs, tmp_path)
        assert {name: after[name] for name in before} == before
    else:
        assert not probe.exists(follow_symlinks=False)
        assert snapshot(fs, tmp_path) == before


@pytest.mark.parametrize("capability", ["directory links", "hard links", "working"])
@pytest.mark.parametrize("cleanup_kind", ["error", "noop"])
def test_workspace_link_probe_cleanup_failure_is_visible(
    fs: FileSystem,
    tmp_path: Path,
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    cleanup_kind: str,
) -> None:
    before = snapshot(fs, tmp_path)
    primary = OSError(errno.EPERM, "injected capability failure")
    cleanup = OSError(errno.EACCES, "injected cleanup failure")
    attempted: list[Path] = []
    inject_probe_failure(fs, monkeypatch, capability, primary)

    def clean(path: Path) -> None:
        attempted.append(path)
        if cleanup_kind == "error":
            raise cleanup

    monkeypatch.setattr(fs, "remove_tree", clean)
    with pytest.raises(ConfigError) as caught:
        require_workspace_links(fs, probe_directory)
    [probe] = attempted
    message = str(caught.value)
    assert probe.parent == probe_directory
    assert probe.name.startswith(LINK_PROBE_PREFIX)
    assert probe.is_dir()
    assert str(probe_directory) in message
    assert str(probe) in message
    assert "cannot remove link probe" in message
    if cleanup_kind == "error":
        assert str(cleanup) in message
    else:
        assert "without removing" in message
    cause = caught.value.__cause__
    if capability != "working":
        assert f"cannot use required {capability}" in message
        assert str(primary) in message
        assert isinstance(cause, ConfigError)
        assert str(cause) in message
        assert cause.__cause__ is primary
    else:
        assert "cannot use required" not in message
        assert isinstance(cause, OSError)
        if cleanup_kind == "error":
            assert cause is cleanup
    after = snapshot(fs, tmp_path)
    assert {name: after[name] for name in before} == before


@pytest.mark.parametrize("replacement", ["directory", "link"])
def test_workspace_link_probe_never_cleans_a_substituted_root(
    fs: FileSystem,
    tmp_path: Path,
    probe_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    """Substitute at the cleanup-entry ownership check, not after that check in a hostile race."""
    before = snapshot(fs, tmp_path)
    outside = tmp_path / "outside"
    saved = tmp_path / "original-probe"
    armed: list[Path] = []
    replaced: list[Path] = []
    removals: list[Path] = []
    hard_link, lstat, remove_tree = os.link, Path.lstat, fs.remove_tree

    def file_link(source: Path, destination: Path) -> None:
        hard_link(source, destination)
        if destination.parent.parent == probe_directory and destination.name == "hard-link":
            armed.append(destination.parent)

    def inspect(path: Path) -> os.stat_result:
        if path in armed:
            armed.remove(path)
            path.rename(saved)
            if replacement == "directory":
                path.mkdir()
                write(path / "sentinel", "replacement owner")
            else:
                fs.make_link(path, "../outside")
            replaced.append(path)
        return lstat(path)

    def clean(path: Path) -> None:
        removals.append(path)
        remove_tree(path)

    monkeypatch.setattr(os, "link", file_link)
    monkeypatch.setattr(Path, "lstat", inspect)
    monkeypatch.setattr(fs, "remove_tree", clean)
    with pytest.raises(ConfigError) as caught:
        require_workspace_links(fs, probe_directory)
    [probe] = replaced
    assert removals == []
    assert str(probe) in str(caught.value)
    assert "changed" in str(caught.value)
    assert "refusing" in str(caught.value)
    assert saved.is_dir()
    assert (saved / "target" / "sentinel").is_file()
    assert (saved / "file").samefile(saved / "hard-link")
    if replacement == "directory":
        assert not is_link(probe)
        assert (probe / "sentinel").read_text(encoding="utf-8") == "replacement owner"
    else:
        assert is_link(probe)
        assert probe.resolve() == outside.resolve()
    after = snapshot(fs, tmp_path)
    assert {name: after[name] for name in before} == before


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
