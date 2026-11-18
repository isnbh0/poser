"""The file-system facility's backend on Windows.

`WindowsFileSystem` matches `poser.adapters.filesystem.FileSystem` by structure; it does not
subclass it, because that module imports this one. `native()` imports this module only on Windows.
"""

import _winapi
import errno
import msvcrt
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from functools import cache
from pathlib import Path

from poser.core.paths import PathLimit, is_link
from poser.core.ports import PREREQUISITES_URL

assert sys.platform == "win32"  # noqa: S101 - narrows the platform for type checkers.

LOCK_POLL_S = 0.05
LOCK_BUSY = frozenset({errno.EACCES, errno.EDEADLOCK})
# How Windows refuses to rename or delete a file another program holds open: access denied, a
# sharing violation, or a lock violation.
HELD = frozenset({5, 32, 33})
# Deleting a file another program holds open with delete sharing leaves it until that program
# closes it, so removing its directory fails as not empty until then.
DIRECTORY_NOT_EMPTY = 145
# The pauses between attempts while a file is held, about 1.6 seconds in all: long enough for a
# virus scanner or the search indexer to let go of a file poser just wrote.
HELD_PAUSES_S = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.8)
HELD_ADVICE = (
    "another program, such as a media player, Explorer's preview pane, or a virus scanner, may "
    "hold it open; close it and try again"
)
# A path holds MAX_PATH (260) characters with its terminating null, and a directory's leaves room
# for an 8.3 file name, unless long paths are on; then the limit is 32,767 characters:
# https://learn.microsoft.com/windows/win32/fileio/maximum-file-path-limitation
MAX_PATH = 260
SHORT_PATHS = PathLimit(
    directory=MAX_PATH - 12 - 1,
    file=MAX_PATH - 1,
    lift=f"turn on Windows long paths as {PREREQUISITES_URL} shows",
)
LONG_PATHS = PathLimit(32_767 - 1, 32_767 - 1)


@cache
def _long_paths() -> bool:
    """Whether this process may create a directory whose path passes MAX_PATH.

    Windows lifts the limit for a program only when the system turns long paths on and the
    program's manifest opts in, as Python's does since 3.6, and reads the switch when a process
    starts. No documented function says whether both hold, so this tries.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="poser-paths-", ignore_cleanup_errors=True) as root:
            Path(root, *["d" * 100] * 3).mkdir(parents=True)
    except OSError:
        return False
    return True


def _retrying[T](attempt: Callable[[], T], path: Path, codes: frozenset[int] = HELD) -> T:
    """Return what `attempt` returns, trying again after each pause in `HELD_PAUSES_S` while it
    fails with a Windows error in `codes`. When the last attempt fails that way, raise
    PermissionError naming `path` and the likely cause."""
    for pause in (*HELD_PAUSES_S, None):
        try:
            return attempt()
        except OSError as error:
            if error.winerror not in codes:
                raise
            if pause is None:
                raise PermissionError(
                    errno.EACCES, f"{error.strerror}; {HELD_ADVICE}", str(path)
                ) from error
        time.sleep(pause)
    raise AssertionError  # The last pause is None, which returns or raises above.


def _clear_read_only(path: Path) -> bool:
    """Clear the read-only attribute of the file at `path`, without following a link, and return
    whether it was set. False when nothing is at `path`."""
    try:
        attributes = path.lstat().st_file_attributes
    except FileNotFoundError:
        return False
    if not attributes & stat.FILE_ATTRIBUTE_READONLY:
        return False
    path.chmod(stat.S_IREAD | stat.S_IWRITE, follow_symlinks=False)
    return True


def _set_read_only(path: Path) -> None:
    path.chmod(stat.S_IREAD, follow_symlinks=False)


class WindowsFileSystem:
    """Byte-range locks through `msvcrt`, `fsync` through writable descriptors, directory
    junctions, and `os.rename`, `os.replace`, and deletions that clear the read-only attribute
    and wait out programs briefly holding a file.

    Where Windows differs from the protocol's POSIX counterpart:

    - Syncing a directory does nothing, because Windows documents no way to flush a directory's
      entries.
    - Waiting for a lock polls, because the C runtime's blocking lock gives up after ten tries.
    - Links are directory junctions, which need no privilege but hold absolute targets. After a
      studio moves, or is copied by a tool that keeps junctions, each song's `latest` leads to
      the old location until that song renders or analyzes again, and
      `FileRenderStore.protected_roots` protects the old location as one the store reaches.
    - A process killed while creating a junction can leave an empty directory at the link's path.
    - A program holding a file open, without allowing deletion, stops renaming or deleting that
      file or a directory above it. Renames, replacements, and removals try again for about 1.6
      seconds (`HELD_PAUSES_S`), then raise PermissionError saying what may hold the file.
    - The read-only attribute stops replacing or deleting a file, so replacing and removing
      clear it first, and replacing sets it again on the destination when the replacement fails.
    - Renaming a directory never replaces one, even an empty one.
    - Unless long paths are on, a file's path holds 259 characters and a directory's 247.
    """

    def path_limit(self) -> PathLimit:
        return LONG_PATHS if _long_paths() else SHORT_PATHS

    @contextmanager
    def lock(self, path: Path, *, wait: bool) -> Generator[None]:
        """Hold a lock on the first byte of `path`, creating the file.

        The system releases the lock when the descriptor closes, which it does when the context
        exits or the process ends, however it ends. Descriptors are not inherited (PEP 446), so
        worker processes never hold the lock.
        """
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o644)
        try:
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in LOCK_BUSY:
                        raise
                    if not wait:
                        raise BlockingIOError(errno.EAGAIN, "locked", str(path)) from error
                time.sleep(LOCK_POLL_S)
            try:
                yield
            finally:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

    def sync(self, path: Path) -> None:
        """`fsync` a file through a writable descriptor, because `FlushFileBuffers` needs write
        access. A directory does nothing."""
        if path.is_dir():
            return
        fd = os.open(path, os.O_RDWR | os.O_BINARY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def make_link(self, link: Path, target: str) -> None:
        """Create a directory junction holding `target` resolved to an absolute path. Raises
        FileExistsError if anything is at `link`.

        `CreateJunction` creates an empty directory and then makes it a junction, and leaves the
        directory behind when that second step fails, as on a volume without reparse points. A
        failure other than FileExistsError therefore removes an empty directory at `link`, which
        the check before the call shows was not there before.
        """
        if link.exists(follow_symlinks=False):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(link))
        try:
            _winapi.CreateJunction(str((link.parent / target).resolve()), str(link))
        except FileExistsError:
            raise
        except OSError:
            if link.is_dir() and not is_link(link):
                with suppress(OSError):
                    link.rmdir()
            raise

    def remove_link(self, path: Path) -> None:
        """Remove a junction or a directory symbolic link with `rmdir`, which Windows documents
        as leaving the target alone, and any other link with `unlink`."""
        if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
            path.rmdir()
        else:
            path.unlink()

    def publish_directory(self, source: Path, destination: Path) -> None:
        """Rename with `os.rename`, which never replaces anything on Windows.

        Windows may report an existing destination as access denied, so any failure while
        `destination` exists raises FileExistsError at once.
        """

        def attempt() -> None:
            try:
                source.rename(destination)
            except OSError as error:
                if destination.exists(follow_symlinks=False):
                    raise FileExistsError(
                        errno.EEXIST, os.strerror(errno.EEXIST), str(destination)
                    ) from error
                raise

        _retrying(attempt, source)

    def rename_directory(self, source: Path, destination: Path) -> None:
        """`publish_directory`, because renaming on Windows never replaces a directory."""
        self.publish_directory(source, destination)

    def replace_file(self, source: Path, destination: Path) -> None:
        """`os.replace`, after clearing the destination's read-only attribute."""
        cleared = _clear_read_only(destination)
        try:
            _retrying(lambda: source.replace(destination), destination)
        except BaseException:
            if cleared:
                with suppress(OSError):
                    _set_read_only(destination)
            raise

    def remove_file(self, path: Path) -> None:
        """`unlink`, after clearing the file's read-only attribute."""
        cleared = _clear_read_only(path)
        try:
            _retrying(path.unlink, path)
        except BaseException:
            if cleared:
                with suppress(OSError):
                    _set_read_only(path)
            raise

    def remove_tree(self, path: Path) -> None:
        """`shutil.rmtree`, which removes a junction or link inside without entering it, clearing
        each refused file's read-only attribute and trying each refused deletion again."""

        def again(function: Callable[[str], object], name: str, error: BaseException) -> None:
            if function not in (os.unlink, os.remove, os.rmdir) or not isinstance(error, OSError):
                raise error
            entry = Path(name)
            if function is not os.rmdir:
                _clear_read_only(entry)
            _retrying(lambda: function(name), entry, HELD | {DIRECTORY_NOT_EMPTY})

        shutil.rmtree(path, onexc=again)
