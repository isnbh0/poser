"""Private staging and native no-clobber directory publication.

No portable os.rename fallback: POSIX rename may replace an owner's empty directory.
"""

import ctypes
import errno
import os
import shutil
import sys
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

from poser.core.ports import StagedDelivery


def _publish(source: Path, destination: Path) -> None:
    if destination.parent.resolve() != destination.parent or source.resolve() != source:
        raise FileExistsError(errno.EEXIST, "publication parent changed", str(destination))
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
        raise OSError(errno.ENOTSUP, "no safe no-clobber directory publication on this platform")
    rename.restype = ctypes.c_int
    if rename(*args) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


class _Stage:
    def __init__(self, path: Path, destination: Path) -> None:
        self.path = path
        self.destination = destination
        self.published = False

    def publish(self) -> None:
        if self.published:
            raise OSError("delivery stage was already published")
        _publish(self.path, self.destination)
        self.published = True


class FileDeliveryWriter:
    """Construction performs no filesystem work."""

    @contextmanager
    def stage(self, destination: Path, files: Mapping[Path, bytes]) -> Generator[StagedDelivery]:
        if destination.parent.resolve() != destination.parent:
            raise FileExistsError(errno.EEXIST, "publication parent changed", str(destination))
        created: list[Path] = []
        stage: _Stage | None = None
        try:
            for parent in reversed(destination.parents):
                try:
                    parent.mkdir()
                except FileExistsError:
                    if not parent.is_dir():
                        raise
                else:
                    created.append(parent)
            path = Path(tempfile.mkdtemp(prefix=".poser-export-", dir=destination.parent))
            stage = _Stage(path, destination)
            for relative, data in files.items():
                if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                    raise ValueError(f"unsafe bundle member: {relative}")
                target = path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
            yield stage
        finally:
            if stage is not None and not stage.published:
                shutil.rmtree(stage.path)
            if stage is None or not stage.published:
                for parent in reversed(created):
                    # Never remove intervening owner content.
                    with suppress(OSError):
                        parent.rmdir()


def create() -> FileDeliveryWriter:
    return FileDeliveryWriter()
