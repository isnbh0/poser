"""The scratch port on the file-system facility: directories that clean up without ever changing
the outcome of the work inside them."""

import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from poser.adapters.filesystem import FileScratch, FileSystem, create_scratch, native
from poser.core.ports import Scratch


class RefusingRemoval:
    """The native file system, except that removing a tree is refused as a held file is."""

    def __init__(self) -> None:
        self.removals: list[Path] = []

    def __getattr__(self, name: str) -> object:
        return getattr(native(), name)

    def remove_tree(self, path: Path) -> None:
        self.removals.append(path)
        raise PermissionError(13, "held by another program", str(path))


def refusing() -> tuple[FileScratch, RefusingRemoval]:
    fs = RefusingRemoval()
    return FileScratch(fs), fs  # ty: ignore[invalid-argument-type]


def test_the_facility_scratch_satisfies_the_port() -> None:
    assert isinstance(create_scratch(), Scratch)


def test_a_directory_is_new_empty_named_by_prefix_and_removed_on_exit(tmp_path: Path) -> None:
    with create_scratch().directory(parent=tmp_path, prefix=".job-") as directory:
        assert directory.parent == tmp_path
        assert directory.name.startswith(".job-")
        assert list(directory.iterdir()) == []
        (directory / "inside.txt").write_text("x", encoding="utf-8", newline="\n")

    assert not directory.exists()


def test_without_a_parent_the_directory_is_in_the_system_temp_folder() -> None:
    with create_scratch().directory() as directory:
        assert directory.parent == Path(tempfile.gettempdir())
        assert directory.name.startswith("poser-")


def test_a_failed_removal_leaves_the_directory_and_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scratch, _ = refusing()

    with scratch.directory(parent=tmp_path) as directory:
        pass

    assert directory.is_dir()
    assert str(directory) in capsys.readouterr().err


def test_a_failed_removal_never_replaces_the_body_s_exception(tmp_path: Path) -> None:
    scratch, _ = refusing()
    error = RuntimeError("the work failed")

    with pytest.raises(RuntimeError) as raised, scratch.directory(parent=tmp_path):
        raise error

    assert raised.value is error


def test_an_impatient_scratch_never_waits_through_the_facility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fs = RefusingRemoval()
    scratch = FileScratch(fs, patient=False)  # ty: ignore[invalid-argument-type]
    removed: list[Path] = []
    real_rmtree = shutil.rmtree

    def one_attempt(path: Path) -> None:
        removed.append(Path(path))
        real_rmtree(path)

    monkeypatch.setattr(shutil, "rmtree", one_attempt)
    with scratch.directory(parent=tmp_path) as directory:
        pass

    assert fs.removals == []
    assert removed == [directory]
    assert not directory.exists()
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows refuses to delete a held file")
def test_a_file_held_briefly_past_the_body_is_still_removed(tmp_path: Path) -> None:
    fs: FileSystem = native()
    opened = threading.Event()

    with FileScratch(fs).directory(parent=tmp_path) as directory:
        held = directory / "held.bin"
        held.write_bytes(b"x")

        def hold() -> None:
            with held.open("rb"):
                opened.set()
                time.sleep(0.3)

        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        assert opened.wait(5)

    thread.join()
    assert not directory.exists()
