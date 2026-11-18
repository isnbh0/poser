"""The file-system facility's backend on macOS and Linux.

`PosixFileSystem` matches `poser.adapters.filesystem.FileSystem` by structure; it does not
subclass it, because that module imports this one.
"""

import ctypes
import errno
import fcntl
import os
import shutil
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from poser.core.paths import PathLimit, PathUnit


class PosixFileSystem:
    """Advisory `flock` locks, `fsync` through read-only descriptors, symbolic links, the C
    library's no-clobber rename, and the standard library's renames and removals.

    No program holding a file open stops renaming or removing it, and a file's read-only mode
    does not stop replacing or removing it, so no operation here waits or retries.
    """

    def path_limit(self) -> PathLimit:
        """`PATH_MAX` less its terminating null, in bytes: 1023 on macOS and 4095 on Linux."""
        longest = os.pathconf("/", "PC_PATH_MAX") - 1
        return PathLimit(longest, longest, PathUnit.BYTES)

    @contextmanager
    def lock(self, path: Path, *, wait: bool) -> Generator[None]:
        """Hold an advisory `flock` on a descriptor of `path`, creating the file.

        The system releases the lock when the descriptor closes, which it does when the context
        exits or the process ends, however it ends.
        """
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def sync(self, path: Path) -> None:
        """`fsync` a read-only descriptor of `path`, which flushes a directory's entries as well
        as a file's contents."""
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def make_link(self, link: Path, target: str) -> None:
        """Create a symbolic link, which raises FileExistsError if anything is at `link`."""
        link.symlink_to(target, target_is_directory=True)

    def remove_link(self, path: Path) -> None:
        path.unlink()

    def publish_directory(self, source: Path, destination: Path) -> None:
        """Rename with `renamex_np(RENAME_EXCL)` on macOS or `renameat2(RENAME_NOREPLACE)` on
        Linux.

        No portable `os.rename` fallback: POSIX rename may replace an owner's empty directory. A
        Linux C library without `renameat2` raises OSError(ENOTSUP).
        """
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rename = library.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            args = (os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
        elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
            rename = library.renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            args = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)  # NOREPLACE
        else:
            raise OSError(
                errno.ENOTSUP, "no safe no-clobber directory publication on this platform"
            )
        rename.restype = ctypes.c_int
        if rename(*args) != 0:
            code = ctypes.get_errno()
            # OSError with EEXIST constructs a FileExistsError.
            raise OSError(code, os.strerror(code), str(destination))

    def rename_directory(self, source: Path, destination: Path) -> None:
        """`os.rename`, which replaces an empty directory at `destination`."""
        try:
            source.rename(destination)
        except OSError as error:
            if error.errno in (errno.EEXIST, errno.ENOTEMPTY):
                raise FileExistsError(
                    errno.EEXIST, os.strerror(errno.EEXIST), str(destination)
                ) from error
            raise

    def replace_file(self, source: Path, destination: Path) -> None:
        source.replace(destination)

    def remove_file(self, path: Path) -> None:
        path.unlink()

    def remove_tree(self, path: Path) -> None:
        shutil.rmtree(path)
