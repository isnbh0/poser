"""The Pedalboard instrument: a plugin, its preset sources, and its saved sounds.

Raise `adapter_version` whenever this adapter's code renders one identity differently. The
`pedalboard` library's version and the plugin's version are already in the identity, so upgrading
either re-renders its stems with no one bumping a version by hand. The identity is a recipe:
Serum's default patch does not repeat between renders, and its stems are still cached.
"""

import fcntl
import os
import plistlib
import threading
import xml.parsers.expat
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from importlib import metadata, resources
from pathlib import Path

from poser.adapters.isolation import WorkerCrashedError, WorkerTimedOutError, run_isolated
from poser.adapters.pedalboard.config import SAVED_SOURCE, PedalboardConfig
from poser.adapters.pedalboard.formats import PresetError, PresetFormat, SavedFormat, SavedSound
from poser.adapters.pedalboard.sources import (
    DEFAULT_DESCRIPTION,
    SourceScan,
    ambiguity,
    combine,
    describe,
    group_by_name,
    list_saved,
    saved_dir,
    scan_configured,
    scan_saved,
)
from poser.adapters.pedalboard.state import decode_state, encode_state
from poser.adapters.pedalboard.worker import capture_job, render_job
from poser.core.jsonvalue import Json, JsonValue, digest, json_object
from poser.core.names import Identifier, Suffix
from poser.core.performance import performance_to_data
from poser.core.ports import (
    Captured,
    CaptureFailure,
    CaptureRequest,
    RenderFailure,
    RenderRequest,
    ResolvedSound,
    SoundFailure,
    SoundListing,
)
from poser.core.sounds import SoundEntry, SoundRef
from poser.core.statuses import RenderFailureStatus, SoundFailureStatus

LOCK_NAME = ".lock"  # Hidden, so scans skip it.
MALFORMED_RESULT = "worker wrote a malformed result"


def plugin_version(path: Path) -> str | None:
    """Return a plugin bundle's version from its `Info.plist`, or None when it has none.

    The build number counts, so a build-only update changes the version. An unreadable or corrupt
    `Info.plist` gives None, as a missing one does, so it never stops a sound from resolving.
    """
    try:
        with (path / "Contents" / "Info.plist").open("rb") as plist:
            info = plistlib.load(plist)
    except (OSError, ValueError, xml.parsers.expat.ExpatError):
        # plistlib.InvalidFileException is a ValueError.
        return None
    fields = json_object(info)
    if fields is None:
        return None
    short, build = fields.get("CFBundleShortVersionString"), fields.get("CFBundleVersion")
    if isinstance(short, str) and isinstance(build, str):
        return f"{short} ({build})"
    if isinstance(short, str):
        return short
    if isinstance(build, str):
        return build
    return None


def payload_state(payload: Json) -> str | None:
    """Return the base64 state in a payload this instrument made."""
    fields = json_object(payload.value())
    state = None if fields is None else fields.get("state")
    if fields is None or set(fields) != {"state"} or not (state is None or isinstance(state, str)):
        raise ValueError(f"not a Pedalboard payload: {payload.text}")
    return state


class PedalboardInstrument:
    """A plugin loaded through Pedalboard, with presets from its sources and saved sounds.

    Building one reads nothing and starts nothing. The first `sounds` or `resolve` scans the
    sources, and the instrument keeps that scan for its lifetime, except that `store` rescans the
    saved source.
    """

    def __init__(
        self, config: PedalboardConfig, sounds_dir: Path, formats: Mapping[Suffix, PresetFormat]
    ) -> None:
        if not formats:
            raise ValueError(f"instrument {config.name!r} needs at least one preset format")
        for suffix, fmt in formats.items():
            if suffix != fmt.suffix:
                raise ValueError(f"preset format for {suffix!r} has the suffix {fmt.suffix!r}")
        self._config = config
        self._sounds_dir = sounds_dir
        self._formats = dict(formats)
        self._lock = threading.Lock()
        self._configured: SourceScan | None = None
        self._saved: SourceScan | None = None
        self._plugin_version: tuple[str | None] | None = None

    @property
    def adapter_id(self) -> Identifier:
        return Identifier("pedalboard")

    @property
    def adapter_version(self) -> str:
        return "1"

    @property
    def guide(self) -> str:
        return resources.files(__package__).joinpath("guide.md").read_text(encoding="utf-8")

    def _scan(self) -> SourceScan:
        with self._lock:
            if self._configured is None or self._saved is None:
                self._configured = scan_configured(self._config, self._formats)
                self._saved = scan_saved(self._config, self._sounds_dir)
            return combine(self._configured, self._saved)

    def _refresh_saved(self) -> None:
        """Rescan the saved source after a store, if a scan was made."""
        with self._lock:
            if self._configured is not None:
                self._saved = scan_saved(self._config, self._sounds_dir)

    def _version(self) -> str | None:
        with self._lock:
            if self._plugin_version is None:
                self._plugin_version = (plugin_version(self._config.path),)
            return self._plugin_version[0]

    def _ref(self, name: str | None) -> SoundRef:
        return SoundRef(self._config.name, name)

    def sounds(self) -> SoundListing:
        scan = self._scan()
        entries = [SoundEntry(self._ref(None), DEFAULT_DESCRIPTION)]
        problems = list(scan.problems)
        for name in sorted(scan.files):
            path, fmt = scan.files[name]
            try:
                entries.append(SoundEntry(self._ref(name), describe(name, path, fmt)))
            except PresetError as error:
                problems.append(f"{name}: {error}")
        return SoundListing(tuple(entries), tuple(problems))

    def resolve(self, ref: SoundRef) -> ResolvedSound | SoundFailure:
        state: bytes | None = None
        if ref.name is not None:
            scan = self._scan()
            source = ref.name.split("/", 1)[0]
            kind = "saved sound" if source == SAVED_SOURCE else "preset"
            if source in scan.unavailable:
                problem = scan.source_problems.get(Identifier(source), "source unavailable")
                return SoundFailure(SoundFailureStatus.LOAD_FAILED, f"{kind} {ref}: {problem}")
            found = scan.files.get(ref.name)
            if found is None:
                detail = f"{self._config.name!r} has no sound {ref.name!r}"
                if problem := scan.name_problems.get(ref.name):
                    detail = f"{detail}; {problem}"
                return SoundFailure(SoundFailureStatus.UNKNOWN_SOUND, detail)
            path, fmt = found
            try:
                state = fmt.read_state(path)
            except PresetError as error:
                return SoundFailure(SoundFailureStatus.LOAD_FAILED, f"{kind} {ref}: {error}")
        text = encode_state(state) if state else None
        identity = digest(
            {
                "path": str(self._config.path),
                "plugin_name": self._config.plugin_name,
                "plugin_version": self._version(),
                "pedalboard": metadata.version("pedalboard"),
                "state": text,
            }
        )
        return ResolvedSound(ref, identity, Json.of({"state": text}))

    def _job(self, state: str | None) -> dict[str, JsonValue]:
        return {
            "path": str(self._config.path),
            "plugin_name": self._config.plugin_name,
            "state": state,
        }

    def render(self, request: RenderRequest, timeout_s: float) -> RenderFailure | None:
        job = self._job(payload_state(request.sound.payload))
        job["performance"] = performance_to_data(request.performance)
        job["audio_path"] = str(request.audio_path)
        try:
            result = run_isolated(render_job, job, timeout_s)
        except WorkerTimedOutError as error:
            return RenderFailure(RenderFailureStatus.TIMED_OUT, str(error))
        except WorkerCrashedError as error:
            return RenderFailure(RenderFailureStatus.CRASHED, str(error))
        match result:
            case {"status": "ok", **rest} if not rest:
                return None
            case {"status": "load_failed", "detail": str(detail), **rest} if detail and not rest:
                return RenderFailure(RenderFailureStatus.LOAD_FAILED, detail)
            case _:
                return RenderFailure(RenderFailureStatus.CRASHED, MALFORMED_RESULT)

    def capture(self, request: CaptureRequest) -> Captured | CaptureFailure:
        start = None if request.start is None else payload_state(request.start.payload)
        job = self._job(start)
        job["performance"] = performance_to_data(request.preview)
        job["preview_path"] = str(request.preview_path)
        try:
            # No timeout, because the owner decides when to close the window.
            result = run_isolated(capture_job, job, None)
        except (WorkerTimedOutError, WorkerCrashedError) as error:
            return CaptureFailure(str(error))
        match result:
            case {"state": str(state), **rest} if not rest:
                try:
                    decode_state(state)
                except ValueError as error:
                    return CaptureFailure(f"worker returned an unusable state: {error}")
                return Captured(Json.of({"state": state}))
            case {"error": str(message), **rest} if message and not rest:
                return CaptureFailure(message)
            case _:
                return CaptureFailure(MALFORMED_RESULT)

    def check_name(self, name: Identifier, *, replace: bool) -> str | None:
        target = self._target(name, replace=replace)
        return target if isinstance(target, str) else None

    def _target(self, name: Identifier, *, replace: bool) -> Path | str:
        """Return the file a store of `name` writes, or why it cannot.

        Replacing writes over the existing file, whatever its suffix's case, so a case-sensitive
        volume never ends up with two files for one name.
        """
        directory = saved_dir(self._config, self._sounds_dir)
        target = directory / f"{name}{SavedFormat().suffix}"
        if not directory.is_dir():
            return target
        try:
            by_name = group_by_name(list_saved(directory))
        except PresetError as error:
            return str(error)
        for other in sorted(by_name):
            if other.lower() == name.lower() and other != name:
                return f"saved sound {other!r} differs only in case"
        existing = by_name.get(name, [])
        if len(existing) > 1:
            return ambiguity(f"saved sound {name!r}", existing)
        if existing and not replace:
            return f"saved sound {name!r} exists; pass --replace"
        return existing[0] if existing else target

    @contextmanager
    def _saved_lock(self, directory: Path) -> Generator[None]:
        """Hold the saved directory's lock, waiting for another store to finish."""
        fd = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def store(
        self, name: Identifier, description: str, captured: Captured, *, replace: bool
    ) -> SoundRef | str:
        sound = SavedSound(description, decode_state(payload_state(captured.payload)))
        directory = saved_dir(self._config, self._sounds_dir)
        directory.mkdir(parents=True, exist_ok=True)
        with self._saved_lock(directory):
            target = self._target(name, replace=replace)
            if isinstance(target, str):
                return target
            SavedFormat().write_sound(target, sound)
        self._refresh_saved()
        return self._ref(f"{SAVED_SOURCE}/{name}")
