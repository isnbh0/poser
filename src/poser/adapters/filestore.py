"""The render store on the filesystem: one directory per song, one directory per kind of file.

A song's directory is `<renders dir>/<song id>`, and it holds:

- `.lock`, held for a whole run;
- `stems/<node><stem>`, each node's stem;
- `stamps/<node>.json`, each stem's render stamp, with the stem file's size and modification
  time when it was stamped;
- `latest/`, the latest results: `report.json`, `mix<stem>`, and `images/<node or [mix]><image>`;
- `history/<run>/`, the records: `song<record>`, `report.json`, and
  `images/<node or [mix]><image>`.

Every run takes the lock, so every song directory that holds anything holds `.lock`, and
`song` refuses an id whose directory would be inside another song's, or hold one. `lock` checks
again before it creates a song's `.lock`, holding `.songs.lock` in the renders directory while
it does, so two handles taken before either locked cannot nest. Symlinks under the renders
directory are followed as the filesystem follows them.
"""

import fcntl
import json
import os
import shutil
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

from poser.adapters.loading import refuse_options
from poser.core.cache import Stamp, StampProblem
from poser.core.jsonvalue import JsonValue, json_object
from poser.core.names import Identifier, NodeName, SongId, Suffix
from poser.core.ports import AdapterContext, Record, SongBusyError
from poser.core.song import is_int, is_number
from poser.core.statuses import StemStatus

LOCK_NAME = ".lock"
SONGS_LOCK_NAME = ".songs.lock"  # The leading dot keeps it apart from every song's directory.
STEMS_DIR = "stems"
STAMPS_DIR = "stamps"
LATEST_DIR = "latest"
IMAGES_DIR = "images"
HISTORY_DIR = "history"
REPORT_NAME = "report.json"
MIX_NAME = "mix"
MIX_IMAGE = "[mix]"  # Brackets cannot appear in a node name.
SONG_NAME = "song"
PREVIEWS_DIR = ".sound-previews"  # The leading dot keeps it apart from every song's directory.
STAMP_KEYS = frozenset(
    {"track_key", "sound_key", "status", "peak_dbfs", "stem_size", "stem_mtime_ns"}
)

GUIDE = """\
## Where results are kept

Each song's results are under `renders/<song path>/`, such as `renders/songs/demo.yaml/` for
`songs/demo.yaml`:

- `stems/`: each node's rendered stem, as `<node>{stem}`. A later render reuses a stem whose
  node and sound are unchanged.
- `latest/`: the latest analysis: `report.json`, the mix as `mix{stem}`, and an image per node
  and one for the mix (`[mix]`) in `latest/images/`. It exists only when every node analyzed.
- `history/<n>/`: one record per analysis, numbered from 1: the song as it was analyzed (`song`
  with the record's suffix, such as `song.yaml`), that run's `report.json`, and its images in
  the record's `images/` directory.
"""


def _image_name(node: NodeName | None, suffix: Suffix) -> str:
    return f"{MIX_IMAGE if node is None else node}{suffix}"


def _write_atomic(path: Path, text: str) -> None:
    """Write text through a temporary file and `os.replace`, so a reader never sees part of it."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
        # mkstemp creates the file owner-only; give it the usual umask-based mode.
        umask = os.umask(0)
        os.umask(umask)
        temp.chmod(0o666 & ~umask)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


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


def _parse_stamp(text: str) -> tuple[Stamp, int, int] | None:
    """Return a stamp file's stamp, stem size, and stem modification time, or None."""
    try:
        fields = json_object(json.loads(text))
    except ValueError:
        return None
    if fields is None or set(fields) != STAMP_KEYS:
        return None
    size, mtime, status = fields["stem_size"], fields["stem_mtime_ns"], fields["status"]
    track_key, sound_key, peak = fields["track_key"], fields["sound_key"], fields["peak_dbfs"]
    if not (is_int(size) and is_int(mtime) and is_number(peak)):
        return None
    if not (isinstance(track_key, str) and isinstance(sound_key, str)):
        return None
    if not (isinstance(status, str) and status in StemStatus):
        return None
    try:
        stamp = Stamp(track_key, sound_key, StemStatus(status), float(peak))
    except ValueError:
        return None
    return stamp, size, mtime


class FileSongStore:
    """Everything kept for one song, in its own directory."""

    def __init__(self, renders_dir: Path, song_id: SongId, stem_suffix: Suffix) -> None:
        self._renders_dir = renders_dir
        self._song_id = song_id
        self._directory = renders_dir / song_id
        self._stem_suffix = stem_suffix

    def _dir(self, *parts: str) -> Path:
        path = self._directory.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @contextmanager
    def lock(self) -> Generator[None]:
        """Hold a song's lock for a run, or raise SongBusyError at once if another run holds it.

        Two runs on one song would stamp each other's stems and number the same history record.
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

    def _stem_file(self, node: NodeName) -> Path:
        return self._directory / STEMS_DIR / f"{node}{self._stem_suffix}"

    def stem_path(self, node: NodeName) -> Path:
        self._dir(STEMS_DIR)
        return self._stem_file(node)

    def _stamp_path(self, node: NodeName) -> Path:
        return self._directory / STAMPS_DIR / f"{node}.json"

    def read_stamp(self, node: NodeName) -> Stamp | StampProblem:
        try:
            text = self._stamp_path(node).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return StampProblem.MISSING
        parsed = _parse_stamp(text)
        if parsed is None:
            return StampProblem.MISSING
        stamp, size, mtime = parsed
        try:
            info = self._stem_file(node).stat()
        except OSError:
            return StampProblem.CHANGED
        if (info.st_size, info.st_mtime_ns) != (size, mtime):
            return StampProblem.CHANGED
        return stamp

    def write_stamp(self, node: NodeName, stamp: Stamp) -> None:
        info = self.stem_path(node).stat()
        fields = {
            "track_key": stamp.track_key,
            "sound_key": stamp.sound_key,
            "status": str(stamp.status),
            "peak_dbfs": stamp.peak_dbfs,
            "stem_size": info.st_size,
            "stem_mtime_ns": info.st_mtime_ns,
        }
        self._dir(STAMPS_DIR)
        _write_atomic(self._stamp_path(node), json.dumps(fields, allow_nan=False) + "\n")

    def discard(self, node: NodeName) -> None:
        self._stamp_path(node).unlink(missing_ok=True)
        self._stem_file(node).unlink(missing_ok=True)

    def clear_latest(self) -> None:
        shutil.rmtree(self._directory / LATEST_DIR, ignore_errors=True)

    def image_path(self, node: NodeName | None, suffix: Suffix) -> Path:
        return self._dir(LATEST_DIR, IMAGES_DIR) / _image_name(node, suffix)

    def mix_path(self) -> Path:
        return self._dir(LATEST_DIR) / f"{MIX_NAME}{self._stem_suffix}"

    def report_path(self) -> Path:
        return self._dir(LATEST_DIR) / REPORT_NAME

    def start_record(self, song_text: str, suffix: Suffix) -> Record:
        history = self._dir(HISTORY_DIR)
        # A record left without a report by an I/O error still counts.
        taken = [int(entry.name) for entry in history.iterdir() if entry.name.isdecimal()]
        run = max(taken, default=0) + 1
        record = history / str(run)
        record.mkdir()
        _write_atomic(record / f"{SONG_NAME}{suffix}", song_text)
        return Record(run, record)

    def keep_image(self, record: Record, node: NodeName | None, suffix: Suffix) -> Path:
        images = record.path / IMAGES_DIR
        images.mkdir(exist_ok=True)
        target = images / _image_name(node, suffix)
        shutil.copyfile(self.image_path(node, suffix), target)
        return target

    def finish_record(self, record: Record, report_text: str) -> None:
        _write_atomic(record.path / REPORT_NAME, report_text)


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
