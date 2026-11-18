"""The file-system facility: the one owner of the platform-specific file-system operations that
adapters need, which are locking, durable sync, directory links, renaming and removing files and
directories, and no-clobber directory publication, and of `write_file`, the one way poser
replaces or creates a file in one step.

`FileSystem` states what adapters need, not how a platform provides it, and each platform has one
backend that matches it by structure. `native()` selects the running platform's backend, so
importing this module loads no platform-specific module and succeeds on every platform.
`tests/test_filesystem.py` holds the contract every backend passes on its own platform.
"""

import os
import stat
import sys
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from poser.core.paths import PathLimit, is_link
from poser.core.ports import PREREQUISITES_URL, ConfigError, UnsupportedPlatformError


class FileSystem(Protocol):
    """The platform-specific file-system operations adapters perform."""

    def path_limit(self) -> PathLimit:
        """How long a path this process may create. The first call may create and remove a
        temporary directory to find out."""
        ...

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

        Raise FileExistsError when `destination` exists. Raise PermissionError, saying another
        program may hold a file open, when the platform keeps refusing the rename for that
        reason.
        """
        ...

    def rename_directory(self, source: Path, destination: Path) -> None:
        """Rename the directory `source` to `destination` in one step.

        Raise FileExistsError when `destination` is a directory holding anything; an empty
        directory there may be replaced. Raise PermissionError, saying another program may hold
        a file open, when the platform keeps refusing the rename for that reason.
        """
        ...

    def replace_file(self, source: Path, destination: Path) -> None:
        """Rename the file `source` to `destination` in one step, replacing a file there, even
        one marked read-only.

        Raise PermissionError, saying another program may hold a file open, when the platform
        keeps refusing the replacement for that reason. When replacing fails, `destination` is
        as it was, read-only mark included.
        """
        ...

    def remove_file(self, path: Path) -> None:
        """Delete the file at `path`, even one marked read-only.

        Raise FileNotFoundError when nothing is at `path`, and PermissionError, saying another
        program may hold it open, when the platform keeps refusing for that reason.
        """
        ...

    def remove_tree(self, path: Path) -> None:
        """Delete the directory `path` and everything under it, read-only files included,
        removing each link inside without entering it.

        Raise FileNotFoundError when nothing is at `path`, and PermissionError, saying another
        program may hold a file open, when the platform keeps refusing for that reason.
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


LINK_PROBE_PREFIX = ".poser-link-probe-"
_LINK_SENTINEL = b"poser workspace link probe\n"


def _probe_directory_links(fs: FileSystem, probe: Path) -> None:
    target = probe / "target"
    target.mkdir()
    (target / "sentinel").write_bytes(_LINK_SENTINEL)
    original = target.stat()
    link = probe / "directory-link"
    fs.make_link(link, target.name)
    if not is_link(link) or not os.path.samestat(link.stat(), original):
        raise OSError("directory-link creation did not link the intended target")
    if (link / "sentinel").read_bytes() != _LINK_SENTINEL:
        raise OSError("the directory link did not expose the target's contents")
    fs.remove_link(link)
    if link.exists(follow_symlinks=False):
        raise OSError("directory-link removal left the link behind")
    if not os.path.samestat(target.stat(), original):
        raise OSError("directory-link removal changed the target")
    if (target / "sentinel").read_bytes() != _LINK_SENTINEL:
        raise OSError("directory-link removal changed the target's contents")


def _probe_hard_links(probe: Path) -> None:
    source = probe / "file"
    source.write_bytes(_LINK_SENTINEL)
    link = probe / "hard-link"
    os.link(source, link)
    first, second = source.lstat(), link.lstat()
    if not (
        stat.S_ISREG(first.st_mode)
        and stat.S_ISREG(second.st_mode)
        and os.path.samestat(first, second)
        and first.st_nlink >= 2
    ):
        raise OSError("hard-link creation did not produce two names for the same regular file")
    if link.read_bytes() != _LINK_SENTINEL:
        raise OSError("the hard link did not preserve the file's contents")


def _remove_link_probe(fs: FileSystem, probe: Path, owned: os.stat_result) -> None:
    try:
        current = probe.lstat()
    except FileNotFoundError:
        return
    if is_link(probe) or not stat.S_ISDIR(current.st_mode) or not os.path.samestat(current, owned):
        raise OSError("probe directory changed; refusing to remove its replacement")
    # This checks ownership at cleanup entry, not against hostile concurrent substitution.
    fs.remove_tree(probe)
    if probe.exists(follow_symlinks=False):
        raise OSError("probe cleanup returned without removing the directory")


def require_workspace_links(fs: FileSystem, directory: Path) -> None:
    """Verify working directory links and hard links on the destination volume before publishing.

    Use only an exclusively allocated private namespace, not similarly named owner entries.
    Cleanup precedes success. A forcibly killed process may leave the namespace for inspection;
    neither initialization nor its recognized-file recovery sweeps these directories.
    """
    try:
        probe = Path(tempfile.mkdtemp(prefix=LINK_PROBE_PREFIX, dir=directory))
    except OSError as error:
        raise ConfigError(f"{directory}: cannot check required links: {error}") from error
    try:
        owned = probe.lstat()
        changed = is_link(probe) or not stat.S_ISDIR(owned.st_mode)
    except OSError as error:
        raise ConfigError(
            f"{directory}: cannot inspect link probe {probe}: {error}; "
            "probe not removed; inspect it before manual cleanup"
        ) from error
    except BaseException as error:
        error.add_note(f"link probe {probe} was not removed; inspect it before manual cleanup")
        raise
    if changed:
        raise ConfigError(f"{directory}: link probe {probe} changed; refusing to use or remove it")

    failure: BaseException | None = None
    try:
        capability = "directory links"
        try:
            _probe_directory_links(fs, probe)
            capability = "hard links"
            _probe_hard_links(probe)
        except OSError as error:
            raise ConfigError(f"{directory}: cannot use required {capability}: {error}") from error
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            _remove_link_probe(fs, probe, owned)
        except OSError as cleanup:
            detail = f"cannot remove link probe {probe}: {cleanup}"
            if failure is not None and not isinstance(failure, Exception):
                failure.add_note(detail)
            else:
                message = (
                    f"{failure}; {detail}" if failure is not None else f"{directory}: {detail}"
                )
                raise ConfigError(message) from (failure if failure is not None else cleanup)


def umask_mode() -> int:
    """The permission bits a new file gets by default: read and write for everyone, less the
    process's umask."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def write_file(
    fs: FileSystem,
    path: Path,
    fill: Callable[[Path], object],
    *,
    replace: bool = True,
    mode: int | None = None,
    prefix: str | None = None,
    suffix: str = ".tmp",
    leftover: Callable[[Path, OSError], object] | None = None,
) -> None:
    """Publish at `path`, in one step, the file `fill` writes, so a reader finds the old file or
    the complete new one, never part of one.

    `fill` receives a fresh, empty temporary file in `path`'s directory, named `prefix` (by
    default `.<name>.`), a random part, and `suffix`, and writes the file there. Its exceptions
    propagate, and nothing is published. The file is then made durable, given its permission
    bits, and published.

    With `replace`, a file at `path` is replaced, even one marked read-only, and a symbolic link
    at `path` is kept and the file it leads to replaced. The file gets `mode`, or by default the
    permission bits of the file it replaces, or `umask_mode()` when there is none. Without
    `replace`, FileExistsError is raised when anything, a link included, is at `path`, and the
    file gets `mode` or `umask_mode()`; publishing links the temporary file at `path`, so a
    process killed between that link and removing the temporary file leaves both names.

    The temporary file is removed whether `write_file` returns or raises. Failing to remove it
    never changes the outcome: `leftover`, when given, receives its path and the error. Without
    `leftover`, that failure is raised after a successful link, with the file already published,
    and is added as a note to the exception of a failed write.
    """
    target = path.resolve() if replace else path
    if mode is None:
        try:
            mode = stat.S_IMODE(target.stat().st_mode) if replace else umask_mode()
        except FileNotFoundError:
            mode = umask_mode()
    descriptor, name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}." if prefix is None else prefix, suffix=suffix
    )
    os.close(descriptor)
    temp = Path(name)
    try:
        fill(temp)
        # Before the mode, which may make the file read-only: Windows syncs through a writable
        # descriptor.
        fs.sync(temp)
        temp.chmod(mode)
        if replace:
            fs.replace_file(temp, target)
            return
        os.link(temp, target)
    except BaseException as error:
        try:
            fs.remove_file(temp)
        except OSError as cleanup:
            if leftover is None:
                error.add_note(f"cannot remove temporary file {temp}: {cleanup}")
            else:
                leftover(temp, cleanup)
        raise
    try:
        fs.remove_file(temp)
    except OSError as cleanup:
        if leftover is None:
            raise
        leftover(temp, cleanup)


def write_text_file(fs: FileSystem, path: Path, text: str) -> None:
    """Replace or create the file at `path` with `text`, as UTF-8 with LF line endings, through
    `write_file`."""
    write_file(fs, path, lambda temp: temp.write_text(text, encoding="utf-8", newline="\n"))


class AtomicFileWriter:
    """`poser.core.ports.FileWriter` through `write_file`. Construction performs no file-system
    work."""

    def __init__(self, fs: FileSystem) -> None:
        self._fs = fs

    def write(
        self,
        path: Path,
        fill: Callable[[Path], object],
        *,
        replace: bool,
        suffix: str = ".tmp",
        leftover: Callable[[Path, OSError], object] | None = None,
    ) -> None:
        write_file(self._fs, path, fill, replace=replace, suffix=suffix, leftover=leftover)


def create_file_writer() -> AtomicFileWriter:
    """The file writer commands build through `poser.compose.file_writer`. Raises
    UnsupportedPlatformError on a platform the facility has no backend for."""
    return AtomicFileWriter(native())
