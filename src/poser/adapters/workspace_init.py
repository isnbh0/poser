"""Recoverable studio publication. The namespace and lock inode are permanent."""

import base64
import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict, cast

from poser.core.ports import ConfigError

IDENTITY = b"poser initialization namespace v1\n"


class Entry(TypedDict):
    target: str
    data: str
    digest: str
    original: str | None
    mode: int
    atime: int
    mtime: int
    intent: bool
    published_mode: int
    published_mtime: int
    inode: int
    device: int


class Directory(TypedDict):
    target: str
    inode: int
    device: int


class Journal(TypedDict):
    version: int
    committed: bool
    entries: list[Entry]
    directories: list[Directory]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def _atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    temporary = path.with_name(path.name + ".tmp")
    # The temporary name is private to the locked, recognized namespace.
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    temporary.replace(path)


class WorkspaceInitializer:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.namespace = directory / ".poser-init"
        self.journal = self.namespace / "journal.json"
        self._active = False

    @contextmanager
    def session(self) -> Generator[None]:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            self.namespace.mkdir()
        except FileExistsError:
            pass
        else:
            # Death before identity creation leaves an unrecognized namespace: safe refusal.
            with (self.namespace / "identity").open("xb") as stream:
                stream.write(IDENTITY)
        identity = self.namespace / "identity"
        if (
            self.namespace.is_symlink()
            or not identity.is_file()
            or not _regular(identity)
            or identity.read_bytes() != IDENTITY
        ):
            raise ConfigError(
                f"{self.namespace}: unrecognized initialization namespace; inspect it"
            )
        lock = self.namespace / "lock"
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise ConfigError(f"{lock}: unsafe initialization lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ConfigError(f"{lock}: initialization is busy; retry later") from error
            if self.journal.exists():
                state = self._load()
                if state["committed"]:
                    self._cleanup()
                    raise ConfigError(f"{self.directory}: studio already exists (committed)")
                self._rollback(state)
            self._active = True
            yield
        finally:
            self._active = False
            os.close(fd)

    def _load(self) -> Journal:
        try:
            state = cast("Journal", json.loads(self.journal.read_bytes()))
            self._validate(state)
        except (ValueError, KeyError, TypeError) as error:
            raise ConfigError(
                f"{self.journal}: invalid journal; inspect without deleting outputs"
            ) from error

        return state

    def _validate(self, state: Journal) -> None:
        if state["version"] != 1 or not isinstance(state["committed"], bool):
            raise ValueError("invalid state")
        for entry in state["entries"]:
            self._target(entry["target"])
            if not isinstance(entry["published_mode"], int) or not isinstance(
                entry["published_mtime"], int
            ):
                raise TypeError("invalid published metadata")
            if _digest(base64.b64decode(entry["data"])) != entry["digest"]:
                raise ValueError("invalid staged digest")
        for directory in state["directories"]:
            self._target(directory["target"])

    def _target(self, name: str) -> Path:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ConfigError(f"{self.journal}: unsafe target {name!r}")
        if relative.parts[0] == ".poser-init":
            raise ConfigError(f"{self.journal}: reserved target {name!r}")
        path = self.directory / relative
        for parent in path.parents:
            if parent == self.directory.parent:
                break
            if parent.is_symlink():
                raise ConfigError(f"{parent}: symlink parent")
        return path

    def _save(self, state: Journal) -> None:
        _atomic(self.journal, json.dumps(state).encode())

    def _cleanup(self) -> None:
        for name in ("stage", "restore", "restore.tmp", "journal.json.tmp"):
            (self.namespace / name).unlink(missing_ok=True)
        self.journal.unlink(missing_ok=True)

    def publish(
        self, outputs: Mapping[str, bytes], *, expected_ignore: bytes | None = None
    ) -> None:
        if not self._active:
            raise ConfigError("initialization publication requires an active locked session")
        entries: list[Entry] = []
        directories: list[Directory] = []
        for name, data in outputs.items():
            path = self._target(name)
            original = None
            mode, atime, mtime = 0o644, 0, 0
            if path.exists() or path.is_symlink():
                if name != ".gitignore" or not _regular(path):
                    raise ConfigError(f"{path}: exists or is unsafe")
                info = path.stat()
                original = base64.b64encode(path.read_bytes()).decode()
                mode, atime, mtime = stat.S_IMODE(info.st_mode), info.st_atime_ns, info.st_mtime_ns
            if name == ".gitignore":
                observed = base64.b64decode(original) if original is not None else None
                if observed != expected_ignore:
                    raise ConfigError(f"{path}: owner changed .gitignore during preparation")
            for parent in reversed(path.parents):
                if parent.is_relative_to(self.directory) and not parent.exists():
                    relative = str(parent.relative_to(self.directory))
                    if not any(item["target"] == relative for item in directories):
                        directories.append(Directory(target=relative, inode=0, device=0))
            entries.append(
                Entry(
                    target=name,
                    data=base64.b64encode(data).decode(),
                    digest=_digest(data),
                    original=original,
                    mode=mode,
                    atime=atime,
                    mtime=mtime,
                    intent=False,
                    published_mode=0,
                    published_mtime=0,
                    inode=0,
                    device=0,
                )
            )
        state = Journal(version=1, committed=False, entries=entries, directories=directories)
        self._save(state)
        try:
            for directory in directories:
                path = self._target(directory["target"])
                path.mkdir()
                info = path.stat()
                directory["inode"], directory["device"] = info.st_ino, info.st_dev
                self._save(state)
            for entry in entries:
                path = self._target(entry["target"])
                stage = self.namespace / "stage"
                with stage.open("xb") as stream:
                    stream.write(base64.b64decode(entry["data"]))
                    stream.flush()
                    os.fsync(stream.fileno())
                stage.chmod(entry["mode"])
                info = stage.stat()
                entry["inode"], entry["device"] = info.st_ino, info.st_dev
                # Linking/renaming preserves these; reads may change atime, so exclude it.
                entry["published_mode"] = stat.S_IMODE(info.st_mode)
                entry["published_mtime"] = info.st_mtime_ns
                entry["intent"] = True
                self._save(state)
                if entry["original"] is None:
                    os.link(stage, path)
                    stage.unlink()
                else:
                    self._check_original(path, entry)
                    stage.replace(path)
            state["committed"] = True
            self._save(state)
        except BaseException:
            # Read durable state: a committed journal must never be rolled back.
            durable = self._load()
            if not durable["committed"]:
                self._rollback(durable)
            raise
        self._cleanup()

    def _check_original(self, path: Path, entry: Entry) -> None:
        if (
            not _regular(path)
            or path.read_bytes() != base64.b64decode(entry["original"] or "")
            or stat.S_IMODE(path.stat().st_mode) != entry["mode"]
            or path.stat().st_mtime_ns != entry["mtime"]
        ):
            raise ConfigError(f"{path}: owner changed .gitignore during initialization")

    def _rollback(self, state: Journal) -> None:
        problems: list[str] = []
        for entry in reversed(state["entries"]):
            if not entry["intent"]:
                continue
            path = self._target(entry["target"])
            try:
                if not path.exists() and not path.is_symlink():
                    continue
                info = path.lstat()
                original = entry["original"]
                if (
                    original is not None
                    and (info.st_ino, info.st_dev) != (entry["inode"], entry["device"])
                    and _regular(path)
                    and path.read_bytes() == base64.b64decode(original)
                ):
                    continue
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_ino != entry["inode"]
                    or info.st_dev != entry["device"]
                    or stat.S_IMODE(info.st_mode) != entry["published_mode"]
                    or info.st_mtime_ns != entry["published_mtime"]
                    or _digest(path.read_bytes()) != entry["digest"]
                ):
                    problems.append(str(path))
                    continue
                if original is None:
                    path.unlink()
                else:
                    restore = self.namespace / "restore"
                    _atomic(restore, base64.b64decode(original), entry["mode"])
                    os.utime(restore, ns=(entry["atime"], entry["mtime"]))
                    restore.replace(path)
            except OSError as error:
                problems.append(f"{path}: {error}")
        for directory in reversed(state["directories"]):
            path = self._target(directory["target"])
            try:
                if path.exists() or path.is_symlink():
                    info = path.lstat()
                    if (
                        not stat.S_ISDIR(info.st_mode)
                        or info.st_ino != directory["inode"]
                        or info.st_dev != directory["device"]
                    ):
                        problems.append(f"{path}: directory ownership ambiguous")
                    elif not any(path.iterdir()):
                        path.rmdir()
            except OSError as error:
                problems.append(f"{path}: {error}")
        if problems:
            raise ConfigError(
                "initialization recovery needs inspection; preserved journal and paths: "
                + ", ".join(problems)
            )
        self._cleanup()


def create(directory: Path) -> WorkspaceInitializer:
    return WorkspaceInitializer(directory)
