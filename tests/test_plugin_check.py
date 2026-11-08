"""Tests that use synthetic audio and substitute worker scripts, with no real plugins."""

import json
import sys
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import pytest

from poser import plugin_check
from poser.plugin_check import (
    PluginResult,
    PluginSpec,
    Status,
    analyze,
    parse_worker_result,
    run_worker,
)

NUM_SAMPLES = round(plugin_check.DURATION_S * plugin_check.SAMPLE_RATE)
PASS_RESULT: dict[str, object] = {
    "status": "pass",
    "is_instrument": True,
    "peak_dbfs": -6.0,
    "note_peaks_dbfs": [-6.0, -6.0, -6.0, -6.0],
    "clipped": False,
    "repeatable": True,
    "max_difference_dbfs": -120.0,
}


def note_window(index: int) -> slice:
    start_s = plugin_check.FIRST_NOTE_S + index * plugin_check.NOTE_SPACING_S
    start = round(start_s * plugin_check.SAMPLE_RATE)
    stop = round((start_s + plugin_check.NOTE_LENGTH_S) * plugin_check.SAMPLE_RATE)
    return slice(start, stop)


def tone_render(level: float = 0.5, skip_note: int | None = None) -> np.ndarray:
    render = np.zeros((plugin_check.NUM_CHANNELS, NUM_SAMPLES), dtype=np.float32)
    for index in range(len(plugin_check.NOTES)):
        if index != skip_note:
            render[..., note_window(index)] = level
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


def test_tone_in_all_note_windows_passes() -> None:
    result = analyze(tone_render(), tone_render())
    assert result.status is Status.PASS
    assert result.peak_dbfs == pytest.approx(-6.02, abs=0.01)
    assert result.note_peaks_dbfs == pytest.approx([-6.02] * 4, abs=0.01)


def test_all_zero_render_is_silent() -> None:
    silence = np.zeros((plugin_check.NUM_CHANNELS, NUM_SAMPLES), dtype=np.float32)
    result = analyze(silence, silence)
    assert result.status is Status.SILENT
    assert result.peak_dbfs == plugin_check.FLOOR_DBFS


def test_silent_second_render_is_silent() -> None:
    silence = np.zeros((plugin_check.NUM_CHANNELS, NUM_SAMPLES), dtype=np.float32)
    assert analyze(tone_render(), silence).status is Status.SILENT


def test_empty_note_window_is_missing_notes() -> None:
    render = tone_render(skip_note=2)
    result = analyze(render, render)
    assert result.status is Status.MISSING_NOTES
    assert result.note_peaks_dbfs[2] == plugin_check.FLOOR_DBFS
    assert result.note_peaks_dbfs[1] == pytest.approx(-6.02, abs=0.01)


def test_nan_render_is_non_finite() -> None:
    render = tone_render()
    render[0, 100] = np.nan
    result = analyze(render, render)
    assert result.status is Status.NON_FINITE
    assert np.isfinite(result.peak_dbfs)
    assert np.isfinite(result.max_difference_dbfs)


def test_infinity_in_second_render_is_non_finite() -> None:
    second = tone_render()
    second[1, 200] = np.inf
    result = analyze(tone_render(), second)
    assert result.status is Status.NON_FINITE
    assert np.isfinite(result.peak_dbfs)


def test_identical_renders_are_repeatable() -> None:
    result = analyze(tone_render(), tone_render())
    assert result.repeatable is True
    assert result.max_difference_dbfs == plugin_check.FLOOR_DBFS


def test_differing_renders_are_reported_without_failing() -> None:
    second = tone_render()
    second[0, note_window(0).start] += 0.1
    result = analyze(tone_render(), second)
    assert result.repeatable is False
    assert result.max_difference_dbfs == pytest.approx(-20.0, abs=0.01)
    assert result.status is Status.PASS


def test_full_scale_sample_is_clipped_without_failing() -> None:
    render = tone_render()
    render[0, note_window(1).start] = 1.0
    result = analyze(render, render)
    assert result.clipped is True
    assert result.status is Status.PASS


def test_clean_render_is_not_clipped() -> None:
    assert analyze(tone_render(), tone_render()).clipped is False


def test_worker_past_timeout_is_timed_out(tmp_path: Path) -> None:
    command = write_script(tmp_path, "import time\ntime.sleep(30)\n")
    assert run_worker(command, timeout_s=0.5)["status"] is Status.TIMED_OUT


def test_worker_exiting_nonzero_is_crashed(tmp_path: Path) -> None:
    command = write_script(tmp_path, "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
    result = run_worker(command, timeout_s=30)
    assert result["status"] is Status.CRASHED
    detail = result.get("detail")
    assert detail is not None
    assert "exit code 3" in detail
    assert "boom" in detail


def test_worker_exiting_zero_without_result_is_crashed(tmp_path: Path) -> None:
    command = write_script(tmp_path, "pass\n")
    assert run_worker(command, timeout_s=30)["status"] is Status.CRASHED


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
    assert run_worker(command, timeout_s=30)["status"] is Status.CRASHED


MALFORMED = {"status": Status.CRASHED, "detail": "worker wrote a malformed result"}
UNKNOWN_STATUS = {"status": Status.CRASHED, "detail": "worker wrote an unknown status"}


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
    assert parse_worker_result(PASS_RESULT) == {**PASS_RESULT, "status": Status.PASS}
    assert parse_worker_result({"status": "load_failed", "detail": None}) == {
        "status": Status.LOAD_FAILED,
        "detail": None,
    }


def test_worker_result_is_returned(tmp_path: Path) -> None:
    result = run_worker(write_result_script(tmp_path), timeout_s=30)
    assert result == {**PASS_RESULT, "status": Status.PASS}


def test_stdout_noise_does_not_affect_result(tmp_path: Path) -> None:
    command = write_script(
        tmp_path,
        "import json, sys\n"
        "print('plugin banner')\n"
        f"open(sys.argv[-1], 'w').write(json.dumps({PASS_RESULT!r}))\n",
    )
    assert run_worker(command, timeout_s=30)["status"] is Status.PASS


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
        ("One", Status.CRASHED),
        ("Two", Status.PASS),
    ]


def test_real_worker_reports_missing_bundle_as_load_failed(tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.vst3")
    specs = [PluginSpec("Missing", missing), PluginSpec("Named", missing, "Inner")]

    results = plugin_check.run_check(specs, timeout_s=60)

    assert [result.status for result in results] == [Status.LOAD_FAILED, Status.LOAD_FAILED]
    assert all(result.detail for result in results)


class FakeInstrument:
    def __init__(self, *, is_instrument: bool = True, audio: np.ndarray | None = None) -> None:
        self.is_instrument = is_instrument
        self.audio = tone_render() if audio is None else audio

    def __call__(self, messages: list[object], **kwargs: object) -> np.ndarray:
        assert kwargs["sample_rate"] == plugin_check.SAMPLE_RATE
        assert kwargs["duration"] == plugin_check.DURATION_S
        return self.audio


def test_worker_result_loads_a_fresh_instance_per_render(monkeypatch: pytest.MonkeyPatch) -> None:
    loads = []

    def fake_load(path: str, **kwargs: object) -> FakeInstrument:
        loads.append(kwargs["initialization_timeout"])
        return FakeInstrument()

    monkeypatch.setattr("pedalboard.load_plugin", fake_load)

    result = plugin_check._worker_result("/a.vst3", None)

    assert len(loads) == 2
    assert result["status"] is Status.PASS
    assert result.get("is_instrument") is True


def test_worker_result_does_not_render_an_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load(path: str, **kwargs: object) -> FakeInstrument:
        return FakeInstrument(is_instrument=False, audio=np.zeros((1, 1)))

    monkeypatch.setattr("pedalboard.load_plugin", fake_load)

    result = plugin_check._worker_result("/a.vst3", None)

    assert result == {"status": Status.NOT_INSTRUMENT, "is_instrument": False}


def test_worker_result_rejects_an_unexpected_render_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load(path: str, **kwargs: object) -> FakeInstrument:
        return FakeInstrument(audio=np.zeros((NUM_SAMPLES, plugin_check.NUM_CHANNELS)))

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

    assert plugin_check.main(["--config", str(config)]) == plugin_check.EXIT_ALL_PASS
    output = capsys.readouterr().out
    assert "pass" in output
    assert "a" in output


def test_main_exits_one_when_a_plugin_does_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = write_result_script(tmp_path, {**PASS_RESULT, "status": "silent"})
    monkeypatch.setattr(plugin_check, "worker_command", lambda spec: command)
    config = write_config(tmp_path, ["a"])

    assert plugin_check.main(["--config", str(config)]) == plugin_check.EXIT_SOME_FAILED


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

    assert plugin_check.main(["--config", str(config)]) == plugin_check.EXIT_BAD_CONFIG


def test_default_config_does_not_depend_on_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert [spec.name for spec in plugin_check.load_config(plugin_check.DEFAULT_CONFIG)] == [
        "Serum",
        "Primer",
    ]


def test_table_shows_each_plugin_and_detail() -> None:
    table = plugin_check.format_table(
        [
            PluginResult("Serum", "/s", Status.PASS, True, -3.0, [], False, False, -20.0),
            PluginResult("Primer", "/p", Status.LOAD_FAILED, detail="no bundle"),
        ]
    )
    lines = table.splitlines()
    assert lines[1].split() == ["Serum", "pass", "yes", "-3.0", "no"]
    assert lines[2].split() == ["Primer", "load_failed", "-", "-", "-"]
    assert lines[3] == "Primer: no bundle"


def test_pattern_has_four_notes_within_duration() -> None:
    # mido sets message fields dynamically, so they are read through `dict()`.
    messages = [m.dict() for m in plugin_check.note_pattern()]
    assert [m["type"] for m in messages].count("note_on") == 4
    assert [m["type"] for m in messages].count("note_off") == 4
    assert all(0 <= m["time"] <= plugin_check.DURATION_S for m in messages)
    assert [m["note"] for m in messages if m["type"] == "note_on"] == [48, 60, 72, 84]
    assert [m["time"] for m in messages if m["type"] == "note_on"] == [0.5, 1.5, 2.5, 3.5]
    assert [m["time"] for m in messages if m["type"] == "note_off"] == [1.0, 2.0, 3.0, 4.0]


def test_parent_module_does_not_import_pedalboard_at_module_level() -> None:
    source = Path(plugin_check.__file__).read_text()
    assert "\nimport pedalboard" not in source
    assert "\nfrom pedalboard" not in source
