"""Advice selection is configuration, not a rendering option or prose heuristic."""

import dataclasses
import importlib
import re
import subprocess
from pathlib import Path

import pytest
from fakes import FakeInstrument, FakeRenderStore, FakeSound, NpyAudioFiles
from helpers import REPO_DIR, SCAFFOLD_DIR, SURGE_DIR

from poser import compose
from poser.adapters.song_formats import SONG_FORMATS
from poser.cli import init_workspace
from poser.cli.guide import main, workspace_guide
from poser.compose import Composition, load, parse_config
from poser.core.cache import Stamp
from poser.core.graph import Graph
from poser.core.guide import GENERATED, GuideBlock, compose_guide, observed_advice
from poser.core.loading import load_song
from poser.core.names import NodeName, SongId
from poser.core.ports import AdapterContext, AdviceProvider, ConfigError, Guided
from poser.core.rendering import TrackResult, render_song
from poser.core.song import Song
from poser.core.statuses import StemStatus

CONFIG = '[instruments.synth]\nadapter = "pedalboard"\npath = "/missing/plugin.vst3"\n'


def composition(tmp_path: Path, setting: str = "") -> Composition:
    config = parse_config(CONFIG + setting, tmp_path / "poser.toml")
    return Composition(config, AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders"))


@pytest.mark.parametrize("setting", ["", "\n[guidance]\n", "\n[guidance]\nadvice = true\n"])
def test_default_and_explicit_on(tmp_path: Path, setting: str) -> None:
    config = composition(tmp_path, setting).config
    assert config.guidance.advice is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(config.guidance, "advice", False)  # noqa: B010 - exercise frozen runtime guard


@pytest.mark.parametrize(
    ("setting", "diagnostic"),
    [
        ("guidance = false\n", "guidance: must be a table"),
        ('[guidance]\nadvice = "off"\n', "guidance.advice: must be a boolean"),
        ("[guidance]\nadvice = 0\n", "guidance.advice: must be a boolean"),
        ("[guidance]\nunknown = true\n", "guidance.unknown: unknown key"),
    ],
)
def test_invalid_guidance(tmp_path: Path, setting: str, diagnostic: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(diagnostic)):
        parse_config(setting + "\n" + CONFIG, tmp_path / "poser.toml")


def test_inline_advice_is_reserved(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"instruments\.synth\.advice: reserved"):
        composition(tmp_path, 'advice = "coaching"\n')


def test_instrument_notes_are_required_not_adapter_options(tmp_path: Path) -> None:
    configured = composition(
        tmp_path, 'guide = "Required operational sentinel"\n\n[guidance]\nadvice = false\n'
    )
    assert "Required operational sentinel" in workspace_guide(configured)
    assert "guide" not in configured.config.instruments[0].spec.options
    assert "advice" not in configured.config.instruments[0].spec.options


def test_filter_precedes_markdown_validation_and_preserves_legacy_reference() -> None:
    text = compose_guide(
        "# Required", [], ["## Legacy\nRequired", GuideBlock("SENTINEL", True)], advice=False
    )
    assert "SENTINEL" not in text
    assert "## Legacy\nRequired" in text
    assert text.startswith(GENERATED + "\nAdvice: off\n")
    assert observed_advice(text) is False
    assert observed_advice("# Foreign\nAdvice: off") is None


def test_modes_keep_reference_and_adapter_options_identical(tmp_path: Path) -> None:
    on = composition(tmp_path)
    off = composition(tmp_path, "\n[guidance]\nadvice = false\n")
    assert on.config.instruments == off.config.instruments
    assert on.config.adapters == off.config.adapters
    required = workspace_guide(off)
    guided = workspace_guide(on)
    assert observed_advice(guided) is True
    # All required content remains byte-identical aside from the visible mode marker.
    stripped = guided.replace("Advice: on", "Advice: off")
    for block in [GuideBlock((SCAFFOLD_DIR / "advice.md").read_text(), True), *on.guide_blocks()]:
        if block.advice:
            stripped = stripped.replace("\n\n" + block.text.strip(), "")
    assert stripped == required


def test_on_off_on_regeneration_and_check(tmp_path: Path) -> None:
    path = tmp_path / "poser.toml"
    original = None
    for enabled in (True, False, True):
        path.write_text(CONFIG + f"\n[guidance]\nadvice = {str(enabled).lower()}\n")
        assert main(["--config", str(path), "--check"]) == 1
        assert main(["--config", str(path)]) == 0
        assert main(["--config", str(path), "--check"]) == 0
        text = (tmp_path / "POSER.md").read_text()
        assert observed_advice(text) is enabled
        if original is None:
            original = text
    assert text == original


@pytest.mark.parametrize("enabled", [True, False])
def test_examples_load_in_both_modes(tmp_path: Path, enabled: bool) -> None:
    text = workspace_guide(
        composition(tmp_path, f"\n[guidance]\nadvice = {str(enabled).lower()}\n")
    )
    examples = re.findall(r"```python\n(.*?)```", text, re.DOTALL)
    assert examples
    for i, body in enumerate(examples):
        path = tmp_path / f"example{i}.py"
        path.write_text(body)
        song = load_song(path, SONG_FORMATS).source
        assert isinstance(song, Song | Graph)
        sources = song.tracks if isinstance(song, Song) else song.track_nodes
        assert sources[0].sound == "synth"


def test_guidance_toggle_preserves_rendered_bytes_keys_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real render pipeline uses the same song and store across on/off/on compositions."""
    store = FakeRenderStore(tmp_path / "renders")
    audio = NpyAudioFiles()
    original_loader = compose.load_object

    def load_fake(target: str) -> object:
        if target == "guidance_fakes:instrument":
            return lambda name, options, context: FakeInstrument(name, {None: FakeSound()})
        if target == "guidance_fakes:store":
            return lambda options, context, suffix: store
        if target == "guidance_fakes:audio":
            return lambda options, context: audio
        return original_loader(target)

    monkeypatch.setattr(compose, "load_object", load_fake)
    path = tmp_path / "song.yaml"
    path.write_text(
        "bpm: 120\ntracks:\n  - name: voice\n    sound: synth\n    notes:\n"
        "      - {beat: 0, pitch: 60, length: 1, velocity: 100}\n"
    )
    song = load_song(path, SONG_FORMATS)
    song_id = SongId("song.yaml")
    node = NodeName("voice")
    song_store = store.song(song_id)
    baseline_stamp = None
    baseline_bytes = None
    baseline_result = None
    baseline_request = None
    for enabled in (True, False, True):
        config = parse_config(
            '[instruments.synth]\nadapter = "guidance_fakes:instrument"\n'
            '[adapters]\nrender_store = "guidance_fakes:store"\n'
            'audio_files = "guidance_fakes:audio"\n'
            f"[guidance]\nadvice = {str(enabled).lower()}\n",
            tmp_path / "poser.toml",
        )
        configured = Composition(
            config, AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
        )
        assert observed_advice(workspace_guide(configured)) is enabled
        instrument = next(iter(configured.instruments().values()))
        assert isinstance(instrument, FakeInstrument)

        def render(
            configured: Composition = configured, *, force: bool = False
        ) -> list[TrackResult]:
            return render_song(
                song,
                configured.render_store(),
                song_id,
                configured.instruments(),
                configured.audio_files(),
                effects=configured.effects(),
                timeout_s=10,
                force=force,
            )

        result = render()
        stamp = song_store.read_stamp(node)
        assert isinstance(stamp, Stamp)
        assert stamp.status is StemStatus.RENDERED
        stem_bytes = song_store.stem_path(node).read_bytes()
        if baseline_stamp is None:
            baseline_stamp, baseline_bytes, baseline_result = stamp, stem_bytes, result
            assert len(instrument.renders) == 1
            baseline_request = instrument.renders[0]
        else:
            # Toggling alone must hit the earlier mode's cache, not invoke the instrument.
            assert instrument.renders == []
            assert stamp.track_key == baseline_stamp.track_key
            assert stamp.sound_key == baseline_stamp.sound_key
            assert stamp == baseline_stamp
            assert stem_bytes == baseline_bytes
            assert all(item.cached for item in result)
            assert [dataclasses.replace(item, cached=False) for item in result] == baseline_result
        instrument.renders.clear()
        assert render(force=True) == baseline_result
        assert len(instrument.renders) == 1
        request = instrument.renders[0]
        assert baseline_request is not None
        assert request.sound == baseline_request.sound
        assert request.performance == baseline_request.performance
        assert song_store.read_stamp(node) == baseline_stamp
        assert song_store.stem_path(node).read_bytes() == baseline_bytes


def test_scsynth_guide_is_required_package_data_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = REPO_DIR / "packages" / "poser-scsynth" / "src"
    monkeypatch.syspath_prepend(str(source))
    adapter = importlib.import_module("poser_scsynth.instrument")
    package_guide = (source / "poser_scsynth" / "guide.md").read_bytes()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("guide generation must not scan sources, render, or start a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(adapter, "scan_source", forbidden)
    required_blocks = []
    for enabled in (True, False):
        config = parse_config(
            '[instruments.sc]\nadapter = "poser_scsynth.instrument:create"\n'
            'scsynth = "/missing/scsynth"\n'
            '[instruments.sc.sources]\nexternal = "/missing/sounds"\n'
            f"[guidance]\nadvice = {str(enabled).lower()}\n",
            tmp_path / "poser.toml",
        )
        configured = Composition(
            config, AdapterContext(tmp_path, tmp_path / "sounds", tmp_path / "renders")
        )
        instrument = next(iter(configured.instruments().values()))
        assert isinstance(instrument, Guided)
        assert not isinstance(instrument, AdviceProvider)
        for method in ("sounds", "resolve", "render"):
            monkeypatch.setattr(type(instrument), method, forbidden)
        text = package_guide.decode("utf-8")
        assert instrument.guide == text
        classified = [block for block in configured.guide_blocks() if block.text == text]
        assert classified == [GuideBlock(text, advice=False)]
        required_blocks.append(classified)
        rendered = workspace_guide(configured)
        assert rendered.count(text.strip()) == 1
        assert observed_advice(rendered) is enabled
    assert required_blocks[0] == required_blocks[1]


def test_regeneration_preserves_owner_files_and_operational_notes(tmp_path: Path) -> None:
    owner_files = {
        "AGENTS.md": b"# Owner instructions\nKeep the commissioned melody intact.\n",
        "CLAUDE.md": b"@AGENTS.md\nOwner-specific session requirements.\n",
    }
    for name, data in owner_files.items():
        (tmp_path / name).write_bytes(data)
    note = "Controller 1 controls brightness; preserve the owner calibration."
    config_path = tmp_path / "poser.toml"
    for enabled in (True, False, True):
        config_bytes = (
            CONFIG + f'guide = "{note}"\n[guidance]\nadvice = {str(enabled).lower()}\n'
        ).encode()
        config_path.write_bytes(config_bytes)
        assert main(["--config", str(config_path)]) == 0
        assert main(["--config", str(config_path), "--check"]) == 0
        assert config_path.read_bytes() == config_bytes
        assert note in (tmp_path / "POSER.md").read_text()
        assert {name: (tmp_path / name).read_bytes() for name in owner_files} == owner_files


def test_fresh_off_initialization_complete_context_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = tmp_path / "Surge XT.vst3"
    plugin.mkdir()
    presets = tmp_path / "factory"
    presets.mkdir()
    target = tmp_path / "studio"

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("fresh guide initialization must not launch plugins or processes")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert (
        init_workspace.main(
            [
                str(target),
                "--setup",
                "surge",
                "--plugin-path",
                str(plugin),
                "--presets-path",
                str(presets),
                "--advice",
                "off",
            ]
        )
        == 0
    )
    inventory = {
        path.relative_to(target).as_posix(): path.read_text()
        for path in target.rglob("*")
        if path.is_file() and ".poser-init" not in path.relative_to(target).parts
    }
    assert set(inventory) == {
        "AGENTS.md",
        "CLAUDE.md",
        "POSER.md",
        "poser.toml",
        "songs/demo.yaml",
        ".gitignore",
    }
    configured = load(target / "poser.toml", target / "sounds", target / "renders")
    assert configured.config.guidance.advice is False
    assert inventory["POSER.md"] == workspace_guide(configured)
    assert observed_advice(inventory["POSER.md"]) is False
    for note in configured.instrument_notes():
        assert note.guide
        assert note.guide in inventory["POSER.md"]
    assert inventory["AGENTS.md"] == (SCAFFOLD_DIR / "agents.md").read_text()
    assert inventory["CLAUDE.md"] == "@AGENTS.md\n"
    assert inventory["songs/demo.yaml"] == (SURGE_DIR / "demo.yaml").read_text()
    assert main(["--config", str(target / "poser.toml"), "--check"]) == 0
