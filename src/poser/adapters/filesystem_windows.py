"""The file-system facility's backend on Windows.

`WindowsFileSystem` matches `poser.adapters.filesystem.FileSystem` by structure; it does not
subclass it, because that module imports this one. `native()` imports this module only on Windows.
"""

import _winapi
import errno
import msvcrt
import os
import stat
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

assert sys.platform == "win32"  # noqa: S101 - narrows the platform for type checkers.

LOCK_POLL_S = 0.05
LOCK_BUSY = frozenset({errno.EACCES, errno.EDEADLOCK})


class WindowsFileSystem:
    """Byte-range locks through `msvcrt`, `fsync` through writable descriptors, directory
    junctions, and `os.rename`.

    Where Windows differs from the protocol's POSIX counterpart:

    - Syncing a directory does nothing, because Windows documents no way to flush a directory's
      entries.
    - Waiting for a lock polls, because the C runtime's blocking lock gives up after ten tries.
    - Links are directory junctions, which need no privilege but hold absolute targets. After a
      studio moves or is copied, each song's `latest` leads to the old location until that song
      renders again. Until then, `FileRenderStore.protected_roots` refuses the junction as a link
      that escapes the store, so `export-song` refuses every song in the studio.
    - A process killed while creating a junction can leave an empty directory at the link's path.
    """

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
            if link.is_dir() and not self.is_link(link):
                with suppress(OSError):
                    link.rmdir()
            raise

    def is_link(self, path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

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
        `destination` exists raises FileExistsError.
        """
        try:
            source.rename(destination)
        except OSError as error:
            if destination.exists(follow_symlinks=False):
                raise FileExistsError(
                    errno.EEXIST, os.strerror(errno.EEXIST), str(destination)
                ) from error
            raise
