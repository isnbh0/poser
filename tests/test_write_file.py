"""`write_file`, the one way poser publishes a file whole, tested through a file system that
records what it is asked to do and can be told to fail."""

import stat
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from helpers import assert_mode

from poser.adapters.filesystem import FileSystem, native, write_file
from poser.core.paths import PathLimit
from poser.core.ports import UnsupportedPlatformError

try:
    native()
except UnsupportedPlatformError as unsupported:
    pytest.skip(str(unsupported), allow_module_level=True)


class Recording:
    """The native file system, recording each call `write_file` makes and failing on request."""

    def __init__(self) -> None:
        self.inner: FileSystem = native()
        self.calls: list[tuple[str, Path]] = []
        self.writable_when_synced: list[bool] = []
        self.failing: set[str] = set()

    def _record(self, name: str, path: Path) -> None:
        self.calls.append((name, path))
        if name in self.failing:
            raise OSError(f"{name} failed")

    @contextmanager
    def lock(self, path: Path, *, wait: bool) -> Generator[None]:
        with self.inner.lock(path, wait=wait):
            yield

    def sync(self, path: Path) -> None:
        self._record("sync", path)
        # Windows syncs through a writable descriptor, so the file must still be writable.
        self.writable_when_synced.append(bool(stat.S_IMODE(path.stat().st_mode) & stat.S_IWUSR))
        self.inner.sync(path)

    def make_link(self, link: Path, target: str) -> None:
        self.inner.make_link(link, target)

    def remove_link(self, path: Path) -> None:
        self.inner.remove_link(path)

    def publish_directory(self, source: Path, destination: Path) -> None:
        self.inner.publish_directory(source, destination)

    def rename_directory(self, source: Path, destination: Path) -> None:
        self.inner.rename_directory(source, destination)

    def replace_file(self, source: Path, destination: Path) -> None:
        self._record("replace_file", destination)
        self.inner.replace_file(source, destination)

    def remove_file(self, path: Path) -> None:
        self._record("remove_file", path)
        self.inner.remove_file(path)

    def remove_tree(self, path: Path) -> None:
        self.inner.remove_tree(path)

    def path_limit(self) -> PathLimit:
        return self.inner.path_limit()


def text(contents: str) -> Callable[[Path], None]:
    def fill(temp: Path) -> None:
        temp.write_text(contents, encoding="utf-8", newline="\n")

    return fill


def names(directory: Path) -> list[str]:
    return sorted(entry.name for entry in directory.iterdir())


def test_a_read_only_file_is_synced_before_its_mode_is_set(tmp_path: Path) -> None:
    fs = Recording()
    path = tmp_path / "file"
    write_file(fs, path, text("new"), mode=0o444)
    assert fs.writable_when_synced == [True]
    assert_mode(path, 0o444)
    assert path.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("replace", [True, False])
def test_the_file_is_published_through_the_facility(tmp_path: Path, *, replace: bool) -> None:
    fs = Recording()
    path = tmp_path / "file"
    if replace:
        path.write_text("old", encoding="utf-8", newline="\n")
    write_file(fs, path, text("new"), replace=replace)
    assert path.read_text(encoding="utf-8") == "new"
    assert names(tmp_path) == ["file"]
    steps = [name for name, _ in fs.calls]
    assert steps == (["sync", "replace_file"] if replace else ["sync", "remove_file"])


def test_a_failed_replace_keeps_the_old_file_and_removes_the_temp(tmp_path: Path) -> None:
    fs = Recording()
    fs.failing.add("replace_file")
    path = tmp_path / "file"
    path.write_text("old", encoding="utf-8", newline="\n")
    with pytest.raises(OSError, match="replace_file failed"):
        write_file(fs, path, text("new"))
    assert path.read_text(encoding="utf-8") == "old"
    assert names(tmp_path) == ["file"]
    assert fs.calls[-1][0] == "remove_file"


def test_a_failing_fill_publishes_nothing_and_removes_the_temp(tmp_path: Path) -> None:
    fs = Recording()

    def fail(temp: Path) -> None:
        temp.write_text("partial", encoding="utf-8", newline="\n")
        raise RuntimeError("fill failed")

    with pytest.raises(RuntimeError, match="fill failed"):
        write_file(fs, tmp_path / "file", fail)
    assert names(tmp_path) == []
    assert [name for name, _ in fs.calls] == ["remove_file"]


def test_creating_refuses_anything_already_there(tmp_path: Path) -> None:
    fs = Recording()
    path = tmp_path / "file"
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        write_file(fs, path, text("new"), replace=False)
    assert names(tmp_path) == ["file"]
    assert path.is_symlink()


def test_replacing_through_a_symbolic_link_keeps_the_link(tmp_path: Path) -> None:
    fs = Recording()
    real = tmp_path / "real"
    real.write_text("old", encoding="utf-8", newline="\n")
    link = tmp_path / "link"
    link.symlink_to(real)
    write_file(fs, link, text("new"))
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "new"
    assert names(tmp_path) == ["link", "real"]


def test_a_leftover_after_publishing_is_raised_without_a_handler(tmp_path: Path) -> None:
    fs = Recording()
    fs.failing.add("remove_file")
    path = tmp_path / "file"
    with pytest.raises(OSError, match="remove_file failed"):
        write_file(fs, path, text("new"), replace=False)
    assert path.read_text(encoding="utf-8") == "new"


def test_a_leftover_after_publishing_goes_to_the_handler(tmp_path: Path) -> None:
    fs = Recording()
    fs.failing.add("remove_file")
    path = tmp_path / "file"
    heard: list[tuple[Path, str]] = []
    write_file(
        fs,
        path,
        text("new"),
        replace=False,
        leftover=lambda temp, error: heard.append((temp, str(error))),
    )
    assert path.read_text(encoding="utf-8") == "new"
    ((temp, error),) = heard
    assert temp.exists()
    assert error == "remove_file failed"


def test_a_leftover_after_a_failure_is_noted_on_the_failure(tmp_path: Path) -> None:
    fs = Recording()
    fs.failing.update({"replace_file", "remove_file"})
    with pytest.raises(OSError, match="replace_file failed") as raised:
        write_file(fs, tmp_path / "file", text("new"))
    (note,) = raised.value.__notes__
    assert note.startswith("cannot remove temporary file ")
    assert note.endswith(": remove_file failed")
