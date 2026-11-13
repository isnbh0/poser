"""Workspaces for command tests, whose `poser.toml` selects test adapters by import path.

The adapters live in a module written into the workspace, under a name no other test uses, so
each test imports its own copy and its module state starts empty.
"""

import importlib
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
import yaml

# The test adapters. Options shape each one; an unknown option is a ConfigError, as for poser's.
ADAPTERS = """
from fakes import FakeInstrument, FakeMixer, FakeSection, FakeSound, SavedSounds

from poser.core.names import Identifier
from poser.core.ports import ConfigError, RenderFailure, SoundListing
from poser.core.statuses import RenderFailureStatus

SAVED = {}  # Saved sounds by instrument name, kept between commands as files would be.
BUILT = {}  # Every instrument built, by name.
SETUP = {}  # A function applied to each instrument built, by name.
MIXES = []  # The frames of every mix the test mixer made.


class ListingInstrument(FakeInstrument):
    def __init__(self, *args, problems=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._problems = tuple(problems)

    def sounds(self):
        listing = super().sounds()
        return SoundListing(listing.entries, listing.problems + self._problems)


def instrument(name, options, context):
    options = dict(options)
    fail = options.pop("fail", None)
    failure = None if fail is None else RenderFailure(RenderFailureStatus(fail), f"it {fail}")
    amplitude = options.pop("amplitude", 0.5)
    late = options.pop("late_attempts", 0)
    sounds = {}
    if options.pop("default", True):
        sounds[None] = FakeSound(
            "default sound", "default", amplitude, render_failure=failure, late_attempts=late
        )
    for preset in options.pop("presets", []):
        sounds[preset] = FakeSound(f"{preset} preset", preset, amplitude, render_failure=failure)
    problems = options.pop("problems", [])
    if options:
        raise ConfigError(f"unknown option {min(options)!r}")
    made = ListingInstrument(
        name, sounds, saved=SAVED.setdefault(name, SavedSounds()), problems=problems
    )
    if name in SETUP:
        SETUP[name](made)
    BUILT.setdefault(name, []).append(made)
    return made


class CountingMixer(FakeMixer):
    def mix(self, stems, frames):
        MIXES.append(frames)
        return super().mix(stems, frames)


def mixer(options, context):
    if options:
        raise ConfigError("the test mixer takes no options")
    return CountingMixer()


class PartSection(FakeSection):
    def __init__(self, name, defects, raises_on):
        super().__init__(name, data={"probe": True}, defects=defects)
        self._raises_on = raises_on

    def _result(self, part, request):
        if part == self._raises_on:
            raise RuntimeError(f"no {part} for {self.name}")
        return super()._result(part, request)


def section(options, context):
    options = dict(options)
    made = PartSection(
        options.pop("name", "probe"), options.pop("defects", []), options.pop("raises_on", None)
    )
    if options:
        raise ConfigError(f"unknown option {min(options)!r}")
    return made


class RaisingFramework:
    name = Identifier("broken")
    guide = "## Broken framework"

    def read(self, request):
        raise RuntimeError("cannot read")


def raising_framework(options, context):
    return RaisingFramework()


class RaisingVocabulary:
    name = Identifier("chords")
    guide = "## Broken vocabulary"

    def parse(self, text):
        raise RuntimeError("cannot parse")


def raising_vocabulary(options, context):
    return RaisingVocabulary()
"""

INSTRUMENTS = """
[instruments.synth]
adapter = "{module}:instrument"
presets = ["lead", "pad"]
"""


@dataclass(frozen=True)
class CommandWorkspace:
    root: Path
    module: ModuleType  # The test adapters, as the workspace's commands import them.

    @property
    def config(self) -> Path:
        return self.root / "poser.toml"

    @property
    def renders(self) -> Path:
        return self.root / "renders"

    def write_config(self, text: str) -> None:
        self.config.write_text(textwrap.dedent(text).format(module=self.module.__name__))


def make_workspace(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    instruments: str = INSTRUMENTS,
    adapters: str = "",
) -> CommandWorkspace:
    """Make `root` a workspace whose instruments, and any adapters named, are the test ones.

    `instruments` and `adapters` are TOML in which `{module}` names the test adapters' module.
    """
    root.mkdir(parents=True, exist_ok=True)
    name = f"cli_adapters_{uuid.uuid4().hex}"
    (root / f"{name}.py").write_text(ADAPTERS)
    monkeypatch.syspath_prepend(str(root))
    workspace = CommandWorkspace(root.resolve(), importlib.import_module(name))
    workspace.write_config(instruments + adapters)
    return workspace


NOTE = {"beat": 0, "pitch": 60, "length": 1, "velocity": 100}


def write_song(path: Path, *tracks: tuple[str, str], velocity: int = 100) -> Path:
    """Write a YAML song with a one-note track for each `(name, sound)`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    note = {**NOTE, "velocity": velocity}
    data = {"bpm": 120, "tracks": [{"name": n, "sound": s, "notes": [note]} for n, s in tracks]}
    path.write_text(yaml.safe_dump(data))
    return path


def write_script_song(path: Path, sound: str, marker: Path | None = None) -> Path:
    """Write a script song; when `marker` is given, running the script creates that file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    touch = "" if marker is None else f"    Path({str(marker)!r}).touch()\n"
    path.write_text(
        "from pathlib import Path\n\n"
        "from poser import Note, Song, Track\n\n\n"
        "def build() -> Song:\n"
        f"{touch}"
        f"    return Song(120, (Track('lead', {sound!r}, (Note(0, 60, 1, 100),)),))\n"
    )
    return path
