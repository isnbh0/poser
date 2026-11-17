"""The file-system facility: the one owner of the platform-specific file-system operations that
adapters need, which are locking, durable sync, directory links, and no-clobber directory
publication.

`FileSystem` states what adapters need, not how a platform provides it, and each platform has one
backend that matches it by structure. `native()` selects the running platform's backend, so
importing this module loads no platform-specific module and succeeds on every platform.
`tests/test_filesystem.py` holds the contract every backend passes on its own platform.
"""

import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from poser.core.ports import UnsupportedPlatformError

PREREQUISITES_URL = "https://github.com/isnbh0/poser/blob/main/docs/setup.md#prerequisites"


class FileSystem(Protocol):
    """The platform-specific file-system operations adapters perform."""

    def lock(self, path: Path, *, wait: bool) -> AbstractContextManager[None]:
        """Hold an exclusive lock on `path`, creating the file if it is missing.

        While the context is open, every other `lock` on the same path is excluded, from this
        process or another. With `wait=True`, wait for as long as another holder keeps the lock.
        With `wait=False`, raise BlockingIOError at once instead of waiting. The lock ends when
        the context exits or when the holding process ends, however it ends. After a process
        dies, the platform may take a moment to release its locks. A lock file left on disk means
        nothing by itself.
        """
        ...

    def sync(self, path: Path) -> None:
        """Make a file's contents, or a directory's entries, durable.

        If the platform cannot flush a directory's entries, the directory case does nothing, and
        the backend's docstring says so.
        """
        ...

    def make_link(self, link: Path, target: str) -> None:
        """Create a directory link at `link` leading to `target`, a path relative to `link`'s
        directory.

        Raise FileExistsError if anything exists at `link`. Create nothing anywhere else, even
        when interrupted.
        """
        ...

    def remove_link(self, path: Path) -> None:
        """Remove the link at `path`, leaving what it leads to untouched. `poser.core.paths.is_link`
        tells whether a path is a link, on every platform."""
        ...

    def publish_directory(self, source: Path, destination: Path) -> None:
        """Rename the directory `source` to `destination` in one step, never replacing anything.

        Raise FileExistsError when `destination` exists.
        """
        ...


def native() -> FileSystem:
    """Return the running platform's backend, or raise UnsupportedPlatformError if it has none.

    Reads only `sys.platform` and imports the backend's module, so an adapter's factory may call
    it. A new backend adds its platform test here and its name to the message.
    """
    platform = sys.platform
    if platform == "darwin" or platform.startswith("linux"):
        from poser.adapters.filesystem_posix import PosixFileSystem

        return PosixFileSystem()
    if platform == "win32":
        from poser.adapters.filesystem_windows import WindowsFileSystem

        return WindowsFileSystem()
    raise UnsupportedPlatformError(
        f"poser's file-system support does not cover this platform ({platform}); it covers "
        f"macOS, Linux, and Windows. See {PREREQUISITES_URL}"
    )
