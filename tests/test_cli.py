"""The commands, run through `main` in workspaces whose `poser.toml` selects test adapters."""

import json
from pathlib import Path

import pytest
from helpers import fxp_bytes
from workspaces import CommandWorkspace, make_workspace, write_script_song, write_song

from poser.adapters.wav import WavAudioFiles
from poser.cli import analyze, check, guide, init_workspace, render, sounds
from poser.cli.common import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    EXIT_SONG_BUSY,
    align_rows,
)
from poser.compose import load
from poser.core.audio import SILENCE_DBFS
from poser.core.guide import GENERATED
from poser.core.names import SongId
from poser.core.ports import CaptureFailure

SONG = "songs/tune.yaml"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CommandWorkspace:
    return make_workspace(tmp_path, monkeypatch)


def stem(workspace: CommandWorkspace, track: str, song: str = SONG) -> Path:
    return workspace.renders / song / "stems" / f"{track}.wav"


def out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


# render-song


def test_render_prints_the_table(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_song(workspace.root / SONG, ("lead", "synth:lead"), ("bass", "synth"))

    assert render.main([SONG]) == EXIT_ALL_PASS

    rows = [
        ("TRACK", "STATUS", "CACHED", "PEAK DBFS", "STEM"),
        ("bass", "rendered", "-", "-6.0", str(stem(workspace, "bass"))),
        ("lead", "rendered", "-", "-6.0", str(stem(workspace, "lead"))),
    ]
    assert out(capsys) == "\n".join(align_rows(rows)) + "\n"
    assert stem(workspace, "lead").is_file()


def test_render_json_holds_rendered_cached_and_failed_tracks(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    song = workspace.root / SONG
    write_song(song, ("lead", "synth:lead"))
    render.main([SONG])
    capsys.readouterr()
    write_song(song, ("lead", "synth:lead"), ("pad", "synth:pad"), ("odd", "synth:nope"))

    assert render.main([SONG, "--json"]) == EXIT_SOME_FAILED

    assert json.loads(out(capsys)) == [
        {
            "track": "lead",
            "status": "rendered",
            "peak_dbfs": pytest.approx(-6.0, abs=0.1),
            "stem": str(stem(workspace, "lead")),
            "detail": None,
            "cached": True,
        },
        {
            "track": "odd",
            "status": "unknown_sound",
            "peak_dbfs": None,
            "stem": None,
            "detail": "no sound named 'nope'",
            "cached": False,
        },
        {
            "track": "pad",
            "status": "rendered",
            "peak_dbfs": pytest.approx(-6.0, abs=0.1),
            "stem": str(stem(workspace, "pad")),
            "detail": None,
            "cached": False,
        },
    ]


def test_result_data_has_exactly_the_documented_keys(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_song(workspace.root / SONG, ("lead", "synth:lead"))

    render.main([SONG, "--json"])

    (data,) = json.loads(out(capsys))
    assert list(data) == ["track", "status", "peak_dbfs", "stem", "detail", "cached"]


def test_render_caches_then_force_renders_again(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_song(workspace.root / SONG, ("lead", "synth:lead"))
    render.main([SONG])
    capsys.readouterr()

    assert render.main([SONG]) == EXIT_ALL_PASS
    assert out(capsys).splitlines()[1].split()[:3] == ["lead", "rendered", "yes"]
    render.main([SONG, "--json"])
    assert [result["cached"] for result in json.loads(out(capsys))] == [True]
    render.main([SONG, "--json", "--force"])
    assert [result["cached"] for result in json.loads(out(capsys))] == [False]
    assert len(workspace.module.BUILT["synth"][-1].renders) == 1


def test_render_names_an_unknown_instrument(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_song(workspace.root / SONG, ("lead", "keys:piano"))

    assert render.main([SONG]) == EXIT_SOME_FAILED

    assert out(capsys).splitlines()[-1] == "lead: no instrument 'keys' in poser.toml"


def test_render_failure_exits_one_with_its_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instruments = '[instruments.synth]\nadapter = "{module}:instrument"\nfail = "crashed"\n'
    make_workspace(tmp_path, monkeypatch, instruments)
    write_song(tmp_path / SONG, ("lead", "synth"))

    assert render.main([SONG]) == EXIT_SOME_FAILED

    lines = out(capsys).splitlines()
    assert lines[1].split() == ["lead", "crashed", "-", "-", "-"]
    assert lines[2] == "lead: it crashed"


def test_song_outside_the_workspace_exits_2_before_its_script_runs(
    workspace: CommandWorkspace, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    marker = elsewhere / "ran"
    song = write_script_song(elsewhere / "x.py", "synth", marker)

    for main in (render.main, analyze.main):
        assert main([str(song)]) == EXIT_BAD_CONFIG
    assert not marker.exists()


def test_song_overlapping_another_exits_2_before_its_script_runs(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_song(workspace.root / "songs" / "a.yaml", ("lead", "synth"))
    render.main(["songs/a.yaml"])
    (workspace.root / "songs" / "a.yaml").unlink()
    marker = workspace.root / "ran"
    write_script_song(workspace.root / "songs" / "a.yaml" / "b.py", "synth", marker)
    capsys.readouterr()

    assert render.main(["songs/a.yaml/b.py"]) == EXIT_BAD_CONFIG

    assert "would be inside the directory of the song 'songs/a.yaml'" in capsys.readouterr().err
    assert not marker.exists()


@pytest.mark.parametrize("prog", ["render-song", "analyze-song"])
def test_busy_song_exits_3(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str], prog: str
) -> None:
    write_song(workspace.root / SONG, ("lead", "synth"))
    store = load(workspace.config, workspace.root / "sounds", workspace.renders).render_store()
    main = render.main if prog == "render-song" else analyze.main

    with store.song(SongId(SONG)).lock():
        assert main([SONG]) == EXIT_SONG_BUSY

    assert capsys.readouterr().err.startswith(f"{prog}: ")
    assert not stem(workspace, "lead").exists()


def test_missing_song_exits_2(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    for main, prog in ((render.main, "render-song"), (analyze.main, "analyze-song")):
        assert main(["songs/missing.yaml"]) == EXIT_BAD_CONFIG
        assert capsys.readouterr().err.startswith(f"{prog}: ")
    assert not workspace.renders.exists()


def test_unreadable_song_exits_2(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    song = workspace.root / SONG
    song.parent.mkdir()
    song.write_text("bpm: [")

    for main, prog in ((render.main, "render-song"), (analyze.main, "analyze-song")):
        assert main([SONG]) == EXIT_BAD_CONFIG
        assert capsys.readouterr().err.startswith(f"{prog}: ")


def test_bad_config_exits_2_before_any_work(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = workspace.root / "ran"
    write_script_song(workspace.root / "songs" / "x.py", "synth", marker)
    workspace.write_config('[instruments.synth]\nadapter = "{module}:instrument"\nloud = true\n')

    assert render.main(["songs/x.py"]) == EXIT_BAD_CONFIG

    assert capsys.readouterr().err == (
        f"render-song: {workspace.config}: instruments.synth: unknown option 'loud'\n"
    )
    assert not marker.exists()


# analyze-song


ANALYSIS_ADAPTERS = """
[adapters]
mixer = "{module}:mixer"
sections = [{{ use = "{module}:section", name = "probe" }}]
"""


def test_analyze_prints_the_report_and_keeps_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = make_workspace(tmp_path, monkeypatch, adapters=ANALYSIS_ADAPTERS)
    write_song(tmp_path / SONG, ("lead", "synth:lead"))
    render.main([SONG])
    capsys.readouterr()

    assert analyze.main([SONG, "--json"]) == EXIT_ALL_PASS

    report = json.loads(out(capsys))
    assert report["song"] == "tune"
    assert report["run"] == 1
    (node,) = report["nodes"]
    assert node["sections"] == {"probe": {"version": 1, "data": {"probe": True}, "defects": []}}
    assert report["mix"]["sections"]["probe"]["defects"] == []
    assert workspace.module.MIXES
    assert (workspace.renders / SONG / "history" / "1").is_dir()


def test_analyze_table_has_a_mix_row(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_song(workspace.root / SONG, ("lead", "synth:lead"))
    render.main([SONG])
    capsys.readouterr()

    assert analyze.main([SONG]) == EXIT_ALL_PASS

    lines = out(capsys).splitlines()
    assert lines[0].split() == [
        "NAME",
        "STATUS",
        "GAIN",
        "DB",
        "PEAK",
        "DBFS",
        "LUFS",
        "CLIPPED",
        "DEFECTS",
    ]
    assert lines[1].split()[:3] == ["lead", "analyzed", "0.0"]
    assert lines[2].split()[:3] == ["[mix]", "written", "-"]
    assert len(lines) == 3


def test_missing_stem_fails_the_analysis(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    write_song(workspace.root / SONG, ("lead", "synth:lead"))

    assert analyze.main([SONG]) == EXIT_SOME_FAILED

    lines = out(capsys).splitlines()
    assert lines[1].split()[:2] == ["lead", "missing_stem"]
    assert lines[2].split() == ["[mix]", "not_written", "-", "-", "-", "-", "-"]
    assert len(lines) == 3


def test_a_section_raising_on_the_mix_fails_the_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adapters = """
[adapters]
sections = [{{ use = "{module}:section", name = "probe", raises_on = "mix" }}]
"""
    make_workspace(tmp_path, monkeypatch, adapters=adapters)
    write_song(tmp_path / SONG, ("lead", "synth:lead"))
    render.main([SONG])
    capsys.readouterr()

    assert analyze.main([SONG]) == EXIT_SOME_FAILED

    lines = out(capsys).splitlines()
    assert lines[1].split()[-1] == "-"
    assert lines[2].split()[-1] == "-"
    assert lines[3:] == ["mix: section probe: RuntimeError: no mix for probe"]


def test_a_section_defect_on_a_track_is_a_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adapters = """
[adapters]
sections = [{{ use = "{module}:section", name = "probe", defects = ["harsh", "dull"] }}]
"""
    make_workspace(tmp_path, monkeypatch, adapters=adapters)
    write_song(tmp_path / SONG, ("lead", "synth:lead"))
    render.main([SONG])
    capsys.readouterr()

    assert analyze.main([SONG]) == EXIT_SOME_FAILED

    lines = out(capsys).splitlines()
    assert lines[3:] == ["lead: section probe: harsh, dull", "mix: section probe: harsh, dull"]


# check-instruments


def test_check_prints_the_table_and_exits_1_when_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instruments = """
[instruments.synth]
adapter = "{module}:instrument"

[instruments.broken]
adapter = "{module}:instrument"
fail = "timed_out"
"""
    make_workspace(tmp_path, monkeypatch, instruments)

    assert check.main([]) == EXIT_SOME_FAILED

    rows = [
        ("INSTRUMENT", "ADAPTER", "STATUS", "PEAK DBFS", "REPEATABLE"),
        ("synth", "fake", "pass", "-6.0", "yes"),
        ("broken", "fake", "timed_out", "-", "-"),
    ]
    assert out(capsys) == "\n".join([*align_rows(rows), "broken: it timed_out"]) + "\n"


def test_check_json_and_a_pass(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert check.main(["--json"]) == EXIT_ALL_PASS

    (result,) = json.loads(out(capsys))
    assert result["name"] == "synth"
    assert result["adapter"] == "fake"
    assert result["status"] == "pass"
    assert result["repeatable"] is True
    assert set(result) == {
        "name",
        "adapter",
        "status",
        "peak_dbfs",
        "note_peaks_dbfs",
        "clipped",
        "repeatable",
        "max_difference_dbfs",
        "detail",
    }


# list-sounds


def test_list_prints_each_instrument_s_sounds(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sounds.list_main([]) == EXIT_ALL_PASS

    rows = [
        ("SOUND", "DESCRIPTION"),
        ("synth", "default sound"),
        ("synth:lead", "lead preset"),
        ("synth:pad", "pad preset"),
    ]
    assert out(capsys) == "\n".join(align_rows(rows)) + "\n"


def test_list_json(workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]) -> None:
    assert sounds.list_main(["--json"]) == EXIT_ALL_PASS

    assert json.loads(out(capsys)) == [
        {"sound": "synth", "description": "default sound"},
        {"sound": "synth:lead", "description": "lead preset"},
        {"sound": "synth:pad", "description": "pad preset"},
    ]


PEDALBOARD_CONFIG = """
[instruments.serum]
adapter = "pedalboard"
path = "/Library/Audio/Plug-Ins/VST3/Serum.vst3"

[instruments.serum.sources]
presets = "presets"
"""


def pedalboard_workspace(tmp_path: Path) -> None:
    """A workspace on the pedalboard adapter, whose sounds scan needs no plugin."""
    (tmp_path / "presets").mkdir()
    (tmp_path / "poser.toml").write_text(PEDALBOARD_CONFIG.lstrip())


def test_list_notes_unclaimed_source_files_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pedalboard_workspace(tmp_path)
    (tmp_path / "presets" / "One.fxp").write_bytes(fxp_bytes(b"x"))
    (tmp_path / "presets" / "Notes.txt").write_bytes(b"stray")

    assert sounds.list_main([]) == EXIT_ALL_PASS

    rows = [
        ("SOUND", "DESCRIPTION"),
        ("serum", "default patch"),
        ("serum:presets/One", "preset"),
    ]
    note = "list-sounds: note: serum: source 'presets' holds 1 .txt poser cannot read"
    captured = capsys.readouterr()
    assert captured.out == "\n".join(align_rows(rows)) + "\n"
    assert note in captured.err

    assert sounds.list_main(["--json"]) == EXIT_ALL_PASS

    captured = capsys.readouterr()
    assert json.loads(captured.out) == [
        {"sound": "serum", "description": "default patch"},
        {"sound": "serum:presets/One", "description": "preset"},
    ]
    assert note in captured.err


def test_list_notes_do_not_hide_a_problem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pedalboard_workspace(tmp_path)
    (tmp_path / "presets" / "One.fxp").write_bytes(fxp_bytes(b"x"))
    (tmp_path / "presets" / "Notes.txt").write_bytes(b"stray")
    (tmp_path / "sounds" / "serum").mkdir(parents=True)
    (tmp_path / "sounds" / "serum" / "bad.json").write_bytes(b'{"description": "x"}')

    assert sounds.list_main([]) == EXIT_SOME_FAILED

    captured = capsys.readouterr()
    assert "SOUND" in captured.out
    assert (
        "list-sounds: serum: saved/bad: must hold exactly the keys ['description', 'state']"
        in captured.err
    )
    assert (
        "list-sounds: note: serum: source 'presets' holds 1 .txt poser cannot read" in captured.err
    )


def test_list_reports_one_instrument_s_problem_and_the_old_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instruments = """
[instruments.synth]
adapter = "{module}:instrument"

[instruments.keys]
adapter = "{module}:instrument"
problems = ["presets: cannot read"]
"""
    make_workspace(tmp_path, monkeypatch, instruments)
    (tmp_path / "sounds").mkdir()
    (tmp_path / "sounds" / "warm.json").write_text("{}")

    assert sounds.list_main([]) == EXIT_SOME_FAILED

    captured = capsys.readouterr()
    assert [line.split()[0] for line in captured.out.splitlines()] == ["SOUND", "synth", "keys"]
    old = tmp_path.resolve() / "sounds" / "warm.json"
    assert captured.err.splitlines() == [
        "list-sounds: keys: presets: cannot read",
        (
            f"list-sounds: {old}: saved sounds now live in sounds/<instrument>/; move it to "
            'sounds/<instrument>/<name>.json and delete its "plugin" key'
        ),
    ]


# save-sound


OPENING = "Opening synth. Choose the sound, then close the window to save it.\n"


def save(*args: str) -> int:
    return sounds.save_main(list(args))


@pytest.mark.parametrize(
    "args",
    [
        ["synth", "bad name", "--description", "x"],
        ["synth", "_pad", "--description", "x"],
        ["synth", "new", "--description", " "],
        ["synth", "new", "--description", "x", "--from", "synth:"],
        ["keys", "new", "--description", "x"],
        ["synth", "lead", "--description", "x"],
        ["synth", "new", "--description", "x", "--from", "other:lead"],
        ["synth", "new", "--description", "x", "--from", "synth:nope"],
    ],
    ids=[
        "invalid-name",
        "leading-underscore",
        "blank-description",
        "bad-from",
        "unknown-instrument",
        "built-in-name",
        "from-another-instrument",
        "unknown-from",
    ],
)
def test_save_refusals_exit_2_before_capturing(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    assert save(*args) == EXIT_BAD_CONFIG

    assert capsys.readouterr().err.startswith("save-sound: ")
    assert not [i for i in workspace.module.BUILT.get("synth", []) if i.captures]
    assert not (workspace.renders / ".sound-previews").exists()


def test_save_refuses_an_unknown_instrument_by_name(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    save("keys", "new", "--description", "x")

    assert capsys.readouterr().err == "save-sound: no instrument 'keys' in poser.toml\n"


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        (["bad synth", "new"], "instrument: Identifier must match"),
        (["synth", "bad name"], "name: Identifier must match"),
        (["synth", "new", "--from", "synth:"], "--from: "),
    ],
    ids=["instrument", "name", "from"],
)
def test_save_names_the_argument_it_cannot_parse(
    workspace: CommandWorkspace,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    problem: str,
) -> None:
    assert save(*args, "--description", "x") == EXIT_BAD_CONFIG

    captured = capsys.readouterr()
    assert captured.err.startswith(f"save-sound: {problem}")
    assert captured.out == ""


def test_save_prints_the_sound_and_its_preview(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert save("synth", "warm", "--description", "warm pad", "--from", "synth:pad") == 0

    preview = workspace.renders / ".sound-previews" / "synth" / "warm.wav"
    captured = capsys.readouterr()
    assert captured.out == (f"{OPENING}sound: synth:warm\npreview: {preview} (peak -12.0 dBFS)\n")
    assert captured.err == ""
    assert preview.is_file()
    assert sounds.list_main(["--json"]) == EXIT_ALL_PASS
    assert {"sound": "synth:warm", "description": "warm pad"} in json.loads(out(capsys))


def test_replace_saves_over_an_existing_sound(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    save("synth", "warm", "--description", "first")
    assert save("synth", "warm", "--description", "x") == EXIT_BAD_CONFIG
    capsys.readouterr()

    assert save("synth", "warm", "--description", "second", "--replace") == EXIT_ALL_PASS
    assert out(capsys).startswith(f"{OPENING}sound: synth:warm\n")

    sounds.list_main(["--json"])
    assert {"sound": "synth:warm", "description": "second"} in json.loads(out(capsys))


def test_quiet_preview_saves_with_a_warning(
    workspace: CommandWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def quiet(instrument: object) -> None:
        monkeypatch.setattr(instrument, "capture_bad_output", None)

    workspace.module.SETUP["synth"] = quiet
    monkeypatch.setattr("poser.core.capturing.measure_peak_dbfs", lambda _samples: -80.0)

    assert save("synth", "hush", "--description", "x") == EXIT_ALL_PASS

    assert capsys.readouterr().err == (
        f"save-sound: warning: the preview is below {SILENCE_DBFS:g} dBFS; "
        "the sound is saved, but check it\n"
    )


def test_failed_capture_is_not_saved(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(instrument: object) -> None:
        instrument.capture_failure = CaptureFailure("the window crashed")  # ty: ignore[unresolved-attribute]

    workspace.module.SETUP["synth"] = failing

    assert save("synth", "warm", "--description", "x") == EXIT_SOME_FAILED

    captured = capsys.readouterr()
    assert captured.out == OPENING
    assert captured.err == "save-sound: not saved: the window crashed\n"


def test_interrupt_during_capture_is_not_saved(
    workspace: CommandWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupting(instrument: object) -> None:
        instrument.capture_raises = KeyboardInterrupt()  # ty: ignore[unresolved-attribute]

    workspace.module.SETUP["synth"] = interrupting

    assert save("synth", "warm", "--description", "x") == EXIT_SOME_FAILED

    assert capsys.readouterr().err == "save-sound: interrupted, not saved\n"
    sounds.list_main(["--json"])
    assert "synth:warm" not in out(capsys)


@pytest.mark.parametrize(
    ("raised", "error"), [(KeyboardInterrupt(), "interrupted"), (OSError("disk full"), "disk full")]
)
def test_preview_failure_after_saving(
    workspace: CommandWorkspace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raised: BaseException,
    error: str,
) -> None:
    def write(*_args: object) -> None:
        raise raised

    monkeypatch.setattr(WavAudioFiles, "write", write)

    assert save("synth", "warm", "--description", "x") == EXIT_SOME_FAILED

    assert capsys.readouterr().err == (
        f"save-sound: saved synth:warm, but its preview could not be written: {error}\n"
    )


# update-guide


@pytest.fixture
def initialized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    surge = tmp_path / "surge.vst3"
    surge.mkdir()
    assert (
        init_workspace.main([str(tmp_path), "--setup", "surge", "--plugin-path", str(surge)]) == 0
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_update_guide_check_follows_poser_toml(
    initialized: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    guide_file = initialized / "AGENTS.md"
    assert guide.main(["--check"]) == EXIT_ALL_PASS
    config = initialized / "poser.toml"
    config.write_text(config.read_text().replace("factory patches", "patches"))
    before = guide_file.read_text()
    capsys.readouterr()

    assert guide.main(["--check"]) == EXIT_SOME_FAILED
    assert capsys.readouterr().err == (
        "update-guide: AGENTS.md is out of date; run uv run update-guide\n"
    )
    assert guide_file.read_text() == before

    assert guide.main([]) == EXIT_ALL_PASS
    assert out(capsys) == f"{guide_file}\n"
    assert "Surge XT patches are available" in guide_file.read_text()
    assert guide.main(["--check"]) == EXIT_ALL_PASS
    assert sorted(path.name for path in initialized.iterdir() if path.name.startswith(".")) == [
        ".gitignore"
    ]


def test_update_guide_without_a_workspace_or_with_a_bad_config_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert guide.main([]) == EXIT_BAD_CONFIG
    assert capsys.readouterr().err.startswith("update-guide: no poser.toml in ")

    (tmp_path / "poser.toml").write_text("[instruments]\n")
    assert guide.main(["--check"]) == EXIT_BAD_CONFIG
    assert capsys.readouterr().err == (
        f"update-guide: {tmp_path.resolve() / 'poser.toml'}: instruments: "
        "must name at least one instrument\n"
    )


@pytest.mark.parametrize("inside", [True, False], ids=["inside-a-workspace", "outside"])
def test_update_guide_config_writes_beside_that_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: bool
) -> None:
    plugin = tmp_path / "surge.vst3"
    plugin.mkdir()
    args = ["--setup", "surge", "--plugin-path", str(plugin)]
    other = tmp_path / "other"
    assert init_workspace.main([str(other), *args]) == 0
    (other / "AGENTS.md").write_text(f"{GENERATED}\nold body\n")
    here = tmp_path / "here"
    here.mkdir()
    if inside:
        assert init_workspace.main([str(here), *args]) == 0
    monkeypatch.chdir(here)
    mine = here / "AGENTS.md"
    before = mine.read_text() if inside else None
    config = ["--config", str(other / "poser.toml")]

    assert guide.main([*config, "--check"]) == EXIT_SOME_FAILED
    assert guide.main(config) == EXIT_ALL_PASS
    assert guide.main([*config, "--check"]) == EXIT_ALL_PASS

    assert (other / "AGENTS.md").read_text().startswith("<!-- ")
    assert (mine.read_text() if inside else None) == before
    assert inside or not mine.exists()


@pytest.mark.parametrize(
    "text", ["# Developing poser\n", "", f"stale\n{GENERATED}\n"], ids=["prose", "empty", "later"]
)
def test_update_guide_refuses_to_replace_a_guide_it_did_not_write(
    initialized: Path, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    guide_file = initialized / "AGENTS.md"
    guide_file.write_text(text)
    problem = (
        f"update-guide: {guide_file} was not written by update-guide; move it away to replace it\n"
    )

    assert guide.main([]) == EXIT_BAD_CONFIG
    assert capsys.readouterr().err == problem
    assert guide.main(["--check"]) == EXIT_SOME_FAILED
    assert capsys.readouterr().err == problem

    assert guide_file.read_text() == text
    guide_file.unlink()
    assert guide.main([]) == EXIT_ALL_PASS
    assert guide_file.read_text().startswith(f"{GENERATED}\n")


def test_update_guide_writes_through_a_symlink_and_keeps_the_mode(
    initialized: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    link = initialized / "AGENTS.md"
    target = initialized / "docs" / "guide.md"
    target.parent.mkdir()
    target.write_text(f"{GENERATED}\nold body\n")
    target.chmod(0o600)
    link.unlink()
    link.symlink_to(Path("docs") / "guide.md")

    assert guide.main([]) == EXIT_ALL_PASS

    assert link.is_symlink()
    assert target.read_text() == link.read_text()
    assert "old body" not in target.read_text()
    assert target.stat().st_mode & 0o777 == 0o600
    assert sorted(path.name for path in target.parent.iterdir()) == ["guide.md"]
    assert guide.main(["--check"]) == EXIT_ALL_PASS


def test_update_guide_keeps_an_existing_guide_s_mode(initialized: Path) -> None:
    guide_file = initialized / "AGENTS.md"
    guide_file.chmod(0o640)
    guide_file.write_text(f"{GENERATED}\nold body\n")

    assert guide.main([]) == EXIT_ALL_PASS

    assert guide_file.stat().st_mode & 0o777 == 0o640
    assert "old body" not in guide_file.read_text()
