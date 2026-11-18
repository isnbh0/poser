"""No-clobber studio publication with in-process rollback.

Each target is linked from a complete temp file, so it either does not exist or holds all of
its bytes. `.gitignore` is merged by compare-and-replace. A failure inside the process removes
only what this run wrote, identified by the inode and device of its temp files. Files are
written, replaced, and removed through the file-system facility.
"""

import contextlib
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from poser.adapters.filesystem import FileSystem, native, require_workspace_links, write_file
from poser.core.names import is_inner_relative
from poser.core.paths import is_link
from poser.core.ports import ConfigError

TEMP_PREFIX = ".poser-tmp-"
IGNORE = ".gitignore"
CREATED_MODE = 0o644


def _regular(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _leftover(path: Path, data: bytes) -> bool:
    """Whether `path` holds exactly `data`, linked only from itself and this run's temp files.

    A run killed between linking a target and unlinking its temp file leaves the target with a
    second link, the `.poser-tmp-*` file it came from.
    """
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return False
    twins = sum(
        1
        for temp in path.parent.glob(TEMP_PREFIX + "*")
        if temp != path and os.path.samestat(temp.lstat(), info)
    )
    return info.st_nlink == 1 + twins and path.read_bytes() == data


@dataclass(frozen=True)
class _Original:
    """The `.gitignore` this run replaced, and the inode of the replacement."""

    data: bytes
    mode: int
    atime: int
    mtime: int
    inode: int
    device: int
    written: bytes


@dataclass
class _Publication:
    """What one `publish` call has created, so a failure can remove exactly that."""

    directory: Path
    fs: FileSystem
    directories: list[Path] = field(default_factory=list)
    created: list[tuple[Path, int, int]] = field(default_factory=list)
    original: _Original | None = None

    def ensure_directory(self, path: Path) -> None:
        missing: list[Path] = []
        while not path.exists():
            missing.append(path)
            path = path.parent
        for item in reversed(missing):
            try:
                item.mkdir()
            except FileExistsError:
                continue
            self.directories.append(item)

    def create(self, path: Path, data: bytes) -> None:
        def fill(temp: Path) -> None:
            temp.write_bytes(data)
            info = temp.stat()
            # Record before linking, so no instant exists where this run's target is unrecorded.
            self.created.append((path, info.st_ino, info.st_dev))

        try:
            write_file(self.fs, path, fill, replace=False, mode=CREATED_MODE, prefix=TEMP_PREFIX)
        except FileExistsError:
            raise
        except OSError as error:  # such as a filesystem without hard links
            raise ConfigError(f"{path}: cannot create: {error.strerror}") from error

    def replace_ignore(self, path: Path, data: bytes, expected: bytes) -> None:
        before = path.lstat()
        mode = stat.S_IMODE(before.st_mode)

        def fill(temp: Path) -> None:
            temp.write_bytes(data)
            try:
                unchanged = _regular(path) and path.read_bytes() == expected
            except FileNotFoundError:
                unchanged = False
            if not unchanged:
                raise ConfigError(".gitignore changed during initialization; retry")
            info = temp.stat()
            self.original = _Original(
                expected,
                mode,
                before.st_atime_ns,
                before.st_mtime_ns,
                info.st_ino,
                info.st_dev,
                data,
            )

        write_file(self.fs, path, fill, mode=mode, prefix=TEMP_PREFIX)

    def rollback(self) -> None:
        for path, inode, device in reversed(self.created):
            with contextlib.suppress(OSError):
                info = path.lstat()
                if (info.st_ino, info.st_dev) == (inode, device):
                    self.fs.remove_file(path)
        if self.original is not None:
            with contextlib.suppress(OSError):
                self._restore_ignore(self.original)
        for directory in reversed(self.directories):
            with contextlib.suppress(OSError):
                directory.rmdir()

    def _restore_ignore(self, original: _Original) -> None:
        path = self.directory / IGNORE
        info = path.lstat()
        if (info.st_ino, info.st_dev) != (original.inode, original.device):
            return
        if path.read_bytes() != original.written:
            return

        def fill(temp: Path) -> None:
            temp.write_bytes(original.data)
            os.utime(temp, ns=(original.atime, original.mtime))

        write_file(self.fs, path, fill, mode=original.mode, prefix=TEMP_PREFIX)


class WorkspaceInitializer:
    def __init__(self, directory: Path, fs: FileSystem) -> None:
        self.directory = directory
        self.fs = fs

    def _target(self, name: str) -> Path:
        relative = Path(name)
        if not is_inner_relative(relative):
            raise ConfigError(f"unsafe initialization target {name!r}")
        path = self.directory / relative
        for parent in path.parents:
            if parent == self.directory:
                break
            if is_link(parent):
                raise ConfigError(f"{parent}: linked parent")
        return path

    def _temps(self, names: Iterable[str]) -> list[Path]:
        parents = {self.directory} | {self._target(name).parent for name in names}
        return [
            temp
            for parent in sorted(parents)
            if parent.is_dir() and not is_link(parent)
            for temp in sorted(parent.glob(TEMP_PREFIX + "*"))
            if stat.S_ISREG(temp.lstat().st_mode)
        ]

    def leftovers(self, outputs: Mapping[str, bytes]) -> tuple[str, ...]:
        found: list[str] = []
        for name, data in outputs.items():
            if name == IGNORE:
                continue
            path = self._target(name)
            if path.exists(follow_symlinks=False):
                if not _leftover(path, data):
                    raise ConfigError(f"{path} exists")
                found.append(name)
        temps = self._temps(outputs)
        return (*found, *(str(temp.relative_to(self.directory)) for temp in temps))

    def remove_leftovers(self, outputs: Mapping[str, bytes]) -> tuple[str, ...]:
        removed: list[str] = []
        changed: list[Path] = []
        for name, data in outputs.items():
            if name == IGNORE:
                continue
            path = self._target(name)
            if not path.exists(follow_symlinks=False):
                continue
            if _leftover(path, data):
                self.fs.remove_file(path)
                removed.append(name)
            else:
                changed.append(path)
        for temp in self._temps(outputs):
            self.fs.remove_file(temp)
        if changed:
            raise ConfigError(f"{changed[0]}: changed; not removed")
        return tuple(removed)

    def publish(self, outputs: Mapping[str, bytes], *, expected_ignore: bytes | None) -> None:
        paths = {name: self._target(name) for name in outputs}
        publication = _Publication(self.directory, self.fs)
        try:
            publication.ensure_directory(self.directory)
            require_workspace_links(self.fs, self.directory)
            for name, data in outputs.items():
                if name == IGNORE:
                    continue
                publication.ensure_directory(paths[name].parent)
                publication.create(paths[name], data)
            if IGNORE in outputs:
                if expected_ignore is None:
                    publication.create(paths[IGNORE], outputs[IGNORE])
                else:
                    publication.replace_ignore(paths[IGNORE], outputs[IGNORE], expected_ignore)
        except BaseException:
            publication.rollback()
            raise


def create(directory: Path) -> WorkspaceInitializer:
    """Raises UnsupportedPlatformError on a platform the file-system facility has no backend
    for."""
    return WorkspaceInitializer(directory, native())
