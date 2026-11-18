"""The Windows backend's own behavior, beyond the portable contract in `test_filesystem.py`.

`CreateJunction` cannot be made to fail on demand, so these tests stand in for it.
"""

import errno
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from poser.adapters.filesystem import native

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the backend runs on Windows")

CREATE_JUNCTION = "poser.adapters.filesystem_windows._winapi.CreateJunction"
# Shorter than the backend's patience for a held file, and longer than its first pauses.
BRIEFLY_S = 0.4
# Longer than the backend's patience for a held file.
TOO_LONG_S = 4.0


def half_made_junction(error: OSError) -> Callable[[str, str], NoReturn]:
    """Fail as `CreateJunction` does after it created the directory."""

    def create(target: str, link: str) -> NoReturn:
        Path(link).mkdir()
        raise error

    return create


def never_called(target: str, link: str) -> NoReturn:
    raise AssertionError(f"CreateJunction ran for {link}")


def test_a_junction_that_fails_half_made_leaves_nothing_at_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "history" / "1").mkdir(parents=True)
    error = OSError(errno.EIO, "the volume holds no reparse points")
    monkeypatch.setattr(CREATE_JUNCTION, half_made_junction(error))
    with pytest.raises(OSError, match="reparse points") as raised:
        native().make_link(tmp_path / "latest", "history/1")
    assert not isinstance(raised.value, FileExistsError)
    assert not (tmp_path / "latest").exists()


def test_an_existing_directory_at_the_link_is_refused_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "latest").mkdir()
    monkeypatch.setattr(CREATE_JUNCTION, never_called)
    with pytest.raises(FileExistsError):
        native().make_link(tmp_path / "latest", "history/1")
    assert (tmp_path / "latest").is_dir()


# Files another program holds open


def hold(path: Path, seconds: float) -> threading.Thread:
    """Hold `path` open for `seconds` in another thread, as a media player or virus scanner does.

    Python opens files without delete sharing, so while it is held Windows refuses to rename,
    replace, or delete it, or to rename a directory above it. Returns once the file is open.
    """
    opened = threading.Event()

    def run() -> None:
        with path.open("rb"):
            opened.set()
            time.sleep(seconds)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert opened.wait(5)
    return thread


def refused(attempt: Callable[[], object]) -> bool:
    """Whether `attempt` fails as Windows fails on a held file."""
    try:
        attempt()
    except PermissionError:
        return True
    return False


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_replacing_a_briefly_held_file_waits_for_it(tmp_path: Path) -> None:
    source = write(tmp_path / "new", "new")
    destination = write(tmp_path / "old", "old")
    holder = hold(destination, BRIEFLY_S)
    assert refused(lambda: source.replace(destination))
    native().replace_file(source, destination)
    assert destination.read_text(encoding="utf-8") == "new"
    holder.join()


def test_replacing_a_file_held_too_long_says_what_may_hold_it(tmp_path: Path) -> None:
    source = write(tmp_path / "new", "new")
    destination = write(tmp_path / "old", "old")
    destination.chmod(0o444)
    holder = hold(destination, TOO_LONG_S)
    with pytest.raises(PermissionError, match="may hold it open"):
        native().replace_file(source, destination)
    holder.join()
    assert destination.read_text(encoding="utf-8") == "old"
    assert sys.platform == "win32"
    assert destination.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
    assert source.exists()


def test_removing_a_briefly_held_read_only_file_waits_for_it(tmp_path: Path) -> None:
    path = write(tmp_path / "file", "data")
    path.chmod(0o444)
    holder = hold(path, BRIEFLY_S)
    assert refused(path.unlink)
    native().remove_file(path)
    assert not path.exists()
    holder.join()


def test_removing_a_tree_with_a_briefly_held_file_waits_for_it(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "inner").mkdir(parents=True)
    held = write(tree / "inner" / "take.wav", "audio")
    held.chmod(0o444)
    holder = hold(held, BRIEFLY_S)
    native().remove_tree(tree)
    assert not tree.exists()
    holder.join()


def test_renaming_a_directory_holding_a_briefly_held_file_waits_for_it(tmp_path: Path) -> None:
    source = tmp_path / "draft"
    source.mkdir()
    held = write(source / "take.wav", "audio")
    destination = tmp_path / "published"
    holder = hold(held, BRIEFLY_S)
    assert refused(lambda: source.rename(destination))
    native().rename_directory(source, destination)
    assert (destination / "take.wav").read_text(encoding="utf-8") == "audio"
    holder.join()
