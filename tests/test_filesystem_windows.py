"""The Windows backend's own behavior, beyond the portable contract in `test_filesystem.py`.

`CreateJunction` cannot be made to fail on demand, so these tests stand in for it.
"""

import errno
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from poser.adapters.filesystem import native

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the backend runs on Windows")

CREATE_JUNCTION = "poser.adapters.filesystem_windows._winapi.CreateJunction"


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
