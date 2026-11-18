"""Private staging and no-clobber directory publication through the file-system facility."""

import errno
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

from poser.adapters.filesystem import FileSystem, native
from poser.core.names import is_inner_relative
from poser.core.ports import StagedDelivery


def _publish(fs: FileSystem, source: Path, destination: Path) -> None:
    if destination.parent.resolve() != destination.parent or source.resolve() != source:
        raise FileExistsError(errno.EEXIST, "publication parent changed", str(destination))
    fs.publish_directory(source, destination)


class _Stage:
    def __init__(self, fs: FileSystem, path: Path, destination: Path) -> None:
        self._fs = fs
        self.path = path
        self.destination = destination
        self.published = False

    def publish(self) -> None:
        if self.published:
            raise OSError("delivery stage was already published")
        _publish(self._fs, self.path, self.destination)
        self.published = True


class FileDeliveryWriter:
    """Construction performs no filesystem work."""

    def __init__(self, fs: FileSystem) -> None:
        self._fs = fs

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
                except OSError:
                    # Windows refuses to create a drive root as access denied, not as existing.
                    if not parent.is_dir():
                        raise
                else:
                    created.append(parent)
            path = Path(tempfile.mkdtemp(prefix=".poser-export-", dir=destination.parent))
            stage = _Stage(self._fs, path, destination)
            for relative, data in files.items():
                if not is_inner_relative(relative):
                    raise ValueError(f"unsafe bundle member: {relative}")
                target = path / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(data)
            yield stage
        finally:
            if stage is not None and not stage.published:
                self._fs.remove_tree(stage.path)
            if stage is None or not stage.published:
                for parent in reversed(created):
                    # Never remove intervening owner content.
                    with suppress(OSError):
                        parent.rmdir()


def create() -> FileDeliveryWriter:
    """Raises UnsupportedPlatformError on a platform the file-system facility has no backend
    for."""
    return FileDeliveryWriter(native())
