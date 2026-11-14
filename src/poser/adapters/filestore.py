"""The render store on the filesystem: one directory per song, units kept by key, records kept by
number.

A song's directory is `<renders dir>/<song id>`, and it holds:

- `.lock`, held for a whole run;
- `objects/<key>/`, one render unit: `meta.json`, listing each stem's status and peak, and the
  stems `0<stem>`, `1<stem>`, ..., one per node of the unit in the unit's node order;
- `history/<n>/`, the records, numbered from 1: `song<record>`, `report.json`, and, in a
  complete record, `audio/mix<stem>` and `images/`;
- `latest`, a symbolic link to `history/<n>`, present only while the newest record is complete.

A unit or record is written in a `.tmp-<random>` directory beside its final place and renamed
into place when complete, and `latest` is replaced by renaming a new link over it, so a reader
sees each complete or not at all. `sweep` deletes the drafts an interrupted run left. Node names
never appear in a path.

Every run takes the lock, so every song directory that holds anything holds `.lock`, and
`song` refuses an id whose directory would be inside another song's, or hold one. `lock` checks
again before it creates a song's `.lock`, holding `.songs.lock` in the renders directory while
it does, so two handles taken before either locked cannot nest. Symlinks under the renders
directory are followed as the filesystem follows them.
"""

import errno
import fcntl
import json
import os
import re
import shutil
import tempfile
from collections.abc import Collection, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from poser.adapters.loading import refuse_options
from poser.core.jsonvalue import JsonValue, json_object
from poser.core.names import Identifier, SongId, Suffix
from poser.core.ports import AdapterContext, SongBusyError, StemMeta
from poser.core.song import is_number
from poser.core.statuses import StemStatus

LOCK_NAME = ".lock"
SONGS_LOCK_NAME = ".songs.lock"  # The leading dot keeps it apart from every song's directory.
OBJECTS_DIR = "objects"
HISTORY_DIR = "history"
LATEST_NAME = "latest"
META_NAME = "meta.json"
SONG_NAME = "song"
DRAFT_PREFIX = ".tmp-"
PREVIEWS_DIR = ".sound-previews"  # The leading dot keeps it apart from every song's directory.

_KEY = re.compile(r"[0-9a-f]{64}")

GUIDE = """\
## Where results are kept

Each song's results are under `renders/<song path>/`, such as `renders/songs/demo.yaml/` for
`songs/demo.yaml`:

- `latest`: a link to the newest record, present only when every node of that analysis
  analyzed. The mix is `latest/audio/mix{stem}`, and the images are in `latest/images/`.
- `history/<n>/`: one record per analysis, numbered from 1: the song as it was analyzed (`song`
  with the record's suffix, such as `song.yaml`) and that run's `report.json`, and, when every
  node analyzed, the mix in `audio/` and an image per node and one for the mix in `images/`.
  Paths in a report are relative to its record's directory.
- `objects/`: rendered stems, one directory per render unit, named by a digest of everything
  the unit's render reads. A later render reuses a unit whose digest is unchanged. poser owns
  this directory: it does not notice a stem edited in place, and `render-song --force` renders
  every unit again.
"""


def _write_atomic(path: Path, text: str) -> None:
    """Write text through a temporary file and `os.replace`, so a reader never sees part of it."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        # mkstemp creates the file owner-only; give it the usual umask-based mode.
        umask = os.umask(0)
        os.umask(umask)
        temp.chmod(0o666 & ~umask)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _sync_tree(directory: Path) -> None:
    """Flush every file under `directory` to disk, so a rename publishes complete contents."""
    for path in directory.rglob("*"):
        if path.is_file() and not path.is_symlink():
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def _draft_dir(parent: Path) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    draft = Path(tempfile.mkdtemp(dir=parent, prefix=DRAFT_PREFIX))
    # mkdtemp creates the directory owner-only; give it the usual umask-based mode.
    umask = os.umask(0)
    os.umask(umask)
    draft.chmod(0o777 & ~umask)
    return draft


def _check_key(key: str) -> None:
    if not (isinstance(key, str) and _KEY.fullmatch(key)):
        raise ValueError(f"a unit key must be 64 lowercase hex digits, got {key!r}")


def _inside_another_song(renders_dir: Path, song_id: SongId) -> str | None:
    """Return why a song's directory is inside another song's, or None."""
    parts = song_id.split("/")
    for depth in range(1, len(parts)):
        if (renders_dir.joinpath(*parts[:depth]) / LOCK_NAME).exists():
            return (
                f"song {song_id!r} would be inside the directory of the song "
                f"{'/'.join(parts[:depth])!r}"
            )
    return None


def _holding_another_song(song_id: SongId) -> str:
    return f"song {song_id!r} would hold other songs' directories"


@contextmanager
def _flock(path: Path, operation: int) -> Generator[None]:
    """Hold an advisory lock on `path`, creating the file. BlockingIOError if it is held."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, operation)
        yield
    finally:
        os.close(fd)


def _parse_meta(text: str) -> tuple[StemMeta, ...] | None:
    """Return the stems a unit's `meta.json` lists, or None if it is not a unit's metadata."""
    try:
        fields = json_object(json.loads(text))
    except ValueError:
        return None
    if fields is None or set(fields) != {"stems"} or not isinstance(fields["stems"], list):
        return None
    stems: list[StemMeta] = []
    for entry in fields["stems"]:
        stem = json_object(entry)
        if stem is None or set(stem) != {"status", "peak_dbfs"}:
            return None
        status, peak = stem["status"], stem["peak_dbfs"]
        if not (isinstance(status, str) and status in StemStatus and is_number(peak)):
            return None
        stems.append(StemMeta(StemStatus(status), float(peak)))
    return tuple(stems) or None


class FileStoredUnit:
    """A published unit's directory."""

    def __init__(self, directory: Path, stem_suffix: Suffix, stems: tuple[StemMeta, ...]) -> None:
        self._directory = directory
        self._stem_suffix = stem_suffix
        self._stems = stems

    def stem_path(self, index: int) -> Path:
        if not 0 <= index < len(self._stems):
            raise IndexError(f"the unit has {len(self._stems)} stems, not one at {index}")
        return self._directory / f"{index}{self._stem_suffix}"

    def stems(self) -> tuple[StemMeta, ...]:
        return self._stems


class FileUnitDraft:
    """A unit being written in its draft directory."""

    def __init__(self, directory: Path, stem_suffix: Suffix) -> None:
        self.directory = directory
        self._stem_suffix = stem_suffix
        self.described: dict[int, StemMeta] = {}

    def stem_path(self, index: int) -> Path:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(f"a stem index must be a non-negative integer, got {index!r}")
        return self.directory / f"{index}{self._stem_suffix}"

    def describe(self, index: int, status: StemStatus, peak_dbfs: float) -> None:
        self.described[index] = StemMeta(status, peak_dbfs)

    def meta_text(self) -> str:
        """Return `meta.json`'s text. Raises ValueError unless every index from 0 up has been
        described and written, and no other."""
        count = len(self.described)
        if count == 0 or set(self.described) != set(range(count)):
            raise ValueError(
                f"a unit must describe its stems from 0 up, got {sorted(self.described)}"
            )
        missing = [i for i in range(count) if not self.stem_path(i).is_file()]
        if missing:
            raise ValueError(f"the unit's draft has no stem at {missing}")
        stems = [
            {"status": str(meta.status), "peak_dbfs": meta.peak_dbfs}
            for _, meta in sorted(self.described.items())
        ]
        return json.dumps({"stems": stems}, allow_nan=False) + "\n"


class FileRecordDraft:
    """A record being written in its draft directory."""

    def __init__(self, directory: Path, run: int) -> None:
        self.directory = directory
        self.run = run

    def path(self, relative: str) -> Path:
        parts = PurePosixPath(relative).parts
        if not parts or PurePosixPath(relative).is_absolute() or ".." in parts:
            raise ValueError(f"a record path must stay inside the record, got {relative!r}")
        path = self.directory.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def relative(self, path: Path) -> str:
        return path.relative_to(self.directory).as_posix()


class FileSongStore:
    """Everything kept for one song, in its own directory."""

    def __init__(self, renders_dir: Path, song_id: SongId, stem_suffix: Suffix) -> None:
        self._renders_dir = renders_dir
        self._song_id = song_id
        self._directory = renders_dir / song_id
        self._stem_suffix = stem_suffix

    @contextmanager
    def lock(self) -> Generator[None]:
        """Hold a song's lock for a run, or raise SongBusyError at once if another run holds it.

        Two runs on one song would publish over each other's units and number the same record.
        The operating system releases the lock when its descriptor closes, even if the process
        dies, so the lock file left on disk means nothing by itself.
        """
        self._renders_dir.mkdir(parents=True, exist_ok=True)
        with _flock(self._renders_dir / SONGS_LOCK_NAME, fcntl.LOCK_EX):
            self._check_nesting()
            self._directory.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SongBusyError(
                    f"another render-song or analyze-song is running on {self._directory.name}"
                ) from error
            yield
        finally:
            os.close(fd)

    def _check_nesting(self) -> None:
        """Raise ValueError if another song's directory took this one's place since `song`.

        Once a song's `.lock` exists, no song can lock inside or around it, so only a song
        without one is searched for another song's `.lock` below it.
        """
        if problem := _inside_another_song(self._renders_dir, self._song_id):
            raise ValueError(problem)
        if (self._directory / LOCK_NAME).exists() or not self._directory.is_dir():
            return
        if any(self._directory.rglob(LOCK_NAME)):
            raise ValueError(_holding_another_song(self._song_id))

    def sweep(self) -> None:
        for parent in (self._directory / OBJECTS_DIR, self._directory / HISTORY_DIR):
            if parent.is_dir():
                for entry in parent.iterdir():
                    if entry.name.startswith(DRAFT_PREFIX):
                        shutil.rmtree(entry, ignore_errors=True)
        if self._directory.is_dir():
            for entry in self._directory.glob(f"{LATEST_NAME}{DRAFT_PREFIX}*"):
                entry.unlink(missing_ok=True)

    def find(self, key: str) -> FileStoredUnit | None:
        _check_key(key)
        directory = self._directory / OBJECTS_DIR / key
        try:
            text = (directory / META_NAME).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        stems = _parse_meta(text)
        if stems is None:
            return None
        names = {f"{index}{self._stem_suffix}" for index in range(len(stems))}
        held = {entry.name for entry in directory.iterdir() if entry.name != META_NAME}
        if held != names:
            return None
        return FileStoredUnit(directory, self._stem_suffix, stems)

    @contextmanager
    def write_unit(self, key: str, *, replace: bool = False) -> Generator[FileUnitDraft]:
        _check_key(key)
        objects = self._directory / OBJECTS_DIR
        draft = FileUnitDraft(_draft_dir(objects), self._stem_suffix)
        try:
            yield draft
            _write_atomic(draft.directory / META_NAME, draft.meta_text())
            _sync_tree(draft.directory)
            self._publish(draft.directory, objects / key, replace=replace)
        finally:
            shutil.rmtree(draft.directory, ignore_errors=True)

    def _publish(self, draft: Path, target: Path, *, replace: bool) -> None:
        """Rename the draft to `target`. An existing complete unit there is kept unless
        `replace`; anything else there is swapped out and deleted."""
        try:
            draft.rename(target)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
        else:
            return
        if not replace and self.find(target.name) is not None:
            return
        old = target.parent / f"{DRAFT_PREFIX}{os.urandom(8).hex()}"
        target.rename(old)
        try:
            draft.rename(target)
        finally:
            shutil.rmtree(old, ignore_errors=True)

    def keep_units(self, keys: Collection[str]) -> None:
        objects = self._directory / OBJECTS_DIR
        if not objects.is_dir():
            return
        kept = set(keys)
        for entry in objects.iterdir():
            if not entry.name.startswith(DRAFT_PREFIX) and entry.name not in kept:
                shutil.rmtree(entry, ignore_errors=True)

    @contextmanager
    def write_record(
        self, song_text: str, suffix: Suffix, *, latest: bool
    ) -> Generator[FileRecordDraft]:
        history = self._directory / HISTORY_DIR
        history.mkdir(parents=True, exist_ok=True)
        taken = [int(entry.name) for entry in history.iterdir() if entry.name.isdecimal()]
        run = max(taken, default=0) + 1
        draft = FileRecordDraft(_draft_dir(history), run)
        try:
            _write_atomic(draft.path(f"{SONG_NAME}{suffix}"), song_text)
            yield draft
            _sync_tree(draft.directory)
            draft.directory.rename(self.record_path(run))
        finally:
            shutil.rmtree(draft.directory, ignore_errors=True)
        if latest:
            self._point_latest(f"{HISTORY_DIR}/{run}")
        else:
            self.clear_latest()

    def _point_latest(self, target: str) -> None:
        """Replace `latest` with a link to `target`, relative to the song's directory, in one
        rename."""
        link = self._directory / LATEST_NAME
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)  # A `latest` directory from an older layout.
        temp = self._directory / f"{LATEST_NAME}{DRAFT_PREFIX}{os.urandom(8).hex()}"
        temp.symlink_to(target, target_is_directory=True)
        try:
            temp.replace(link)
        finally:
            temp.unlink(missing_ok=True)

    def record_path(self, run: int) -> Path:
        return self._directory / HISTORY_DIR / str(run)

    def clear_latest(self) -> None:
        link = self._directory / LATEST_NAME
        if link.is_symlink() or link.is_file():
            link.unlink()
        elif link.is_dir():
            shutil.rmtree(link)  # A `latest` directory from an older layout.


class FileRenderStore:
    """Songs' results and saved sounds' previews under one renders directory."""

    def __init__(self, renders_dir: Path, stem_suffix: Suffix) -> None:
        self._renders_dir = renders_dir
        self._stem_suffix = stem_suffix

    @property
    def stem_suffix(self) -> Suffix:
        return self._stem_suffix

    @property
    def guide(self) -> str:
        return GUIDE.format(stem=self._stem_suffix)

    def protected_roots(self) -> tuple[Path, ...]:
        """Declare confinement, refusing existing links that escape the store."""
        root = self._renders_dir.resolve()
        if root.exists():
            for path in root.rglob("*"):
                if path.is_symlink() and not path.resolve().is_relative_to(root):
                    raise ValueError(f"render store symlink escapes its protected root: {path}")
        return (root,)

    def song(self, song_id: SongId) -> FileSongStore:
        parts = SongId(song_id).split("/")
        if parts[0].startswith("."):
            raise ValueError(f"song {song_id!r}: names starting with '.' are the store's own")
        if problem := _inside_another_song(self._renders_dir, song_id):
            raise ValueError(problem)
        directory = self._renders_dir / song_id
        if directory.is_dir() and not (directory / LOCK_NAME).exists() and any(directory.iterdir()):
            raise ValueError(_holding_another_song(song_id))
        return FileSongStore(self._renders_dir, song_id, self._stem_suffix)

    def preview_path(self, instrument: Identifier, name: Identifier) -> Path:
        directory = self._renders_dir / PREVIEWS_DIR / instrument
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{name}{self._stem_suffix}"


def create(
    options: Mapping[str, JsonValue], context: AdapterContext, stem_suffix: Suffix
) -> FileRenderStore:
    """The factory for `files`: a store under the command's renders directory. Takes no options."""
    refuse_options(options)
    return FileRenderStore(context.renders_dir, stem_suffix)
