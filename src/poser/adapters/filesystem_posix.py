"""The file-system facility's backend on macOS and Linux.

`PosixFileSystem` matches `poser.adapters.filesystem.FileSystem` by structure; it does not
subclass it, because that module imports this one.
"""

import ctypes
import errno
import fcntl
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class PosixFileSystem:
    """Advisory `flock` locks, `fsync` through read-only descriptors, symbolic links, and the C
    library's no-clobber rename."""

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
