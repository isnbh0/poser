"""No-clobber studio publication with in-process rollback.

Each target is linked from a complete temp file, so it either does not exist or holds all of
its bytes. `.gitignore` is merged by compare-and-replace. A failure inside the process removes
only what this run wrote, identified by the inode and device of its temp files.
"""

import contextlib
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

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
    directories: list[Path] = field(default_factory=list)
    temps: list[Path] = field(default_factory=list)
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

    def temp(self, directory: Path, data: bytes, mode: int) -> tuple[Path, os.stat_result]:
        fd, name = tempfile.mkstemp(dir=directory, prefix=TEMP_PREFIX)
        path = Path(name)
        self.temps.append(path)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            return path, os.fstat(stream.fileno())

    def create(self, path: Path, data: bytes) -> None:
        temp, info = self.temp(path.parent, data, CREATED_MODE)
        # Record before linking, so no instant exists where this run's target is unrecorded.
        self.created.append((path, info.st_ino, info.st_dev))
        try:
            os.link(temp, path)
        except FileExistsError:
            raise
        except OSError as error:  # such as a filesystem without hard links
            raise ConfigError(f"{path}: cannot create: {error.strerror}") from error
        temp.unlink()
        self.temps.remove(temp)

    def replace_ignore(self, path: Path, data: bytes, expected: bytes) -> None:
        before = path.lstat()
        mode = stat.S_IMODE(before.st_mode)
        temp, info = self.temp(path.parent, data, mode)
        try:
            unchanged = _regular(path) and path.read_bytes() == expected
        except FileNotFoundError:
            unchanged = False
        if not unchanged:
            raise ConfigError(".gitignore changed during initialization; retry")
        self.original = _Original(
            expected,
            mode,
            before.st_atime_ns,
            before.st_mtime_ns,
            info.st_ino,
            info.st_dev,
            data,
        )
        temp.replace(path)
        self.temps.remove(temp)

    def rollback(self) -> None:
        for path, inode, device in reversed(self.created):
            with contextlib.suppress(OSError):
                info = path.lstat()
                if (info.st_ino, info.st_dev) == (inode, device):
                    path.unlink()
        if self.original is not None:
            with contextlib.suppress(OSError):
                self._restore_ignore(self.original)
        for temp in self.temps:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
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
        temp, _info = self.temp(path.parent, original.data, original.mode)
        os.utime(temp, ns=(original.atime, original.mtime))
        temp.replace(path)
        self.temps.remove(temp)


class WorkspaceInitializer:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _target(self, name: str) -> Path:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ConfigError(f"unsafe initialization target {name!r}")
        path = self.directory / relative
        for parent in path.parents:
            if parent == self.directory:
                break
            if parent.is_symlink():
                raise ConfigError(f"{parent}: symlink parent")
        return path

    def remove_leftovers(self, outputs: Mapping[str, bytes]) -> tuple[str, ...]:
        removed: list[str] = []
        changed: list[Path] = []
        parents = {self.directory}
        for name, data in outputs.items():
            if name == IGNORE:
                continue
            path = self._target(name)
            parents.add(path.parent)
            if not path.exists() and not path.is_symlink():
                continue
            if _leftover(path, data):
                path.unlink()
                removed.append(name)
            else:
                changed.append(path)
        for parent in sorted(parents):
            if not parent.is_dir() or parent.is_symlink():
                continue
            for temp in parent.glob(TEMP_PREFIX + "*"):
                if stat.S_ISREG(temp.lstat().st_mode):
                    temp.unlink()
        if changed:
            raise ConfigError(f"{changed[0]}: changed; not removed")
        return tuple(removed)

    def publish(self, outputs: Mapping[str, bytes], *, expected_ignore: bytes | None) -> None:
        paths = {name: self._target(name) for name in outputs}
        publication = _Publication(self.directory)
        try:
            publication.ensure_directory(self.directory)
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
    return WorkspaceInitializer(directory)
