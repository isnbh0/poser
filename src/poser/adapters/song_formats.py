"""Song formats: YAML song files and Python song scripts."""

import os
import runpy
import stat
import tempfile
from pathlib import Path

import yaml

from poser.core.names import Suffix
from poser.core.ports import SongFormat
from poser.core.song import Song, SongError, parse_song, song_to_data


class YamlSongFormat:
    """Song data as YAML. It also writes the song into analysis records."""

    @property
    def suffixes(self) -> tuple[Suffix, ...]:
        return (Suffix(".yaml"), Suffix(".yml"))

    @property
    def suffix(self) -> Suffix:
        return Suffix(".yaml")

    def load(self, path: Path) -> Song:
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
            raise SongError([f"cannot read {path}: {error}"]) from error
        return parse_song(data)

    def dump(self, song: Song) -> str:
        try:
            return yaml.safe_dump(song_to_data(song), sort_keys=False)
        except yaml.YAMLError as error:
            raise SongError([f"cannot write song: {error}"]) from error


class ScriptSongFormat:
    """A Python script whose `build()` returns a Song."""

    @property
    def suffixes(self) -> tuple[Suffix, ...]:
        return (Suffix(".py"),)

    def load(self, path: Path) -> Song:
        # A song script is the owner's or agent's own code, run like any project script.
        try:
            build = runpy.run_path(str(path)).get("build")
        except SongError as error:
            raise _in_script(path, error) from error
        except (Exception, SystemExit) as error:
            raise SongError([f"cannot run {path}: {type(error).__name__}: {error}"]) from error
        if not callable(build):
            raise SongError([f"{path}: must define a function build() that returns a Song"])
        try:
            song = build()
        except SongError as error:
            raise _in_script(path, error) from error
        except (Exception, SystemExit) as error:
            raise SongError([f"{path}: build() raised {type(error).__name__}: {error}"]) from error
        if not isinstance(song, Song):
            raise SongError([f"{path}: build() must return a Song, got {type(song).__name__}"])
        return song


def _in_script(path: Path, error: SongError) -> SongError:
    """Return a script's song error with the script's path before each problem."""
    return SongError([f"{path}: {problem}" for problem in error.problems])


SONG_FORMATS: tuple[SongFormat, ...] = (YamlSongFormat(), ScriptSongFormat())


def write_yaml_song(song: Song, path: Path) -> None:
    """Write a song as YAML, replacing `path` only once the file is complete.

    A symlinked `path` keeps its link and the target is replaced. The file keeps an existing
    target's permissions, or gets the usual umask-based ones when new.
    """
    try:
        text = YamlSongFormat().dump(song)
    except SongError as error:
        raise SongError([f"cannot write {path}: {error.__cause__}"]) from error
    target = Path(path).resolve()
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    except OSError as error:
        raise SongError([f"cannot write {path}: {error}"]) from error
    try:
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    except OSError as error:
        raise SongError([f"cannot write {path}: {error}"]) from error
    temp = Path(name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        temp.chmod(mode)
        temp.replace(target)
    except BaseException as error:
        temp.unlink(missing_ok=True)
        if isinstance(error, OSError):
            raise SongError([f"cannot write {path}: {error}"]) from error
        raise
