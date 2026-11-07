"""Tests for saved sounds with substitute worker scripts and hand-written files, no real plugins."""

import base64
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from helpers import fxp_bytes

from vsti_composer import sounds
from vsti_composer.plugin_check import EXIT_ALL_PASS, EXIT_BAD_CONFIG, EXIT_SOME_FAILED
from vsti_composer.worker import run_isolated

WORKER_STATE = b"state-from-the-window"


def write_plugins(tmp_path: Path, presets: Path | None = None) -> Path:
    path = tmp_path / "plugins.toml"
    serum_presets = f"presets = {str(presets)!r}\n" if presets else ""
    path.write_text(
        f'[[plugin]]\nname = "Serum"\npath = "/serum.vst3"\n{serum_presets}\n'
        '[[plugin]]\nname = "Primer"\npath = "/primer.vst3"\n'
    )
    return path


def encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def write_sound_file(tmp_path: Path, name: str, content: Any) -> Path:
    sounds_dir = tmp_path / "sounds"
    sounds_dir.mkdir(exist_ok=True)
    path = sounds_dir / f"{name}.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def use_save_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str = "pass") -> None:
    """Replace the save worker with a script that logs its job and acts as `action`."""
    script = tmp_path / "save_worker.py"
    script.write_text(
        f"""
import base64, json, sys
from pathlib import Path
job = json.loads(Path(sys.argv[-2]).read_text())
with open({str(tmp_path / "jobs.log")!r}, "a") as log:
    log.write(json.dumps(job) + "\\n")
action = {action!r}
if action == "crash":
    sys.exit(3)
if action == "error":
    result = dict(error="cannot load the plugin: broken")
elif action == "malformed":
    result = dict(state="!!", peak_dbfs=-6.0)
else:
    if action != "no-preview":
        Path(job["preview_path"]).write_text("preview audio")
    peak = -120.0 if action == "silent" else -6.0
    result = dict(state=base64.b64encode({WORKER_STATE!r}).decode(), peak_dbfs=peak)
Path(sys.argv[-1]).write_text(json.dumps(result))
"""
    )
    command = [sys.executable, str(script)]
    monkeypatch.setattr(sounds, "save_worker_command", lambda job_path: [*command, str(job_path)])


def save(tmp_path: Path, *args: str) -> int:
    argv = [
        *args,
        "--config",
        str(write_plugins(tmp_path)),
        "--sounds-dir",
        str(tmp_path / "sounds"),
        "--output-dir",
        str(tmp_path / "renders"),
    ]
    try:
        return sounds.save_main(argv)
    except SystemExit as error:  # argparse refuses some requests itself, with code 2.
        return int(error.code or 0)


def jobs(tmp_path: Path) -> list[dict[str, Any]]:
    log = tmp_path / "jobs.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def test_saved_file_has_three_keys_and_the_worker_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_save_worker(tmp_path, monkeypatch)

    code = save(tmp_path, "warm-pad", "--plugin", "Serum", "--description", "warm pad")

    assert code == EXIT_ALL_PASS
    data = json.loads((tmp_path / "sounds" / "warm-pad.json").read_text())
    assert data.keys() == {"plugin", "description", "state"}
    assert data["plugin"] == "Serum"
    assert data["description"] == "warm pad"
    assert base64.b64decode(data["state"]) == WORKER_STATE
    assert [path.name for path in (tmp_path / "sounds").iterdir()] == ["warm-pad.json"]
    assert jobs(tmp_path)[0]["plugin_path"] == "/serum.vst3"
    assert jobs(tmp_path)[0]["state"] is None


def test_saved_sound_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_save_worker(tmp_path, monkeypatch)
    save(tmp_path, "warm-pad", "--plugin", "Serum", "--description", "warm pad")

    path = tmp_path / "sounds" / "warm-pad.json"
    sound = sounds.read_sound(path, sounds.load_config(tmp_path / "plugins.toml"))

    assert sound == sounds.SavedSound("warm-pad", "Serum", "warm pad", encode(WORKER_STATE))


@pytest.mark.parametrize(
    "args",
    [
        ["bad name", "--plugin", "Serum", "--description", "x"],
        ["../escape", "--plugin", "Serum", "--description", "x"],
        ["_pad", "--plugin", "Serum", "--description", "x"],
        ["serum", "--plugin", "Serum", "--description", "x"],
        ["existing", "--plugin", "Serum", "--description", "x"],
        ["Existing", "--plugin", "Serum", "--description", "x", "--replace"],
        ["new", "--plugin", "Massive", "--description", "x"],
        ["new", "--from", "missing", "--description", "x"],
        ["new", "--from", "broken", "--description", "x"],
        ["new", "--plugin", "Serum", "--from", "existing", "--description", "x"],
        ["new", "--description", "x"],
        ["new", "--plugin", "Serum"],
        ["new", "--plugin", "Serum", "--description", "  "],
    ],
    ids=[
        "invalid-name",
        "path-name",
        "leading-underscore",
        "plugin-name-other-case",
        "existing-without-replace",
        "existing-other-case",
        "unknown-plugin",
        "unknown-from",
        "unusable-from",
        "both-starts",
        "no-start",
        "no-description",
        "blank-description",
    ],
)
def test_refused_requests_exit_two_and_write_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
) -> None:
    use_save_worker(tmp_path, monkeypatch)
    existing = write_sound_file(
        tmp_path, "existing", {"plugin": "Serum", "description": "old", "state": encode(b"old")}
    )
    write_sound_file(tmp_path, "broken", "{not json")
    before = sorted((path.name, path.read_bytes()) for path in (tmp_path / "sounds").iterdir())

    code = save(tmp_path, *args)

    assert code == EXIT_BAD_CONFIG
    assert capsys.readouterr().err
    assert jobs(tmp_path) == []
    after = sorted((path.name, path.read_bytes()) for path in (tmp_path / "sounds").iterdir())
    assert after == before
    assert existing.exists()
    assert not (tmp_path / "renders" / sounds.PREVIEWS_DIR).exists() or not any(
        (tmp_path / "renders" / sounds.PREVIEWS_DIR).iterdir()
    )


def test_replace_overwrites_an_existing_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_save_worker(tmp_path, monkeypatch)
    path = write_sound_file(
        tmp_path, "pad", {"plugin": "Serum", "description": "old", "state": encode(b"old")}
    )

    code = save(tmp_path, "pad", "--plugin", "Serum", "--description", "new", "--replace")

    assert code == EXIT_ALL_PASS
    data = json.loads(path.read_text())
    assert (data["description"], base64.b64decode(data["state"])) == ("new", WORKER_STATE)


def test_from_passes_the_source_plugin_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_save_worker(tmp_path, monkeypatch)
    write_sound_file(
        tmp_path, "pad", {"plugin": "Primer", "description": "pad", "state": encode(b"source")}
    )

    code = save(tmp_path, "pad-2", "--from", "pad", "--description", "brighter pad")

    assert code == EXIT_ALL_PASS
    job = jobs(tmp_path)[0]
    assert job["plugin_path"] == "/primer.vst3"
    assert base64.b64decode(job["state"]) == b"source"
    saved = json.loads((tmp_path / "sounds" / "pad-2.json").read_text())
    assert saved["plugin"] == "Primer"


def test_from_the_same_name_with_replace_revises_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_save_worker(tmp_path, monkeypatch)
    write_sound_file(
        tmp_path, "pad", {"plugin": "Serum", "description": "pad", "state": encode(b"source")}
    )

    assert save(tmp_path, "pad", "--from", "pad", "--description", "pad", "--replace") == 0
    assert base64.b64decode(jobs(tmp_path)[0]["state"]) == b"source"


@pytest.mark.parametrize("action", ["crash", "error", "malformed", "no-preview"])
def test_failed_worker_exits_one_and_leaves_the_sound_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    use_save_worker(tmp_path, monkeypatch, action)
    path = write_sound_file(
        tmp_path, "pad", {"plugin": "Serum", "description": "old", "state": encode(b"old")}
    )
    before = path.read_bytes()

    code = save(tmp_path, "pad", "--plugin", "Serum", "--description", "new", "--replace")

    assert code == EXIT_SOME_FAILED
    assert path.read_bytes() == before
    assert [p.name for p in (tmp_path / "sounds").iterdir()] == ["pad.json"]
    assert list((tmp_path / "renders" / sounds.PREVIEWS_DIR).iterdir()) == []


def test_interrupt_exits_one_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(job: dict[str, Any]) -> tuple[str, float]:
        Path(job["preview_path"]).write_text("partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(sounds, "run_save_worker", interrupted)

    code = save(tmp_path, "pad", "--plugin", "Serum", "--description", "pad")

    assert code == EXIT_SOME_FAILED
    assert not (tmp_path / "sounds").exists()
    assert list((tmp_path / "renders" / sounds.PREVIEWS_DIR).iterdir()) == []


def test_preview_is_written_to_the_renders_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_save_worker(tmp_path, monkeypatch)

    save(tmp_path, "pad", "--plugin", "Serum", "--description", "pad")

    preview = tmp_path / "renders" / sounds.PREVIEWS_DIR / "pad.wav"
    assert preview.read_text() == "preview audio"
    assert [p.name for p in preview.parent.iterdir()] == ["pad.wav"]
    out = capsys.readouterr().out
    assert str(tmp_path / "sounds" / "pad.json") in out
    assert str(preview) in out
    assert "-6.0 dBFS" in out


def test_silent_preview_still_saves_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_save_worker(tmp_path, monkeypatch, "silent")

    code = save(tmp_path, "pad", "--plugin", "Serum", "--description", "pad")

    assert code == EXIT_ALL_PASS
    assert (tmp_path / "sounds" / "pad.json").exists()
    assert "warning" in capsys.readouterr().err


def list_sounds(tmp_path: Path, *args: str, presets: Path | None = None) -> int:
    config = write_plugins(tmp_path, presets)
    return sounds.list_main(
        [*args, "--config", str(config), "--sounds-dir", str(tmp_path / "sounds")]
    )


def write_presets(tmp_path: Path) -> Path:
    root = tmp_path / "presets"
    for relative in ["Pads/Wide", "Bass/Deep", "Bass/Acid", "Init", "Bass (Hard)/Grit"]:
        path = root / f"{relative}.fxp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fxp_bytes(b"chunk"))
    return root


def test_list_shows_presets_after_saved_sounds_with_folder_descriptions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_sound_file(
        tmp_path, "zap", {"plugin": "Serum", "description": "laser pluck", "state": encode(b"s")}
    )

    assert list_sounds(tmp_path, presets=write_presets(tmp_path)) == EXIT_ALL_PASS
    lines = capsys.readouterr().out.splitlines()
    # Columns are padded with at least two spaces, and names may hold single spaces.
    assert [re.split(r"\s{2,}", line.strip()) for line in lines[1:]] == [
        ["Serum", "Serum", "default patch"],
        ["Primer", "Primer", "default patch"],
        ["zap", "Serum", "laser pluck"],
        ["Serum:Bass (Hard)/Grit", "Serum", "Bass (Hard) preset"],
        ["Serum:Bass/Acid", "Serum", "Bass preset"],
        ["Serum:Bass/Deep", "Serum", "Bass preset"],
        ["Serum:Init", "Serum", "preset"],
        ["Serum:Pads/Wide", "Serum", "Pads preset"],
    ]


def test_list_json_includes_presets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert list_sounds(tmp_path, "--json", presets=write_presets(tmp_path)) == EXIT_ALL_PASS
    entries = json.loads(capsys.readouterr().out)
    assert entries[2] == {
        "name": "Serum:Bass (Hard)/Grit",
        "plugin": "Serum",
        "description": "Bass (Hard) preset",
    }
    assert [entry["name"] for entry in entries[2:]] == [
        "Serum:Bass (Hard)/Grit",
        "Serum:Bass/Acid",
        "Serum:Bass/Deep",
        "Serum:Init",
        "Serum:Pads/Wide",
    ]


def test_list_reports_a_missing_presets_directory_and_lists_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_sound_file(
        tmp_path, "zap", {"plugin": "Serum", "description": "laser pluck", "state": encode(b"s")}
    )

    code = list_sounds(tmp_path, "--json", presets=tmp_path / "missing")

    assert code == EXIT_SOME_FAILED
    captured = capsys.readouterr()
    assert [entry["name"] for entry in json.loads(captured.out)] == ["Serum", "Primer", "zap"]
    assert "does not exist" in captured.err


def test_list_shows_plugin_defaults_then_saved_sounds_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, description in [("zap", "laser pluck"), ("bass-1", "sub bass")]:
        write_sound_file(
            tmp_path, name, {"plugin": "Serum", "description": description, "state": encode(b"s")}
        )

    code = list_sounds(tmp_path)

    assert code == EXIT_ALL_PASS
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["NAME", "PLUGIN", "DESCRIPTION"]
    assert [line.split(maxsplit=2) for line in lines[1:]] == [
        ["Serum", "Serum", "default patch"],
        ["Primer", "Primer", "default patch"],
        ["bass-1", "Serum", "sub bass"],
        ["zap", "Serum", "laser pluck"],
    ]


def test_list_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_sound_file(
        tmp_path, "zap", {"plugin": "Serum", "description": "laser pluck", "state": encode(b"s")}
    )

    assert list_sounds(tmp_path, "--json") == EXIT_ALL_PASS
    assert json.loads(capsys.readouterr().out) == [
        {"name": "Serum", "plugin": "Serum", "description": "default patch"},
        {"name": "Primer", "plugin": "Primer", "description": "default patch"},
        {"name": "zap", "plugin": "Serum", "description": "laser pluck"},
    ]


def test_list_reports_unusable_files_and_still_lists_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_sound_file(
        tmp_path, "zap", {"plugin": "Serum", "description": "laser pluck", "state": encode(b"s")}
    )
    write_sound_file(tmp_path, "broken", "{not json")
    write_sound_file(tmp_path, "bad name", {"plugin": "Serum", "description": "x", "state": "AA=="})

    code = list_sounds(tmp_path, "--json")

    assert code == EXIT_SOME_FAILED
    captured = capsys.readouterr()
    assert [entry["name"] for entry in json.loads(captured.out)] == ["Serum", "Primer", "zap"]
    assert "broken.json" in captured.err
    assert "bad name.json" in captured.err


def test_list_without_a_sounds_directory_lists_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert list_sounds(tmp_path, "--json") == EXIT_ALL_PASS
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_list_exits_two_for_a_bad_plugin_list(tmp_path: Path) -> None:
    assert sounds.list_main(["--config", str(tmp_path / "missing.toml")]) == EXIT_BAD_CONFIG


def test_run_isolated_without_timeout_runs_to_completion(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    script.write_text(
        "import sys, time\nfrom pathlib import Path\ntime.sleep(0.5)\n"
        "Path(sys.argv[-1]).write_text('{\"done\": true}')\n"
    )

    assert run_isolated([sys.executable, str(script)], None) == {"done": True}


def test_default_sounds_directory_is_in_the_project() -> None:
    assert sounds.SOUNDS_DIR == sounds.PROJECT_DIR / "sounds"


class EditorFakePlugin:
    """Records the order of window, state, and render calls across loads."""

    def __init__(self, events: list[Any], audio: Any) -> None:
        self.events = events
        self.audio = audio
        self.state = b"default"

    @property
    def raw_state(self) -> bytes:
        return self.state

    @raw_state.setter
    def raw_state(self, value: bytes) -> None:
        self.events.append(("set_state", value))
        self.state = value

    def show_editor(self) -> None:
        self.events.append(("show_editor", self.state))
        self.state = WORKER_STATE  # The owner chose a sound in the window.

    def __call__(self, messages: list[Any], **kwargs: Any) -> Any:
        self.events.append(("render", self.state))
        return self.audio


def test_save_worker_renders_the_preview_from_a_newly_loaded_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import numpy as np
    from pedalboard.io import ReadableAudioFile

    events: list[Any] = []
    audio = np.zeros((2, 48000), dtype=np.float32)
    audio[:, 100] = 0.5

    def load(*args: Any, **kwargs: Any) -> EditorFakePlugin:
        events.append(("load", None))
        return EditorFakePlugin(events, audio)

    monkeypatch.setattr("pedalboard.load_plugin", load)
    preview = tmp_path / ".pad.preview.wav"
    job = {
        "plugin_path": "/serum.vst3",
        "plugin_name": None,
        "state": encode(b"start"),
        "preview_path": str(preview),
    }

    result = sounds.save_in_worker(job)

    assert result == {"state": encode(WORKER_STATE), "peak_dbfs": pytest.approx(-6.02, abs=0.01)}
    assert events == [
        ("load", None),
        ("set_state", b"start"),
        ("show_editor", b"start"),
        ("render", WORKER_STATE),
        ("load", None),
        ("set_state", WORKER_STATE),
        ("render", WORKER_STATE),
    ]
    with ReadableAudioFile(str(preview)) as written:  # ty: ignore[no-matching-overload]
        assert (written.samplerate, written.num_channels) == (48000, 2)
    assert [path.name for path in tmp_path.iterdir()] == [".pad.preview.wav"]


def test_save_worker_reports_a_load_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise ImportError("no such plugin")

    monkeypatch.setattr("pedalboard.load_plugin", fail)
    job = {"plugin_path": "/x.vst3", "plugin_name": None, "state": None, "preview_path": "/none"}

    assert sounds.save_in_worker(job) == {"error": "cannot load the plugin: no such plugin"}


def test_failed_preview_move_leaves_the_sound_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_save_worker(tmp_path, monkeypatch)
    path = write_sound_file(
        tmp_path, "pad", {"plugin": "Serum", "description": "old", "state": encode(b"old")}
    )
    before = path.read_bytes()
    (tmp_path / "renders" / sounds.PREVIEWS_DIR / "pad.wav").mkdir(parents=True)

    code = save(tmp_path, "pad", "--plugin", "Serum", "--description", "new", "--replace")

    assert code == EXIT_SOME_FAILED
    assert path.read_bytes() == before


def test_a_leftover_preview_does_not_pass_for_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_save_worker(tmp_path, monkeypatch, "no-preview")
    preview_dir = tmp_path / "renders" / sounds.PREVIEWS_DIR
    preview_dir.mkdir(parents=True)
    (preview_dir / ".pad.preview.wav").write_text("from a killed run")

    code = save(tmp_path, "pad", "--plugin", "Serum", "--description", "pad")

    assert code == EXIT_SOME_FAILED
    assert not (tmp_path / "sounds").exists()


def test_failed_sound_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(sounds.os, "replace", fail)
    sound = sounds.SavedSound("pad", "Serum", "pad", encode(b"s"))

    with pytest.raises(OSError, match="disk full"):
        sounds.write_sound(tmp_path / "sounds", sound)

    assert list((tmp_path / "sounds").iterdir()) == []


def test_previews_are_apart_from_song_stem_directories() -> None:
    assert not sounds.NAME_PATTERN.fullmatch(sounds.PREVIEWS_DIR)
    assert sounds.PREVIEWS_DIR.startswith(".")
