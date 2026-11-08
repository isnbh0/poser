"""Saved sounds: plugin states the owner chose, stored as files that songs name.

A sound named `warm-pad` is `sounds/warm-pad.json`, holding the plugin's `plugins.toml` name, the
owner's description, and the plugin's `raw_state` in base64. Serum restores from `raw_state`, not
from `preset_data`, so `raw_state` is what gets stored. This module is the only one that knows the
file format.

`save-sound` opens a plugin's window in a worker and saves the state left when the window closes.
`list-sounds` shows what a song can use. Running this module with `python -m` is the save worker.
"""

import argparse
import base64
import json
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from poser.adapters.pedalboard import state as pedalboard_state
from poser.adapters.pedalboard.formats import PresetError
from poser.adapters.pedalboard.worker import capture_state, performance_messages, render_duration_s
from poser.adapters.wav import WavAudioFiles
from poser.cli.common import EXIT_ALL_PASS, EXIT_BAD_CONFIG, EXIT_SOME_FAILED, align_rows
from poser.core.audio import (
    NUM_CHANNELS,
    SAMPLE_RATE,
    SILENCE_DBFS,
    AudioProblem,
    make_audio,
    measure_peak_dbfs,
)
from poser.core.checks import CHECK_SAMPLES, check_pattern
from poser.plugin_check import INITIALIZATION_TIMEOUT_S, ConfigError, PluginSpec, load_config
from poser.presets import preset_description, preset_files
from poser.worker import (
    MalformedJobError,
    WorkerCrashedError,
    WorkerTimedOutError,
    job_fields,
    json_object,
    optional_str,
    run_isolated,
)
from poser.workspace import WorkspaceError, fill_path_options

if TYPE_CHECKING:
    from pedalboard import AudioUnitPlugin, VST3Plugin
    from pedalboard_native import MIDIMessageLike

# Under the renders directory. The leading dot keeps it apart from any song's stem directory.
PREVIEWS_DIR = ".sound-previews"
# Like a track name, but starting with a letter or digit, so it cannot read as a command option.
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
SOUND_KEYS = {"plugin", "description", "state"}
DEFAULT_DESCRIPTION = "default patch"


class SoundError(Exception):
    """A saved sound file cannot be used."""


@dataclass(frozen=True)
class SavedSound:
    name: str
    plugin: str
    description: str
    state: str  # Standard base64 of the plugin's raw_state.


class SaveJob(TypedDict):
    plugin_path: str
    plugin_name: str | None
    state: str | None  # Base64 state to start from, or None for the plugin's default patch.
    preview_path: str


class SaveFailure(TypedDict):
    error: str


class SaveSuccess(TypedDict):
    state: str
    peak_dbfs: float


@dataclass(frozen=True)
class SoundEntry:
    name: str
    plugin: str
    description: str


def sound_path(sounds_dir: Path, name: str) -> Path:
    return sounds_dir / f"{name}.json"


def sound_files(sounds_dir: Path) -> dict[str, Path]:
    """Map each sound file's exact stem to its path, skipping hidden temporary files.

    Lookup goes through this listing, not `Path.exists`, because a default macOS volume would
    match `Pad` to `pad.json`.
    """
    if not sounds_dir.is_dir():
        return {}
    return {
        path.stem: path
        for path in sounds_dir.iterdir()
        if path.suffix == ".json" and not path.name.startswith(".")
    }


def decode_state(state: object) -> bytes:
    """Return a saved sound's state, raising SoundError with the reason it cannot be used."""
    try:
        return pedalboard_state.decode_state(state)
    except ValueError as error:
        raise SoundError(str(error)) from error


def encode_state(data: bytes) -> str:
    return pedalboard_state.encode_state(data)


def plugin_name_clash(name: str, specs: list[PluginSpec]) -> str | None:
    """Return the plugin a name matches ignoring case, so a `sound` value has one meaning."""
    return next((spec.name for spec in specs if spec.name.lower() == name.lower()), None)


def read_sound(path: Path, specs: list[PluginSpec]) -> SavedSound:
    name = path.stem
    if not NAME_PATTERN.fullmatch(name):
        raise SoundError(f"name {name!r} does not match {NAME_PATTERN.pattern}")
    if clash := plugin_name_clash(name, specs):
        raise SoundError(f"name {name!r} is taken by the plugin {clash!r}")
    try:
        data = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise SoundError(f"cannot read {path.name}: {error}") from error
    fields = json_object(data)
    if fields is None or set(fields) != SOUND_KEYS:
        raise SoundError(f"must hold exactly the keys {sorted(SOUND_KEYS)}")
    plugin, description, state = fields["plugin"], fields["description"], fields["state"]
    if not isinstance(plugin, str) or plugin not in {spec.name for spec in specs}:
        raise SoundError(f"plugin {plugin!r} is not in the plugin list")
    if not isinstance(description, str) or not description.strip():
        raise SoundError("description must be a non-empty string")
    if not isinstance(state, str):
        raise SoundError("state is not a string")
    decode_state(state)
    return SavedSound(name, plugin, description, state)


def write_sound(sounds_dir: Path, sound: SavedSound) -> Path:
    """Write a sound file through a temporary file, so a reader never sees a partial one."""
    sounds_dir.mkdir(parents=True, exist_ok=True)
    target = sound_path(sounds_dir, sound.name)
    temp = sounds_dir / f".{sound.name}.tmp.json"
    fields = {"plugin": sound.plugin, "description": sound.description, "state": sound.state}
    try:
        temp.write_text(json.dumps(fields) + "\n")
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def scan_sounds(
    sounds_dir: Path, specs: list[PluginSpec]
) -> tuple[list[SavedSound], list[tuple[str, str]]]:
    """Read every sound file, returning the usable sounds by name and (file, problem) pairs."""
    sounds, problems = [], []
    for _, path in sorted(sound_files(sounds_dir).items()):
        try:
            sounds.append(read_sound(path, specs))
        except SoundError as error:
            problems.append((path.name, str(error)))
    return sounds, problems


def list_entries(
    sounds_dir: Path, specs: list[PluginSpec]
) -> tuple[list[SoundEntry], list[tuple[str, str]]]:
    """Return plugin defaults, saved sounds by name, then each plugin's presets by name."""
    sounds, problems = scan_sounds(sounds_dir, specs)
    entries = [SoundEntry(spec.name, spec.name, DEFAULT_DESCRIPTION) for spec in specs]
    entries += [SoundEntry(sound.name, sound.plugin, sound.description) for sound in sounds]
    for spec in specs:
        try:
            names = sorted(preset_files(spec))
        except PresetError as error:
            problems.append((f"{spec.name} presets", str(error)))
            continue
        entries += [SoundEntry(name, spec.name, preset_description(name)) for name in names]
    return entries, problems


def list_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="list-sounds", description="List the sounds a song can use."
    )
    parser.add_argument("--config", type=Path, help="plugin list (TOML)")
    parser.add_argument("--sounds-dir", type=Path, help="saved sound files")
    parser.add_argument("--json", action="store_true", help="print the list as JSON")
    args = parser.parse_args(argv)

    try:
        fill_path_options(args)
    except WorkspaceError as error:
        print(f"list-sounds: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    try:
        specs = load_config(args.config)
    except ConfigError as error:
        print(f"list-sounds: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    entries, problems = list_entries(args.sounds_dir, specs)
    if args.json:
        print(json.dumps([asdict(entry) for entry in entries], indent=2))
    else:
        rows: list[tuple[str, ...]] = [("NAME", "PLUGIN", "DESCRIPTION")]
        rows += [(entry.name, entry.plugin, entry.description) for entry in entries]
        print("\n".join(align_rows(rows)))
    for file_name, problem in problems:
        print(f"list-sounds: {file_name}: {problem}", file=sys.stderr)
    return EXIT_SOME_FAILED if problems else EXIT_ALL_PASS


class UsageError(Exception):
    """A save-sound request that is refused before any window opens."""


def plan_save(args: argparse.Namespace, specs: list[PluginSpec]) -> tuple[PluginSpec, str | None]:
    """Check a save request and return the plugin and starting state, or raise UsageError."""
    name = args.name
    if not NAME_PATTERN.fullmatch(name):
        raise UsageError(f"name {name!r} does not match {NAME_PATTERN.pattern}")
    if clash := plugin_name_clash(name, specs):
        raise UsageError(f"name {name!r} is taken by the plugin {clash!r}")
    if not args.description.strip():
        raise UsageError("the description must not be empty")

    files = sound_files(args.sounds_dir)
    same_but_case = [other for other in files if other.lower() == name.lower() and other != name]
    if same_but_case:
        raise UsageError(f"name {name!r} differs only in case from the saved {same_but_case[0]!r}")
    if name in files and not args.replace:
        raise UsageError(f"a sound named {name!r} exists; pass --replace to overwrite it")

    specs_by_name = {spec.name: spec for spec in specs}
    if args.plugin is not None:
        if args.plugin not in specs_by_name:
            raise UsageError(f"no plugin named {args.plugin!r} in the plugin list")
        return specs_by_name[args.plugin], None

    if args.source not in files:
        raise UsageError(f"no saved sound named {args.source!r}")
    try:
        source = read_sound(files[args.source], specs)
    except SoundError as error:
        raise UsageError(f"saved sound {args.source!r} cannot be used: {error}") from error
    return specs_by_name[source.plugin], source.state


def parse_save_job(value: object) -> SaveJob:
    """Return a save job read back from JSON, raising MalformedJobError if it is malformed.

    The parent writes every job in `save_main`, so a malformed one is a bug.
    """
    fields = job_fields(value, set(SaveJob.__annotations__), "save job")
    path, name, state = fields["plugin_path"], fields["plugin_name"], fields["state"]
    preview = fields["preview_path"]
    if not (isinstance(path, str) and optional_str(name) and optional_str(state)):
        raise MalformedJobError("save job", value)
    if not isinstance(preview, str):
        raise MalformedJobError("save job", value)
    return {"plugin_path": path, "plugin_name": name, "state": state, "preview_path": preview}


def save_worker_command(job_path: Path) -> list[str]:
    return [sys.executable, "-m", "poser.sounds", str(job_path)]


def run_save_worker(job: SaveJob) -> tuple[str, float]:
    """Run the save worker with no timeout and return the base64 state and preview peak.

    Raises WorkerCrashedError for any failure, including a malformed result or no preview.
    """
    with tempfile.TemporaryDirectory() as directory:
        job_path = Path(directory) / "job.json"
        job_path.write_text(json.dumps(job))
        # No timeout, because the owner decides when to close the window.
        payload = run_isolated(save_worker_command(job_path), None)
    fields = json_object(payload)
    if fields is None:
        raise WorkerCrashedError("worker wrote a malformed result")
    match fields:
        case {"error": str(message)}:
            raise WorkerCrashedError(message)
        case {"state": str(state), "peak_dbfs": int(peak) | float(peak)} if len(fields) == 2:
            try:
                decode_state(state)
            except SoundError as error:
                raise WorkerCrashedError(f"worker returned an unusable state: {error}") from error
            if not Path(job["preview_path"]).is_file():
                raise WorkerCrashedError("worker wrote no preview")
            return state, float(peak)
        case _:
            raise WorkerCrashedError("worker wrote a malformed result")


def save_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="save-sound",
        description=(
            "Open a plugin's window and save its state, when the window closes, as a sound."
        ),
    )
    parser.add_argument("name", help="sound name, used as a track's `sound`")
    start = parser.add_mutually_exclusive_group(required=True)
    start.add_argument("--plugin", help="start from this plugin's default patch")
    start.add_argument("--from", dest="source", help="start from this saved sound")
    parser.add_argument("--description", required=True, help="what the sound is and is for")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing sound")
    parser.add_argument("--config", type=Path, help="plugin list (TOML)")
    parser.add_argument("--sounds-dir", type=Path, help="saved sound files")
    parser.add_argument("--output-dir", type=Path, help="where previews go")
    args = parser.parse_args(argv)

    try:
        fill_path_options(args)
    except WorkspaceError as error:
        print(f"save-sound: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    try:
        specs = load_config(args.config)
        spec, start_state = plan_save(args, specs)
    except (ConfigError, UsageError) as error:
        print(f"save-sound: {error}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    preview_dir = args.output_dir / PREVIEWS_DIR
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview = preview_dir / f"{args.name}.wav"
    pending_preview = preview_dir / f".{args.name}.preview.wav"
    job: SaveJob = {
        "plugin_path": spec.path,
        "plugin_name": spec.plugin_name,
        "state": start_state,
        "preview_path": str(pending_preview),
    }

    print(f"Opening {spec.name}. Choose the sound, then close the window to save it.")
    try:
        # A preview left by a killed run must not pass for this run's.
        pending_preview.unlink(missing_ok=True)
        state, peak_dbfs = run_save_worker(job)
        # The preview is disposable, so it moves first. Writing the sound is the last step that
        # can fail, so a failed or interrupted run leaves the sound file unchanged.
        pending_preview.replace(preview)
        path = write_sound(
            args.sounds_dir, SavedSound(args.name, spec.name, args.description, state)
        )
    except (WorkerCrashedError, WorkerTimedOutError, OSError) as error:
        print(f"save-sound: not saved: {error}", file=sys.stderr)
        return EXIT_SOME_FAILED
    except KeyboardInterrupt:
        print("save-sound: interrupted, not saved", file=sys.stderr)
        return EXIT_SOME_FAILED
    finally:
        pending_preview.unlink(missing_ok=True)

    print(f"sound: {path}")
    print(f"preview: {preview} (peak {peak_dbfs:.1f} dBFS)")
    if peak_dbfs < SILENCE_DBFS:
        print(
            f"save-sound: warning: the preview is below {SILENCE_DBFS:g} dBFS; "
            "the sound is saved, but check it",
            file=sys.stderr,
        )
    return EXIT_ALL_PASS


def save_in_worker(job: SaveJob) -> SaveFailure | SaveSuccess:
    """Show the plugin's window, then render a preview from the state it leaves."""
    # Imported here so the parent never loads plugin code.
    from pedalboard import AudioUnitPlugin, VST3Plugin, load_plugin

    def load(state: bytes | None) -> VST3Plugin | AudioUnitPlugin:
        # `load_plugin` is annotated as returning `ExternalPlugin`, but it returns one of these.
        plugin = cast(
            "VST3Plugin | AudioUnitPlugin",
            load_plugin(
                job["plugin_path"],
                plugin_name=job["plugin_name"],
                initialization_timeout=INITIALIZATION_TIMEOUT_S,
            ),
        )
        if state is not None:
            plugin.raw_state = state
        return plugin

    start = None if job["state"] is None else base64.b64decode(job["state"])
    # Any exception from third-party plugin code is the result being reported.
    try:
        plugin = load(start)
    except Exception as error:  # noqa: BLE001
        return {"error": f"cannot load the plugin: {error}"}

    plugin.show_editor()  # Blocks until the owner closes the window.
    state = capture_state(plugin)

    # A newly loaded plugin proves the saved state restores before the sound is kept.
    try:
        plugin = load(state)
    except Exception as error:  # noqa: BLE001
        return {"error": f"cannot restore the saved state: {error}"}
    # The same reset as `render_stem`, so the preview sounds as a song will.
    # Pedalboard's stubs take a `list` of its message union, and `list` is invariant.
    messages: list[MIDIMessageLike] = [*performance_messages(check_pattern())]
    rendered = plugin(
        messages,
        duration=render_duration_s(CHECK_SAMPLES),
        sample_rate=SAMPLE_RATE,
        num_channels=NUM_CHANNELS,
        reset=True,
    )
    audio = make_audio(rendered, CHECK_SAMPLES)
    if isinstance(audio, AudioProblem):
        return {"error": f"preview: {audio.message}"}
    WavAudioFiles().write(Path(job["preview_path"]), audio)
    return {"state": encode_state(state), "peak_dbfs": measure_peak_dbfs(audio.samples)}


def worker_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="poser.sounds")
    parser.add_argument("job_path", type=Path)
    parser.add_argument("result_path", type=Path)
    args = parser.parse_args(argv)

    # The result goes to a file because plugins may print to stdout.
    job = parse_save_job(json.loads(args.job_path.read_text()))  # From `run_save_worker`.
    result = save_in_worker(job)
    args.result_path.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(worker_main(sys.argv[1:]))
