"""Tests that use synthetic audio and substitute worker scripts, with no real plugins."""

import json
import sys
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pytest

from poser import plugin_check
from poser.adapters.pedalboard.worker import render_duration_s
from poser.cli.common import EXIT_ALL_PASS, EXIT_BAD_CONFIG, EXIT_SOME_FAILED
from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE
from poser.core.checks import CHECK_NOTES, CHECK_SAMPLES, CheckStatus
from poser.plugin_check import (
    ConfigError,
    PluginResult,
    PluginSpec,
    load_config,
    parse_worker_result,
    run_worker,
)

PASS_RESULT: dict[str, object] = {
    "status": "pass",
    "is_instrument": True,
    "peak_dbfs": -6.0,
    "note_peaks_dbfs": [-6.0, -6.0, -6.0, -6.0],
    "clipped": False,
    "repeatable": True,
    "max_difference_dbfs": -120.0,
}


def tone_render(level: float = 0.5) -> np.ndarray:
    render = np.zeros((NUM_CHANNELS, CHECK_SAMPLES), dtype=np.float32)
    for index in range(len(CHECK_NOTES)):
        start = 24000 + index * 48000
        render[..., start : start + 24000] = level
    return render


def write_script(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "worker.py"
    script.write_text(body)
    return [sys.executable, str(script)]


def write_result_script(tmp_path: Path, result: Mapping[str, object] = PASS_RESULT) -> list[str]:
    return write_script(
        tmp_path,
        f"import json, sys\nopen(sys.argv[-1], 'w').write(json.dumps({result!r}))\n",
    )


def test_worker_past_timeout_is_timed_out(tmp_path: Path) -> None:
    command = write_script(tmp_path, "import time\ntime.sleep(30)\n")
    assert run_worker(command, timeout_s=0.5)["status"] is CheckStatus.TIMED_OUT


def test_worker_exiting_nonzero_is_crashed(tmp_path: Path) -> None:
    command = write_script(tmp_path, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
    result = run_worker(command, timeout_s=30)
    assert result["status"] is CheckStatus.CRASHED
    detail = result.get("detail")
    assert detail is not None
    assert "exit code 3" in detail
    assert "boom" in detail


def test_worker_exiting_zero_without_result_is_crashed(tmp_path: Path) -> None:
    command = write_script(tmp_path, "pass\n")
    assert run_worker(command, timeout_s=30)["status"] is CheckStatus.CRASHED


@pytest.mark.parametrize(
    "written",
    [
        "not json",
        json.dumps(["pass"]),
        json.dumps({"status": "unknown"}),
        json.dumps({"status": "pass", "surprise": 1}),
    ],
)
def test_worker_writing_bad_result_is_crashed(tmp_path: Path, written: str) -> None:
    command = write_script(tmp_path, f"import sys\nopen(sys.argv[-1], 'w').write({written!r})\n")
    assert run_worker(command, timeout_s=30)["status"] is CheckStatus.CRASHED


MALFORMED = {"status": CheckStatus.CRASHED, "detail": "worker wrote a malformed result"}
UNKNOWN_STATUS = {"status": CheckStatus.CRASHED, "detail": "worker wrote an unknown status"}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (["pass"], MALFORMED),
        ({**PASS_RESULT, "surprise": 1}, MALFORMED),
        ({**PASS_RESULT, "peak_dbfs": "loud"}, MALFORMED),
        ({**PASS_RESULT, "clipped": 1}, MALFORMED),
        ({**PASS_RESULT, "note_peaks_dbfs": [-6.0, None]}, MALFORMED),
        ({**PASS_RESULT, "detail": 3}, MALFORMED),
        ({**PASS_RESULT, "status": "unknown"}, UNKNOWN_STATUS),
        ({**PASS_RESULT, "status": 1}, UNKNOWN_STATUS),
        ({"is_instrument": True}, UNKNOWN_STATUS),
    ],
)
def test_worker_result_parser_reports_bad_results_as_crashed(
    payload: object, expected: dict[str, object]
) -> None:
    assert parse_worker_result(payload) == expected


def test_worker_result_parser_keeps_a_valid_result() -> None:
    assert parse_worker_result(PASS_RESULT) == {**PASS_RESULT, "status": CheckStatus.PASS}
    assert parse_worker_result({"status": "load_failed", "detail": None}) == {
        "status": CheckStatus.LOAD_FAILED,
        "detail": None,
    }


def test_worker_result_is_returned(tmp_path: Path) -> None:
    result = run_worker(write_result_script(tmp_path), timeout_s=30)
    assert result == {**PASS_RESULT, "status": CheckStatus.PASS}


def test_stdout_noise_does_not_affect_result(tmp_path: Path) -> None:
    command = write_script(
        tmp_path,
        "import json, sys\n"
        "print('plugin banner')\n"
        f"open(sys.argv[-1], 'w').write(json.dumps({PASS_RESULT!r}))\n",
    )
    assert run_worker(command, timeout_s=30)["status"] is CheckStatus.PASS


def test_failed_first_plugin_still_reports_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = write_script(
        tmp_path,
        "import json, sys\n"
        "if sys.argv[1] == 'bad':\n"
        "    sys.exit(1)\n"
        f"open(sys.argv[-1], 'w').write(json.dumps({PASS_RESULT!r}))\n",
    )
    monkeypatch.setattr(plugin_check, "worker_command", lambda spec: [*base, spec.path])
    specs = [PluginSpec("One", "bad"), PluginSpec("Two", "good")]

    results = plugin_check.run_check(specs)

    assert [(r.name, r.status) for r in results] == [
        ("One", CheckStatus.CRASHED),
        ("Two", CheckStatus.PASS),
    ]


def test_real_worker_reports_missing_bundle_as_load_failed(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.vst3")
    specs = [PluginSpec("Missing", missing), PluginSpec("Named", missing, "Inner")]

    results = plugin_check.run_check(specs, timeout_s=60)

    assert [result.status for result in results] == [
        CheckStatus.LOAD_FAILED,
        CheckStatus.LOAD_FAILED,
    ]
    assert all(result.detail for result in results)


class FakeInstrument:
    def __init__(self, *, is_instrument: bool = True, audio: np.ndarray | None = None) -> None:
        self.is_instrument = is_instrument
        self.audio = tone_render() if audio is None else audio

    def __call__(self, messages: list[object], **kwargs: object) -> np.ndarray:
        assert kwargs["sample_rate"] == SAMPLE_RATE
        assert kwargs["duration"] == render_duration_s(CHECK_SAMPLES)
        return self.audio


def test_worker_result_loads_a_fresh_instance_per_render(monkeypatch: pytest.MonkeyPatch) -> None:
    loads = []

    def fake_load(path: str, **kwargs: object) -> FakeInstrument:
        loads.append(kwargs["initialization_timeout"])
        return FakeInstrument()

    monkeypatch.setattr("pedalboard.load_plugin", fake_load)

    result = plugin_check._worker_result("/a.vst3", None)

    assert len(loads) == 2
    assert result["status"] is CheckStatus.PASS
    assert result.get("is_instrument") is True


def test_worker_result_does_not_render_an_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load(path: str, **kwargs: object) -> FakeInstrument:
        return FakeInstrument(is_instrument=False, audio=np.zeros((1, 1)))

    monkeypatch.setattr("pedalboard.load_plugin", fake_load)

    result = plugin_check._worker_result("/a.vst3", None)

    assert result == {
        "status": CheckStatus.LOAD_FAILED,
        "is_instrument": False,
        "detail": "/a.vst3 is not an instrument",
    }


def test_worker_result_rejects_an_unexpected_render_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load(path: str, **kwargs: object) -> FakeInstrument:
        return FakeInstrument(audio=np.zeros((CHECK_SAMPLES, NUM_CHANNELS)))

    monkeypatch.setattr("pedalboard.load_plugin", fake_load)

    with pytest.raises(RuntimeError, match="render shape"):
        plugin_check._worker_result("/a.vst3", None)


def test_worker_command_passes_plugin_name() -> None:
    command = plugin_check.worker_command(PluginSpec("A", "/a.vst3", "Inner"))
    assert command[-2:] == ["--plugin-name", "Inner"]


def write_config(tmp_path: Path, paths: list[str]) -> Path:
    config = tmp_path / "plugins.toml"
    config.write_text("".join(f'[[plugin]]\nname = "{path}"\npath = "{path}"\n' for path in paths))
    return config


def test_main_exits_zero_when_all_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command = write_result_script(tmp_path)
    monkeypatch.setattr(plugin_check, "worker_command", lambda spec: command)
    config = write_config(tmp_path, ["a", "b"])

    assert plugin_check.main(["--config", str(config)]) == EXIT_ALL_PASS
    output = capsys.readouterr().out
    assert "pass" in output
    assert "a" in output


def test_main_exits_one_when_a_plugin_does_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = write_result_script(tmp_path, {**PASS_RESULT, "status": "silent"})
    monkeypatch.setattr(plugin_check, "worker_command", lambda spec: command)
    config = write_config(tmp_path, ["a"])

    assert plugin_check.main(["--config", str(config)]) == EXIT_SOME_FAILED


def test_main_json_output_is_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    command = write_result_script(tmp_path)
    monkeypatch.setattr(plugin_check, "worker_command", lambda spec: command)
    config = write_config(tmp_path, ["a"])

    plugin_check.main(["--config", str(config), "--json"])

    report = json.loads(capsys.readouterr().out)
    assert report[0]["name"] == "a"
    assert report[0]["status"] == "pass"
    assert report[0]["note_peaks_dbfs"] == PASS_RESULT["note_peaks_dbfs"]


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "not = [valid",
        "",
        "plugin = []",
        '[[plugin]]\nname = "no path"\n',
        '[[plugin]]\nname = "a"\npath = "b"\nplugin_name = 3\n',
    ],
)
def test_main_exits_two_for_bad_config(tmp_path: Path, contents: str | None) -> None:
    config = tmp_path / "plugins.toml"
    if contents is not None:
        config.write_text(contents)

    assert plugin_check.main(["--config", str(config)]) == EXIT_BAD_CONFIG


def test_main_without_config_uses_the_workspace_plugin_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "Missing.vst3"
    (tmp_path / "plugins.toml").write_text(f'[[plugin]]\nname = "Missing"\npath = "{missing}"\n')

    plugin_check.main(["--json"])

    [result] = json.loads(capsys.readouterr().out)
    assert result["name"] == "Missing"
    assert result["status"] == "load_failed"


def test_table_shows_each_plugin_and_detail() -> None:
    table = plugin_check.format_table(
        [
            PluginResult("Serum", "/s", CheckStatus.PASS, True, -3.0, [], False, False, -20.0),
            PluginResult("Primer", "/p", CheckStatus.LOAD_FAILED, detail="no bundle"),
        ]
    )
    lines = table.splitlines()
    assert lines[1].split() == ["Serum", "pass", "yes", "-3.0", "no"]
    assert lines[2].split() == ["Primer", "load_failed", "-", "-", "-"]
    assert lines[3] == "Primer: no bundle"


def test_parent_module_does_not_import_pedalboard_at_module_level() -> None:
    source = Path(plugin_check.__file__).read_text()
    assert "\nimport pedalboard" not in source
    assert "\nfrom pedalboard" not in source


def test_plugin_name_that_is_not_an_identifier_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "plugins.toml"
    config.write_text('[[plugin]]\nname = "Serum Presets"\npath = "/s.vst3"\n')
    with pytest.raises(ConfigError) as caught:
        load_config(config)
    assert str(caught.value) == (
        f"{config}: plugin name 'Serum Presets' must match [A-Za-z0-9][A-Za-z0-9_-]*"
    )


@pytest.mark.parametrize("name", ["Serum", "Serum2", "serum-2"])
def test_identifier_plugin_names_load(tmp_path: Path, name: str) -> None:
    config = tmp_path / "plugins.toml"
    config.write_text(f'[[plugin]]\nname = "{name}"\npath = "/s.vst3"\n')
    assert load_config(config) == [PluginSpec(name, "/s.vst3")]


def write_entry(tmp_path: Path, entry: str) -> Path:
    path = tmp_path / "plugins.toml"
    path.write_text(f"[[plugin]]\n{entry}\n")
    return path


def test_load_config_reads_presets(tmp_path: Path) -> None:
    path = write_entry(tmp_path, 'name = "Serum"\npath = "/s.vst3"\npresets = "/p"')
    assert load_config(path) == [PluginSpec("Serum", "/s.vst3", None, "/p")]


@pytest.mark.parametrize(
    "entry",
    [
        'name = "Serum"\npath = "/s.vst3"\nsamples = "/p"',
        'name = "Ser:um"\npath = "/s.vst3"',
        'name = "Serum"\npath = "/s.vst3"\npresets = ""',
    ],
    ids=["unknown key", "colon in name", "empty presets"],
)
def test_load_config_rejects_bad_entries(tmp_path: Path, entry: str) -> None:
    with pytest.raises(ConfigError):
        load_config(write_entry(tmp_path, entry))
