"""Tests that use a fake plugin or substitute worker scripts, with no real plugins."""

import base64
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import yaml
from helpers import fxp_bytes
from pedalboard.io import ReadableAudioFile

from poser import render
from poser.plugin_check import EXIT_ALL_PASS, EXIT_BAD_CONFIG, EXIT_SOME_FAILED, PluginSpec
from poser.presets import build_state
from poser.render import Outcome, RenderJob, TrackStatus, render_song, render_stem
from poser.song import ControlChange, Note, Song, Track, load_song, parse_song


def make_song(*tracks: Track, bpm: float = 120) -> Song:
    return Song(bpm=bpm, tracks=tracks)


def make_track(name: str = "lead", sound: str = "Serum", *notes: Note) -> Track:
    return Track(name, "pedalboard", sound, notes or (Note(0, 60, 1, 100),))


SPECS = [PluginSpec("Serum", "/serum.vst3")]


def pedalboard_sample(time: float) -> int:
    """Return the sample Pedalboard plays a message at: it truncates `time * sample_rate` in
    32-bit floats."""
    return int(np.float32(time) * np.float32(render.SAMPLE_RATE))


def message_times(track: Track, song: Song) -> list[tuple[str, int, int]]:
    messages = render.track_messages(song, track)
    # mido sets message fields dynamically, so they are read through `dict()`.
    return [
        (f["type"], f["note"], pedalboard_sample(f["time"]))
        for f in (m.dict() for m in messages)
        if f["type"] != "control_change"
    ]


def message_fields(track: Track, song: Song) -> list[tuple[str, int, int]]:
    """Return each message's type, its note or controller, and its sample."""
    return [
        (
            f["type"],
            f["control"] if f["type"] == "control_change" else f["note"],
            pedalboard_sample(f["time"]),
        )
        for f in (m.dict() for m in render.track_messages(song, track))
    ]


def test_beats_convert_to_seconds_at_bpm() -> None:
    track = make_track("lead", "Serum", Note(2, 60, 1, 100))
    assert message_times(track, make_song(track)) == [
        ("note_on", 60, 48000),
        ("note_off", 60, 72000),
    ]


def test_note_velocity_reaches_the_message() -> None:
    track = make_track("lead", "Serum", Note(0, 60, 1, 37))
    messages = render.track_messages(make_song(track), track)
    assert messages[0].dict()["velocity"] == 37


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

    jobs = logged_jobs(tmp_path)
    assert [job["track"]["name"] for job in jobs] == ["short", "long"]
    assert {job["duration_s"] for job in jobs} == {6.0}


def test_repeated_pitch_ends_before_it_retriggers() -> None:
    track = make_track("lead", "Serum", Note(1, 60, 1, 100), Note(0, 60, 1, 100))
    assert message_times(track, make_song(track)) == [
        ("note_on", 60, 0),
        ("note_off", 60, 24000),
        ("note_on", 60, 24000),
        ("note_off", 60, 48000),
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
        self.messages: list[object] = []
        self.kwargs: dict[str, object] = {}

    def __call__(self, messages: list[object], **kwargs: object) -> np.ndarray:
        assert kwargs["sample_rate"] == render.SAMPLE_RATE
        assert kwargs["num_channels"] == render.NUM_CHANNELS
        self.messages, self.kwargs = messages, kwargs
        return self.audio


def job_for(stem: Path, duration_s: float = 0.5, state: str | None = None) -> RenderJob:
    return {
        "plugin_path": "/serum.vst3",
        "plugin_name": None,
        "state": state,
        "bpm": 120,
        "track": {
            "name": "lead",
            "engine": "pedalboard",
            "sound": "Serum",
            "notes": [{"beat": 0, "pitch": 60, "length": 0.5, "velocity": 100}],
            "cc": [],
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

    assert result.status is TrackStatus.RENDERED
    assert result.peak_dbfs == pytest.approx(6.02, abs=0.01)
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

    assert result.status is TrackStatus.SILENT
    assert stem.exists()


def test_worker_reports_load_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise ImportError("no such plugin")

    monkeypatch.setattr("pedalboard.load_plugin", fail)

    result = render_stem(job_for(tmp_path / "lead.wav"))

    assert result == Outcome(TrackStatus.LOAD_FAILED, detail="no such plugin")


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
if action == "stem-crash":
    stem.write_text("audio")
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

    assert entries(song_dir) == []


def test_render_removes_earlier_mix_and_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    song_dir = tmp_path / "song"
    for name in ("mix", "analysis"):
        (song_dir / name).mkdir(parents=True)
        (song_dir / name / "old.json").write_text("{}")

    render_song(make_song(make_track()), "song", SPECS, tmp_path)

    assert entries(song_dir) == ["lead.json", "lead.wav"]


def test_unknown_sound_removes_earlier_stem(tmp_path: Path) -> None:
    song_dir = tmp_path / "song"
    song_dir.mkdir()
    (song_dir / "lead.wav").write_text("earlier audio")

    render_song(make_song(make_track("lead", "Nope")), "song", SPECS, tmp_path)

    assert entries(song_dir) == []


def test_worker_claiming_a_stem_it_did_not_write_is_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "no-stem")

    results = render_song(make_song(make_track()), "song", SPECS, tmp_path)

    assert results[0].status is TrackStatus.CRASHED


def test_earlier_stem_does_not_vouch_for_a_worker_that_wrote_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    song_dir = tmp_path / "song"
    song_dir.mkdir()
    (song_dir / "lead.wav").write_text("earlier audio")
    use_worker(tmp_path, monkeypatch, "no-stem")

    (result,) = render_song(make_song(make_track()), "song", SPECS, tmp_path)

    assert result.detail == "worker reported a stem it did not write"
    assert entries(song_dir) == []


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


def entries(song_dir: Path) -> list[str]:
    """Return the sorted names in a song's render directory, without the lock file every run
    leaves."""
    return sorted(path.name for path in song_dir.iterdir() if path.name != render.LOCK_NAME)


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
    assert row == ["lead", "rendered", "-", "-6.0", str(tmp_path / "out" / "tune" / "lead.wav")]


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


def write_sound_file(sounds_dir: Path, name: str, content: str | Mapping[str, object]) -> None:
    sounds_dir.mkdir(exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content)
    (sounds_dir / f"{name}.json").write_text(text)


def good_sound(state: str = STATE) -> dict[str, str]:
    return {"plugin": "Serum", "description": "warm pad", "state": state}


def logged_jobs(tmp_path: Path) -> list[RenderJob]:
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    content: str | Mapping[str, object],
    problem: str,
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    sounds_dir = tmp_path / "sounds"
    write_sound_file(sounds_dir, name, content)
    song = make_song(make_track("one", name), make_track("two", "Serum"))

    results = render_song(song, "song", SPECS, tmp_path, sounds_dir=sounds_dir)

    assert statuses(results) == [("one", TrackStatus.LOAD_FAILED), ("two", TrackStatus.RENDERED)]
    assert problem in (results[0].detail or "")


class StatefulFakePlugin(FakePlugin):
    def __init__(self, audio: np.ndarray, events: list[tuple[str, bytes | None]]) -> None:
        super().__init__(audio)
        self.events = events

    @property
    def raw_state(self) -> bytes:
        raise AssertionError("not read during a render")

    @raw_state.setter
    def raw_state(self, value: bytes) -> None:
        self.events.append(("raw_state", value))

    def __call__(self, messages: list[object], **kwargs: object) -> np.ndarray:
        self.events.append(("render", None))
        return super().__call__(messages, **kwargs)


def test_worker_restores_the_job_state_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, bytes | None]] = []
    plugin = StatefulFakePlugin(blank_audio(), events)
    monkeypatch.setattr("pedalboard.load_plugin", lambda *args, **kwargs: plugin)

    render_stem(job_for(tmp_path / "lead.wav", state=STATE))

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

    result = render_stem(job_for(stem, state=STATE))

    assert result == Outcome(TrackStatus.LOAD_FAILED, detail="bad state")
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


# Stem cache


def job_names(tmp_path: Path) -> list[str]:
    log = tmp_path / "jobs.log"
    return [job["track"]["name"] for job in logged_jobs(tmp_path)] if log.exists() else []


LEAD = make_track("lead", "Serum", Note(0, 60, 1, 100))
BASS = make_track("bass", "Serum", Note(0, 36, 2, 100))  # Its note ends last, so it sets duration.


def with_notes(track: Track, *notes: Note) -> Track:
    return replace(track, notes=notes)


@pytest.mark.parametrize(
    "changed",
    [
        make_song(with_notes(LEAD, Note(0.5, 60, 1, 100)), BASS),
        make_song(with_notes(LEAD, Note(0, 61, 1, 100)), BASS),
        make_song(with_notes(LEAD, Note(0, 60, 1.5, 100)), BASS),
        make_song(with_notes(LEAD, Note(0, 60, 1, 99)), BASS),
        make_song(LEAD, BASS, bpm=121),
        make_song(LEAD, with_notes(BASS, Note(0, 36, 3, 100))),
        make_song(Track("lead2", "pedalboard", "Serum", LEAD.notes), BASS),
        make_song(Track("lead", "other", "Serum", LEAD.notes), BASS),
        make_song(Track("lead", "pedalboard", "warm-pad", LEAD.notes), BASS),
    ],
    ids=["beat", "pitch", "length", "velocity", "bpm", "duration", "name", "engine", "sound"],
)
def test_track_key_changes_with_every_render_input(changed: Song) -> None:
    assert render.track_key(changed, changed.tracks[0]) != render.track_key(
        make_song(LEAD, BASS), LEAD
    )


def test_track_key_ignores_gain_and_number_spelling() -> None:
    song = make_song(LEAD, BASS)
    gained = Track(LEAD.name, LEAD.engine, LEAD.sound, LEAD.notes, gain=-6.0)
    floats = with_notes(LEAD, Note(0.0, 60, 1.0, 100))
    assert render.track_key(make_song(gained, BASS), gained) == render.track_key(song, LEAD)
    assert render.track_key(make_song(floats, BASS, bpm=120.0), floats) == render.track_key(
        song, LEAD
    )


def test_sound_key_changes_with_state_plugin_path_and_plugin_name() -> None:
    spec = PluginSpec("Serum", "/serum.vst3")
    base = render.sound_key(spec, STATE)
    assert render.sound_key(spec, STATE) == base
    assert render.sound_key(spec, None) != base
    assert render.sound_key(PluginSpec("Serum", "/other.vst3"), STATE) != base
    assert render.sound_key(PluginSpec("Serum", "/serum.vst3", "Serum 2"), STATE) != base


def test_second_render_of_an_unchanged_song_runs_no_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    args = [*cli_args(tmp_path, write_song(tmp_path)), "--json"]
    assert render.main(args) == EXIT_ALL_PASS
    (first,) = json.loads(capsys.readouterr().out)
    # The cached peak must come from the stamp, so give the stamp a peak no worker reports.
    stamp = tmp_path / "out" / "song" / "lead.json"
    stamp.write_text(json.dumps({**json.loads(stamp.read_text()), "peak_dbfs": -9.5}))

    code = render.main(args)

    (second,) = json.loads(capsys.readouterr().out)
    assert code == EXIT_ALL_PASS
    assert job_names(tmp_path) == ["lead"]
    assert first["cached"] is False
    assert second == {**first, "peak_dbfs": -9.5, "cached": True}


def test_cached_silent_track_stays_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_worker(tmp_path, monkeypatch, "silent")
    render_song(make_song(LEAD), "song", SPECS, tmp_path)

    (result,) = render_song(make_song(LEAD), "song", SPECS, tmp_path)

    assert (result.status, result.peak_dbfs, result.cached) == (TrackStatus.SILENT, -6.0, True)
    assert render.exit_code([result]) == EXIT_SOME_FAILED
    assert job_names(tmp_path) == ["lead"]


@pytest.mark.parametrize(
    ("changed", "rendered"),
    [
        (make_song(with_notes(LEAD, Note(0, 62, 1, 100)), BASS), ["lead"]),
        (
            make_song(
                Track(LEAD.name, LEAD.engine, LEAD.sound, LEAD.notes, gain=-3.0),
                Track(BASS.name, BASS.engine, BASS.sound, BASS.notes, gain=-9.0),
            ),
            [],
        ),
        (make_song(LEAD, BASS, bpm=90), ["lead", "bass"]),
        (make_song(LEAD, with_notes(BASS, Note(0, 36, 4, 100))), ["lead", "bass"]),
    ],
    ids=["one track's notes", "gains only", "bpm", "last note extended"],
)
def test_render_runs_only_tracks_whose_inputs_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: Song, rendered: list[str]
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    render_song(make_song(LEAD, BASS), "song", SPECS, tmp_path)

    results = render_song(changed, "song", SPECS, tmp_path)

    assert job_names(tmp_path)[2:] == rendered
    assert [result.track for result in results if not result.cached] == rendered
    assert all(result.status is TrackStatus.RENDERED for result in results)


def test_rewritten_saved_sound_renders_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    sounds_dir = tmp_path / "sounds"
    write_sound_file(sounds_dir, "warm-pad", good_sound())
    song = make_song(make_track("pad", "warm-pad"))
    render_song(song, "song", SPECS, tmp_path, sounds_dir=sounds_dir)
    other = base64.b64encode(b"other-state").decode("ascii")
    write_sound_file(sounds_dir, "warm-pad", good_sound(state=other))

    (result,) = render_song(song, "song", SPECS, tmp_path, sounds_dir=sounds_dir)

    assert not result.cached
    assert [job["state"] for job in logged_jobs(tmp_path)] == [STATE, other]


def test_force_renders_every_track(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    render_song(make_song(LEAD, BASS), "song", SPECS, tmp_path)

    results = render_song(make_song(LEAD, BASS), "song", SPECS, tmp_path, force=True)

    assert job_names(tmp_path) == ["lead", "bass", "lead", "bass"]
    assert [result.cached for result in results] == [False, False]


def test_main_force_renders_every_track(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    args = cli_args(tmp_path, write_song(tmp_path))
    render.main(args)

    assert render.main([*args, "--force"]) == EXIT_ALL_PASS

    assert job_names(tmp_path) == ["lead", "lead"]


def rewrite_stem(stem: Path) -> None:
    stem.write_text("different audio")


def touch_stem(stem: Path) -> None:
    info = stem.stat()
    stem.write_text(stem.read_text())
    os.utime(stem, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))


@pytest.mark.parametrize(
    "spoil",
    [
        rewrite_stem,
        touch_stem,
        lambda stem: stem.unlink(),
        lambda stem: stem.with_suffix(".json").unlink(),
        lambda stem: stem.with_suffix(".json").write_text("{not json"),
    ],
    ids=["stem rewritten", "stem touched", "stem deleted", "stamp deleted", "stamp malformed"],
)
def test_changed_stem_or_stamp_renders_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spoil: Callable[[Path], None]
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    render_song(make_song(LEAD), "song", SPECS, tmp_path)
    spoil(tmp_path / "song" / "lead.wav")

    (result,) = render_song(make_song(LEAD), "song", SPECS, tmp_path)

    assert not result.cached
    assert job_names(tmp_path) == ["lead", "lead"]
    assert render.read_stamp(tmp_path / "song" / "lead.json") is not None


def good_stamp() -> dict[str, object]:
    return {
        "track_key": "a",
        "sound_key": "b",
        "status": "rendered",
        "peak_dbfs": -6.0,
        "stem_size": 5,
        "stem_mtime_ns": 1,
    }


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        b"\xff\xfe".decode("latin-1"),
        json.dumps({key: value for key, value in good_stamp().items() if key != "sound_key"}),
        json.dumps({**good_stamp(), "extra": 1}),
        json.dumps({**good_stamp(), "status": "crashed"}),
        json.dumps({**good_stamp(), "status": ["rendered"]}),
        json.dumps({**good_stamp(), "track_key": 1}),
        json.dumps({**good_stamp(), "sound_key": 1}),
        json.dumps({**good_stamp(), "peak_dbfs": "loud"}),
        json.dumps({**good_stamp(), "peak_dbfs": float("nan")}),
        json.dumps({**good_stamp(), "peak_dbfs": 10**400}),
        json.dumps({**good_stamp(), "stem_size": True}),
        json.dumps({**good_stamp(), "stem_mtime_ns": 1.5}),
    ],
)
def test_malformed_stamp_reads_as_none(tmp_path: Path, text: str) -> None:
    path = tmp_path / "lead.json"
    path.write_text(text)
    assert render.read_stamp(path) is None


def test_missing_stamp_reads_as_none_and_a_good_one_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "lead.json"
    assert render.read_stamp(path) is None
    path.write_text(json.dumps({**good_stamp(), "peak_dbfs": None}))
    assert render.read_stamp(path) == render.Stamp("a", "b", TrackStatus.RENDERED, None, 5, 1)


@pytest.mark.parametrize("action", ["crash", "stem-crash", "no-stem"])
def test_failure_after_a_success_leaves_neither_stem_nor_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    render_song(make_song(LEAD), "song", SPECS, tmp_path)
    use_worker(tmp_path, monkeypatch, action)

    (result,) = render_song(make_song(LEAD), "song", SPECS, tmp_path, force=True)

    assert result.status is TrackStatus.CRASHED
    assert entries(tmp_path / "song") == []


def test_unknown_sound_deletes_stem_and_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    render_song(make_song(LEAD), "song", SPECS, tmp_path)

    (result,) = render_song(make_song(make_track("lead", "Nope")), "song", SPECS, tmp_path)

    assert result.status is TrackStatus.UNKNOWN_SOUND
    assert entries(tmp_path / "song") == []


def test_stamp_records_the_keys_and_the_stem_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    song = make_song(LEAD)

    render_song(song, "song", SPECS, tmp_path)

    song_dir = tmp_path / "song"
    stamp = json.loads((song_dir / "lead.json").read_text())
    info = (song_dir / "lead.wav").stat()
    assert stamp == {
        "track_key": render.track_key(song, LEAD),
        "sound_key": render.sound_key(SPECS[0], None),
        "status": "rendered",
        "peak_dbfs": -6.0,
        "stem_size": info.st_size,
        "stem_mtime_ns": info.st_mtime_ns,
    }
    assert entries(song_dir) == ["lead.json", "lead.wav"]


def test_failed_stamp_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stem = tmp_path / "lead.wav"
    stem.write_text("audio")

    def refuse(source: object, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(render.os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        render.write_stamp(tmp_path / "lead.json", stem, "a", "b", TrackStatus.RENDERED, -6.0)

    assert [path.name for path in tmp_path.iterdir()] == ["lead.wav"]


def test_table_shows_cached_tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    args = cli_args(tmp_path, write_song(tmp_path))
    render.main(args)
    capsys.readouterr()

    render.main(args)

    header, row = capsys.readouterr().out.splitlines()
    assert header.split()[:3] == ["TRACK", "STATUS", "CACHED"]
    assert row.split() == ["lead", "rendered", "yes", "-6.0", str(tmp_path / "out/song/lead.wav")]


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ([], "worker wrote a malformed result"),
        ({"status": "rendered", "extra": 1}, "worker wrote a malformed result"),
        ({"status": "rendered", "peak_dbfs": "loud"}, "worker wrote a malformed result"),
        ({"status": "rendered", "peak_dbfs": True}, "worker wrote a malformed result"),
        ({"status": "rendered", "peak_dbfs": float("inf")}, "worker wrote a malformed result"),
        ({"status": "rendered", "peak_dbfs": 10**400}, "worker wrote a malformed result"),
        ({"status": "load_failed", "detail": 3}, "worker wrote a malformed result"),
        ({"status": "nope"}, "worker wrote an unknown status"),
        ({"status": ["rendered"]}, "worker wrote an unknown status"),
        ({}, "worker wrote an unknown status"),
    ],
)
def test_malformed_worker_result_is_crashed(tmp_path: Path, payload: object, detail: str) -> None:
    stem = tmp_path / "lead.wav"
    stem.write_text("audio")
    assert render.parse_worker_result(payload, stem) == Outcome(TrackStatus.CRASHED, detail=detail)


def test_well_formed_worker_result_is_kept(tmp_path: Path) -> None:
    stem = tmp_path / "lead.wav"
    stem.write_text("audio")
    payload = {"status": "silent", "peak_dbfs": -90}
    assert render.parse_worker_result(payload, stem) == Outcome(TrackStatus.SILENT, -90.0)
    failed = {"status": "load_failed", "detail": "no"}
    assert render.parse_worker_result(failed, stem) == Outcome(TrackStatus.LOAD_FAILED, None, "no")


# Controller changes


def cc_track(*changes: ControlChange, notes: tuple[Note, ...] = (Note(0, 60, 1, 100),)) -> Track:
    return Track("lead", "pedalboard", "Serum", notes, cc=changes)


def events_for(*changes: ControlChange) -> list[tuple[int, int, int]]:
    track = cc_track(*changes)
    return render.control_events(make_song(track), track)


def test_linear_change_sends_one_event_per_value_where_the_line_rounds_to_it() -> None:
    rising = events_for(ControlChange(0, 1, 0, "linear"), ControlChange(1, 1, 4))
    falling = events_for(ControlChange(0, 1, 4, "linear"), ControlChange(1, 1, 0))

    assert rising == [(0, 1, 0), (3000, 1, 1), (9000, 1, 2), (15000, 1, 3), (21000, 1, 4)]
    assert falling == [(0, 1, 4), (3000, 1, 3), (9000, 1, 2), (15000, 1, 1), (21000, 1, 0)]


def test_full_ramp_sends_128_events_however_long_it_is() -> None:
    short = events_for(ControlChange(0, 1, 0, "linear"), ControlChange(1, 1, 127))
    long = events_for(ControlChange(0, 1, 0, "linear"), ControlChange(32, 1, 127))

    assert [value for _, _, value in short] == list(range(128))
    assert [value for _, _, value in long] == list(range(128))


def test_linear_change_ramps_to_the_next_change_of_its_own_controller() -> None:
    events = events_for(
        ControlChange(0, 1, 0, "linear"), ControlChange(0.5, 74, 9), ControlChange(1, 1, 2)
    )

    assert events == [(0, 1, 0), (0, 74, 9), (6000, 1, 1), (18000, 1, 2)]


def test_linear_change_can_ramp_into_another_linear_change() -> None:
    events = events_for(
        ControlChange(0, 1, 0, "linear"), ControlChange(1, 1, 2, "linear"), ControlChange(2, 1, 0)
    )

    assert events == [(0, 1, 0), (6000, 1, 1), (18000, 1, 2), (30000, 1, 1), (42000, 1, 0)]


def test_linear_first_change_holds_its_start_value_until_its_beat() -> None:
    events = events_for(ControlChange(2, 1, 0, "linear"), ControlChange(3, 1, 2))

    assert events == [(0, 1, 0), (54000, 1, 1), (66000, 1, 2)]


def test_linear_change_without_a_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="no later target"):
        events_for(ControlChange(0, 1, 0, "linear"))


def test_first_change_holds_from_the_start_of_the_song() -> None:
    assert events_for(ControlChange(2, 1, 64)) == [(0, 1, 64)]
    assert events_for(ControlChange(2, 1, 64), ControlChange(3, 1, 0)) == [
        (0, 1, 64),
        (72000, 1, 0),
    ]


def test_last_change_at_a_sample_wins_and_other_controllers_keep_listed_order() -> None:
    assert events_for(ControlChange(0, 1, 10), ControlChange(0, 1, 20)) == [(0, 1, 20)]
    assert events_for(ControlChange(1, 74, 5), ControlChange(1, 1, 6)) == [
        (0, 74, 5),
        (0, 1, 6),
    ]


def test_a_ramp_shorter_than_a_sample_sends_one_event() -> None:
    events = events_for(ControlChange(0, 1, 0, "linear"), ControlChange(1e-9, 1, 127))

    assert events == [(0, 1, 127)]


def test_controller_change_sorts_between_note_off_and_note_on_at_one_sample() -> None:
    notes = (Note(0, 60, 1, 100), Note(1, 62, 1, 100))
    track = cc_track(ControlChange(0, 1, 0), ControlChange(1, 1, 90), notes=notes)

    assert message_fields(track, make_song(track)) == [
        ("control_change", 1, 0),
        ("note_on", 60, 0),
        ("note_off", 60, 24000),
        ("control_change", 1, 24000),
        ("note_on", 62, 24000),
        ("note_off", 62, 48000),
    ]


def test_every_message_time_truncates_to_its_sample() -> None:
    # One beat at 120 bpm is 24000 samples, so beat k / 24000 lands on sample k.
    beats = [k / 24000 for k in (27, 49, 54, 61, 98, 12345)]
    notes = tuple(Note(beat, 60, 1e-9, 100) for beat in beats)
    changes = (ControlChange(beat, 1, i + 1) for i, beat in enumerate(beats))
    track = cc_track(ControlChange(0, 1, 0), *changes, notes=notes)

    messages = render.track_messages(make_song(track), track)
    samples = {(m.dict()["type"], pedalboard_sample(m.dict()["time"])) for m in messages}

    assert {("note_on", k) for k in (27, 49, 54, 61, 98, 12345)} <= samples
    assert {("control_change", k) for k in (27, 49, 54, 61, 98, 12345)} <= samples


def test_controller_value_reaches_the_message() -> None:
    track = cc_track(ControlChange(0, 74, 33))
    (change,) = [m.dict() for m in render.track_messages(make_song(track), track)][:1]
    assert (change["type"], change["control"], change["value"], change["channel"]) == (
        "control_change",
        74,
        33,
        0,
    )


def test_duration_uses_a_controller_change_after_the_last_note() -> None:
    late = cc_track(ControlChange(6, 1, 0))
    early = cc_track(ControlChange(0.5, 1, 0))
    assert render.song_duration_s(make_song(late)) == pytest.approx(3.0 + render.TAIL_S)
    assert render.song_duration_s(make_song(early)) == pytest.approx(0.5 + render.TAIL_S)


BASE_CC = (ControlChange(0, 1, 0),)


@pytest.mark.parametrize(
    "cc",
    [
        (*BASE_CC, ControlChange(1, 1, 64)),
        (ControlChange(0.5, 1, 0),),
        (ControlChange(0, 74, 0),),
        (ControlChange(0, 1, 1),),
    ],
    ids=["added", "beat", "control", "value"],
)
def test_track_key_changes_with_controller_changes(cc: tuple[ControlChange, ...]) -> None:
    base, changed = replace(LEAD, cc=BASE_CC), replace(LEAD, cc=cc)
    assert render.track_key(make_song(changed, BASS), changed) != render.track_key(
        make_song(base, BASS), base
    )


def test_track_key_changes_with_shape_and_not_with_an_empty_cc() -> None:
    step = replace(LEAD, cc=(ControlChange(0, 1, 0), ControlChange(1, 1, 9)))
    linear = replace(LEAD, cc=(ControlChange(0, 1, 0, "linear"), ControlChange(1, 1, 9)))
    song = make_song(LEAD, BASS)
    empty = replace(LEAD, cc=())

    assert render.track_key(make_song(step, BASS), step) != render.track_key(
        make_song(linear, BASS), linear
    )
    assert render.track_key(make_song(empty, BASS), empty) == render.track_key(song, LEAD)


def test_worker_job_carries_controller_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    track = cc_track(ControlChange(0, 1, 0, "linear"), ControlChange(1, 1, 127))

    render_song(make_song(track), "song", SPECS, tmp_path)

    (job,) = logged_jobs(tmp_path)
    assert job["track"]["cc"] == [
        {"beat": 0, "control": 1, "value": 0, "shape": "linear"},
        {"beat": 1, "control": 1, "value": 127, "shape": "step"},
    ]


def test_worker_sends_controller_changes_in_512_sample_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = FakePlugin(blank_audio())
    monkeypatch.setattr("pedalboard.load_plugin", lambda *args, **kwargs: plugin)
    job = job_for(tmp_path / "lead.wav")
    job["track"]["cc"] = [
        {"beat": 0.25, "control": 74, "value": 50},
        {"beat": 0.5, "control": 74, "value": 60},
    ]

    render_stem(job)

    sent = [m.dict() for m in plugin.messages if isinstance(m, render.mido.Message)]
    changes = [m for m in sent if m["type"] == "control_change"]
    assert [(m["control"], m["value"], pedalboard_sample(m["time"])) for m in changes] == [
        (74, 50, 0),
        (74, 60, 12000),
    ]
    assert plugin.kwargs["buffer_size"] == 512


def test_render_refuses_to_start_while_the_song_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    song_path = write_song(tmp_path, "tune.yaml")
    song = load_song(song_path)
    song_dir = (tmp_path / "out" / "tune").resolve()

    with render.song_lock(song_dir):
        with pytest.raises(render.SongBusyError):
            render_song(song, "tune", SPECS, tmp_path / "out")
        assert render.main(cli_args(tmp_path, song_path)) == render.EXIT_SONG_BUSY
        assert capsys.readouterr().err == (
            "render-song: another render-song or analyze-song is running on tune\n"
        )
        assert not (song_dir / "lead.wav").exists()

    assert statuses(render_song(song, "tune", SPECS, tmp_path / "out")) == [
        ("lead", TrackStatus.RENDERED)
    ]


LOCK_HOLDER = """
import sys, time
from pathlib import Path
from poser.render import song_lock

with song_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(60)
"""


def test_lock_of_a_killed_run_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_worker(tmp_path, monkeypatch, "pass")
    song = load_song(write_song(tmp_path, "tune.yaml"))
    song_dir = (tmp_path / "out" / "tune").resolve()
    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(song_dir)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        with pytest.raises(render.SongBusyError), render.song_lock(song_dir):
            pass
    finally:
        holder.kill()
        holder.wait()

    assert statuses(render_song(song, "tune", SPECS, tmp_path / "out")) == [
        ("lead", TrackStatus.RENDERED)
    ]
