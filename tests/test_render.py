"""Tests that use a fake plugin or substitute worker scripts, with no real plugins."""

import base64
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from helpers import fxp_bytes
from pedalboard.io import ReadableAudioFile

from vsti_composer import render
from vsti_composer.plugin_check import EXIT_ALL_PASS, EXIT_BAD_CONFIG, EXIT_SOME_FAILED, PluginSpec
from vsti_composer.presets import build_state
from vsti_composer.render import TrackStatus, render_song, render_stem
from vsti_composer.song import Note, Song, Track, parse_song


def make_song(*tracks: Track, bpm: float = 120) -> Song:
    return Song(bpm=bpm, tracks=tracks)


def make_track(name: str = "lead", sound: str = "Serum", *notes: Note) -> Track:
    return Track(name, "pedalboard", sound, notes or (Note(0, 60, 1, 100),))


SPECS = [PluginSpec("Serum", "/serum.vst3")]


def message_times(track: Track, song: Song) -> list[tuple[str, int, float]]:
    messages: list[Any] = render.track_messages(song, track)
    return [(m.type, m.note, m.time) for m in messages]


def test_beats_convert_to_seconds_at_bpm() -> None:
    track = make_track("lead", "Serum", Note(2, 60, 1, 100))
    assert message_times(track, make_song(track)) == [
        ("note_on", 60, 1.0),
        ("note_off", 60, 1.5),
    ]


def test_note_velocity_reaches_the_message() -> None:
    track = make_track("lead", "Serum", Note(0, 60, 1, 37))
    messages: list[Any] = render.track_messages(make_song(track), track)
    assert messages[0].velocity == 37


def test_duration_is_latest_note_end_across_tracks_plus_tail() -> None:
    short = make_track("short", "Serum", Note(0, 60, 1, 100))
    long = make_track("long", "Serum", Note(6, 40, 2, 100), Note(0, 40, 1, 100))
    assert render.song_duration_s(make_song(short, long)) == pytest.approx(4.0 + render.TAIL_S)


def test_every_track_gets_the_same_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command = result_script(tmp_path, "pass")
    monkeypatch.setattr(render, "worker_command", lambda job_path: [*command, str(job_path)])
    short = make_track("short", "Serum", Note(0, 60, 1, 100))
    long = make_track("long", "Serum", Note(6, 40, 2, 100))

    render_song(make_song(short, long), "song", SPECS, tmp_path)

    jobs = [json.loads(line) for line in (tmp_path / "jobs.log").read_text().splitlines()]
    assert [job["track"]["name"] for job in jobs] == ["short", "long"]
    assert {job["duration_s"] for job in jobs} == {6.0}


def test_repeated_pitch_ends_before_it_retriggers() -> None:
    track = make_track("lead", "Serum", Note(1, 60, 1, 100), Note(0, 60, 1, 100))
    assert message_times(track, make_song(track)) == [
        ("note_on", 60, 0.0),
        ("note_off", 60, 0.5),
        ("note_on", 60, 0.5),
        ("note_off", 60, 1.0),
    ]


def test_repeated_pitch_retriggers_despite_float_error_in_beats() -> None:
    assert 0.1 + 0.2 != 0.3
    track = make_track("lead", "Serum", Note(0.1, 60, 0.2, 100), Note(0.3, 60, 0.1, 100))

    types = [message[0] for message in message_times(track, make_song(track))]

    assert types == ["note_on", "note_off", "note_on", "note_off"]


def test_note_shorter_than_a_sample_still_ends_after_it_starts() -> None:
    track = make_track("lead", "Serum", Note(0, 60, 1e-9, 100))

    on, off = message_times(track, make_song(track))

    assert on[0] == "note_on"
    assert off[0] == "note_off"
    assert off[2] > on[2]


class FakePlugin:
    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio

    def __call__(self, messages: list[Any], **kwargs: Any) -> np.ndarray:
        assert kwargs["sample_rate"] == render.SAMPLE_RATE
        assert kwargs["num_channels"] == render.NUM_CHANNELS
        return self.audio


def job_for(stem: Path, duration_s: float = 0.5) -> dict[str, Any]:
    return {
        "plugin_path": "/serum.vst3",
        "plugin_name": None,
        "state": None,
        "bpm": 120,
        "track": {
            "name": "lead",
            "engine": "pedalboard",
            "sound": "Serum",
            "notes": [{"beat": 0, "pitch": 60, "length": 0.5, "velocity": 100}],
        },
        "duration_s": duration_s,
        "stem_path": str(stem),
    }


def install_fake_plugin(monkeypatch: pytest.MonkeyPatch, audio: np.ndarray) -> None:
    monkeypatch.setattr("pedalboard.load_plugin", lambda *args, **kwargs: FakePlugin(audio))


def blank_audio(duration_s: float = 0.5) -> np.ndarray:
    samples = render.expected_samples(duration_s)
    return np.zeros((render.NUM_CHANNELS, samples), dtype=np.float32)


def test_worker_writes_stereo_48k_float_wav_above_full_scale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = blank_audio()
    audio[0, 10] = 1.5
    audio[1, 20] = -2.0
    install_fake_plugin(monkeypatch, audio)
    stem = tmp_path / "lead.wav"

    result = render_stem(job_for(stem))

    assert result["status"] is TrackStatus.RENDERED
    assert result["peak_dbfs"] == pytest.approx(6.02, abs=0.01)
    with ReadableAudioFile(str(stem)) as written:  # ty: ignore[no-matching-overload]
        assert written.samplerate == render.SAMPLE_RATE
        assert written.num_channels == render.NUM_CHANNELS
        assert written.file_dtype == "float32"
        read_back = written.read(written.frames)
    assert np.array_equal(read_back, audio)


def test_worker_leaves_no_temp_file_after_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = blank_audio()
    audio[0, 10] = 0.5
    install_fake_plugin(monkeypatch, audio)

    render_stem(job_for(tmp_path / "lead.wav"))

    assert [path.name for path in tmp_path.iterdir()] == ["lead.wav"]


def test_silent_render_writes_the_stem_and_reports_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_plugin(monkeypatch, blank_audio())
    stem = tmp_path / "lead.wav"

    result = render_stem(job_for(stem))

    assert result["status"] is TrackStatus.SILENT
    assert stem.exists()


def test_worker_reports_load_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise ImportError("no such plugin")

    monkeypatch.setattr("pedalboard.load_plugin", fail)

    result = render_stem(job_for(tmp_path / "lead.wav"))

    assert result == {"status": TrackStatus.LOAD_FAILED, "detail": "no such plugin"}


@pytest.mark.parametrize("bad_value", [np.nan, np.inf])
def test_worker_writes_no_stem_for_non_finite_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_value: float
) -> None:
    audio = blank_audio()
    audio[0, 10] = bad_value
    install_fake_plugin(monkeypatch, audio)
    stem = tmp_path / "lead.wav"

    with pytest.raises(RuntimeError, match="NaN or infinity"):
        render_stem(job_for(stem))

    assert not stem.exists()


def test_worker_rejects_unexpected_render_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_plugin(monkeypatch, blank_audio().T)
    with pytest.raises(RuntimeError, match="render shape"):
        render_stem(job_for(tmp_path / "lead.wav"))


def test_expected_samples_truncates_like_pedalboard() -> None:
    assert render.expected_samples(10.123456789) == 485925
    assert render.expected_samples(5.0) == 240000


def result_script(tmp_path: Path, action: str) -> list[str]:
    """Write a worker that logs its job and acts by track name, as `action` or `name=action`."""
    script = tmp_path / "worker.py"
    script.write_text(
        f"""
import json, sys, time
from pathlib import Path
job = json.loads(Path(sys.argv[-2]).read_text())
with open({str(tmp_path / "jobs.log")!r}, "a") as log:
    log.write(json.dumps(job) + "\\n")
stem = Path(job["stem_path"])
actions = dict(a.split("=") for a in {action!r}.split(",") if "=" in a)
action = actions.get(job["track"]["name"], {action!r} if "=" not in {action!r} else "pass")
if action == "crash":
    stem.with_name("." + stem.stem + ".tmp.wav").write_text("partial")
    sys.exit(4)
if action == "hang":
    time.sleep(30)
if action == "no-stem":
    Path(sys.argv[-1]).write_text(json.dumps(dict(status="rendered", peak_dbfs=-6.0)))
    sys.exit(0)
stem.write_text("audio")
status = "silent" if action == "silent" else "rendered"
Path(sys.argv[-1]).write_text(json.dumps(dict(status=status, peak_dbfs=-6.0)))
"""
    )
    return [sys.executable, str(script)]


def use_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    command = result_script(tmp_path, action)
    monkeypatch.setattr(render, "worker_command", lambda job_path: [*command, str(job_path)])


def statuses(results: list[render.TrackResult]) -> list[tuple[str, TrackStatus]]:
    return [(result.track, result.status) for result in results]


def test_unknown_sound_reports_and_other_tracks_still_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    song = make_song(make_track("one", "Nope"), make_track("two", "Serum"))

    results = render_song(song, "song", SPECS, tmp_path)

    assert statuses(results) == [
        ("one", TrackStatus.UNKNOWN_SOUND),
        ("two", TrackStatus.RENDERED),
    ]
    assert "Nope" in (results[0].detail or "")


@pytest.mark.parametrize(
    ("action", "expected"),
    [("one=crash", TrackStatus.CRASHED), ("one=hang", TrackStatus.TIMED_OUT)],
)
def test_failed_first_track_still_reports_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, expected: TrackStatus
) -> None:
    use_worker(tmp_path, monkeypatch, action)
    song = make_song(make_track("one"), make_track("two"))

    results = render_song(song, "song", SPECS, tmp_path, timeout_s=3)

    assert statuses(results) == [("one", expected), ("two", TrackStatus.RENDERED)]
    assert results[0].stem is None
    assert results[1].stem == str(tmp_path / "song" / "two.wav")


@pytest.mark.parametrize("action", ["crash", "hang"])
def test_failed_track_removes_earlier_stem_and_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    use_worker(tmp_path, monkeypatch, action)
    song_dir = tmp_path / "song"
    song_dir.mkdir()
    (song_dir / "lead.wav").write_text("earlier audio")

    render_song(make_song(make_track()), "song", SPECS, tmp_path, timeout_s=3)

    assert list(song_dir.iterdir()) == []


def test_render_removes_earlier_mix_and_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    song_dir = tmp_path / "song"
    for name in ("mix", "analysis"):
        (song_dir / name).mkdir(parents=True)
        (song_dir / name / "old.json").write_text("{}")

    render_song(make_song(make_track()), "song", SPECS, tmp_path)

    assert sorted(path.name for path in song_dir.iterdir()) == ["lead.wav"]


def test_unknown_sound_removes_earlier_stem(tmp_path: Path) -> None:
    song_dir = tmp_path / "song"
    song_dir.mkdir()
    (song_dir / "lead.wav").write_text("earlier audio")

    render_song(make_song(make_track("lead", "Nope")), "song", SPECS, tmp_path)

    assert list(song_dir.iterdir()) == []


def test_worker_claiming_a_stem_it_did_not_write_is_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "no-stem")

    results = render_song(make_song(make_track()), "song", SPECS, tmp_path)

    assert results[0].status is TrackStatus.CRASHED


def write_song(tmp_path: Path, name: str = "song.yaml") -> Path:
    data = {
        "bpm": 120,
        "tracks": [
            {
                "name": "lead",
                "engine": "pedalboard",
                "sound": "Serum",
                "notes": [{"beat": 0, "pitch": 60, "length": 1, "velocity": 100}],
            }
        ],
    }
    parse_song(data)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def write_plugins(tmp_path: Path) -> Path:
    path = tmp_path / "plugins.toml"
    path.write_text('[[plugin]]\nname = "Serum"\npath = "/serum.vst3"\n')
    return path


def cli_args(tmp_path: Path, song: Path) -> list[str]:
    plugins = write_plugins(tmp_path)
    return [str(song), "--config", str(plugins), "--output-dir", str(tmp_path / "out")]


def test_main_exits_zero_when_all_tracks_render_and_uses_song_file_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")

    code = render.main(cli_args(tmp_path, write_song(tmp_path, "tune.yaml")))

    assert code == EXIT_ALL_PASS
    assert (tmp_path / "out" / "tune" / "lead.wav").exists()
    row = capsys.readouterr().out.splitlines()[1].split()
    assert row == ["lead", "rendered", "-6.0", str(tmp_path / "out" / "tune" / "lead.wav")]


@pytest.mark.parametrize("action", ["crash", "silent"])
def test_main_exits_one_when_a_track_is_not_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    use_worker(tmp_path, monkeypatch, action)

    assert render.main(cli_args(tmp_path, write_song(tmp_path))) == EXIT_SOME_FAILED


def test_main_json_output_is_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")

    render.main([*cli_args(tmp_path, write_song(tmp_path)), "--json"])

    report = json.loads(capsys.readouterr().out)
    assert report[0]["track"] == "lead"
    assert report[0]["status"] == "rendered"
    assert report[0]["peak_dbfs"] == -6.0


def test_main_exits_two_for_missing_song(tmp_path: Path) -> None:
    args = cli_args(tmp_path, tmp_path / "missing.yaml")
    assert render.main(args) == EXIT_BAD_CONFIG


def test_main_exits_two_and_lists_problems_for_invalid_song(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song = tmp_path / "bad.yaml"
    song.write_text("bpm: 0\ntracks: []\n")

    assert render.main(cli_args(tmp_path, song)) == EXIT_BAD_CONFIG
    error = capsys.readouterr().err
    assert "bpm" in error
    assert "at least one track" in error


def test_main_exits_two_for_bad_plugin_list(tmp_path: Path) -> None:
    song = write_song(tmp_path)
    args = [str(song), "--config", str(tmp_path / "missing.toml")]
    assert render.main(args) == EXIT_BAD_CONFIG


def test_real_worker_reports_missing_plugin_as_load_failed(tmp_path: Path) -> None:
    specs = [PluginSpec("Serum", str(tmp_path / "missing.vst3"))]

    results = render_song(make_song(make_track()), "song", specs, tmp_path, timeout_s=60)

    assert results[0].status is TrackStatus.LOAD_FAILED
    assert results[0].detail


def test_default_output_directory_is_in_the_project() -> None:
    assert render.RENDERS_DIR == render.PROJECT_DIR / "renders"


STATE = base64.b64encode(b"saved-state").decode("ascii")


def write_sound_file(sounds_dir: Path, name: str, content: Any) -> None:
    sounds_dir.mkdir(exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (sounds_dir / f"{name}.json").write_text(text)


def good_sound(state: str = STATE) -> dict[str, str]:
    return {"plugin": "Serum", "description": "warm pad", "state": state}


def logged_jobs(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "jobs.log").read_text().splitlines()]


def test_saved_sound_passes_its_state_and_a_plugin_passes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    sounds_dir = tmp_path / "sounds"
    write_sound_file(sounds_dir, "warm-pad", good_sound())
    song = make_song(make_track("pad", "warm-pad"), make_track("lead", "Serum"))

    results = render_song(song, "song", SPECS, tmp_path, sounds_dir=sounds_dir)

    assert statuses(results) == [("pad", TrackStatus.RENDERED), ("lead", TrackStatus.RENDERED)]
    pad, lead = logged_jobs(tmp_path)
    assert (pad["plugin_path"], pad["state"]) == ("/serum.vst3", STATE)
    assert (lead["plugin_path"], lead["state"]) == ("/serum.vst3", None)


def test_sound_name_matches_files_exactly_not_ignoring_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    sounds_dir = tmp_path / "sounds"
    write_sound_file(sounds_dir, "pad", good_sound())

    results = render_song(
        make_song(make_track("lead", "Pad")), "song", SPECS, tmp_path, sounds_dir=sounds_dir
    )

    assert results[0].status is TrackStatus.UNKNOWN_SOUND
    assert not (tmp_path / "jobs.log").exists()


@pytest.mark.parametrize(
    ("name", "content", "problem"),
    [
        ("bad", "{not json", "cannot read"),
        ("bad", {"plugin": "Serum", "state": STATE}, "exactly the keys"),
        ("bad", {**good_sound(), "extra": 1}, "exactly the keys"),
        ("bad", {**good_sound(), "plugin": "Massive"}, "not in the plugin list"),
        ("bad", good_sound(state="not base64!"), "not valid base64"),
        ("bad", good_sound(state=""), "empty"),
        ("bad", {**good_sound(), "description": " "}, "description"),
        ("serum", good_sound(), "taken by the plugin 'Serum'"),
    ],
)
def test_unusable_sound_file_is_load_failed_and_next_track_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, content: Any, problem: str
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    sounds_dir = tmp_path / "sounds"
    write_sound_file(sounds_dir, name, content)
    song = make_song(make_track("one", name), make_track("two", "Serum"))

    results = render_song(song, "song", SPECS, tmp_path, sounds_dir=sounds_dir)

    assert statuses(results) == [("one", TrackStatus.LOAD_FAILED), ("two", TrackStatus.RENDERED)]
    assert problem in (results[0].detail or "")


class StatefulFakePlugin(FakePlugin):
    def __init__(self, audio: np.ndarray, events: list[Any]) -> None:
        super().__init__(audio)
        self.events = events

    @property
    def raw_state(self) -> bytes:
        raise AssertionError("not read during a render")

    @raw_state.setter
    def raw_state(self, value: bytes) -> None:
        self.events.append(("raw_state", value))

    def __call__(self, messages: list[Any], **kwargs: Any) -> np.ndarray:
        self.events.append(("render", None))
        return super().__call__(messages, **kwargs)


def test_worker_restores_the_job_state_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []
    plugin = StatefulFakePlugin(blank_audio(), events)
    monkeypatch.setattr("pedalboard.load_plugin", lambda *args, **kwargs: plugin)

    render_stem({**job_for(tmp_path / "lead.wav"), "state": STATE})

    assert events == [("raw_state", b"saved-state"), ("render", None)]


def test_worker_reports_a_rejected_state_as_load_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RejectingPlugin(FakePlugin):
        @property
        def raw_state(self) -> bytes:
            return b""

        @raw_state.setter
        def raw_state(self, value: bytes) -> None:
            raise RuntimeError("bad state")

    monkeypatch.setattr(
        "pedalboard.load_plugin", lambda *args, **kwargs: RejectingPlugin(blank_audio())
    )
    stem = tmp_path / "lead.wav"

    result = render_stem({**job_for(stem), "state": STATE})

    assert result == {"status": TrackStatus.LOAD_FAILED, "detail": "bad state"}
    assert not stem.exists()


def test_main_takes_a_sounds_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    sounds_dir = tmp_path / "sounds"
    write_sound_file(sounds_dir, "warm-pad", good_sound())
    song = write_song(tmp_path)
    song.write_text(song.read_text().replace("sound: Serum", "sound: warm-pad"))

    code = render.main([*cli_args(tmp_path, song), "--sounds-dir", str(sounds_dir)])

    assert code == EXIT_ALL_PASS
    assert logged_jobs(tmp_path)[0]["state"] == STATE


def preset_specs(tmp_path: Path) -> list[PluginSpec]:
    return [PluginSpec("Serum", "/serum.vst3", None, str(tmp_path / "presets"))]


def write_preset(tmp_path: Path, relative: str, data: bytes) -> None:
    path = tmp_path / "presets" / f"{relative}.fxp"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_preset_passes_its_built_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    write_preset(tmp_path, "Bass/Deep", fxp_bytes(b"chunk-data"))

    results = render_song(
        make_song(make_track("bass", "Serum:Bass/Deep")), "song", preset_specs(tmp_path), tmp_path
    )

    assert statuses(results) == [("bass", TrackStatus.RENDERED)]
    (job,) = logged_jobs(tmp_path)
    expected = base64.b64encode(build_state(b"chunk-data")).decode("ascii")
    assert (job["plugin_path"], job["state"]) == ("/serum.vst3", expected)


@pytest.mark.parametrize(
    ("sound", "with_presets"),
    [("Serum:bass/Deep", True), ("Nope:Bass/Deep", True), ("Serum:Bass/Deep", False)],
    ids=["case differs", "unknown plugin", "plugin without presets"],
)
def test_unmatched_preset_name_is_unknown_sound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sound: str, with_presets: bool
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    write_preset(tmp_path, "Bass/Deep", fxp_bytes(b"chunk-data"))
    specs = preset_specs(tmp_path) if with_presets else SPECS

    results = render_song(make_song(make_track("bass", sound)), "song", specs, tmp_path)

    assert results[0].status is TrackStatus.UNKNOWN_SOUND
    assert not (tmp_path / "jobs.log").exists()


def test_unusable_preset_is_load_failed_and_next_track_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    write_preset(tmp_path, "Bass/Deep", fxp_bytes(b"chunk", kind=b"FxCk"))
    song = make_song(make_track("one", "Serum:Bass/Deep"), make_track("two", "Serum"))

    results = render_song(song, "song", preset_specs(tmp_path), tmp_path)

    assert statuses(results) == [("one", TrackStatus.LOAD_FAILED), ("two", TrackStatus.RENDERED)]
    assert "parameter" in (results[0].detail or "")


def test_missing_presets_directory_is_load_failed_and_next_track_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    song = make_song(make_track("one", "Serum:Bass/Deep"), make_track("two", "Serum"))

    results = render_song(song, "song", preset_specs(tmp_path), tmp_path)

    assert statuses(results) == [("one", TrackStatus.LOAD_FAILED), ("two", TrackStatus.RENDERED)]
    assert "does not exist" in (results[0].detail or "")
