"""Tests that write stems directly and load no plugins."""

import hashlib
import json
import os
from pathlib import Path
from typing import NoReturn

import numpy as np
import pedalboard
import pytest
import yaml
from pedalboard.io import ReadableAudioFile, WriteableAudioFile

from poser import analyze, render
from poser.analyze import Defect, TrackStatus, analyze_song
from poser.plugin_check import (
    EXIT_ALL_PASS,
    EXIT_BAD_CONFIG,
    EXIT_SOME_FAILED,
    PluginSpec,
)
from poser.song import Note, Song, Track, load_song

RATE = 48000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SPECS = [PluginSpec("Serum", "/serum.vst3")]
SERUM_KEY = render.sound_key(SPECS[0], None)


def samples_for(beats: float) -> int:
    # At 60 bpm a beat is one second, and every stem has the 2.0-second tail.
    return int((beats + 2.0) * RATE)


def write_wav(path: Path, audio: np.ndarray) -> None:
    with WriteableAudioFile(  # ty: ignore[no-matching-overload]
        str(path), samplerate=RATE, num_channels=audio.shape[0], bit_depth=32
    ) as output:
        output.write(audio.astype(np.float32))


def read_wav(path: Path) -> np.ndarray:
    with ReadableAudioFile(str(path)) as audio:  # ty: ignore[no-matching-overload]
        return audio.read(audio.frames)


def constant(value: float, beats: float = 1) -> np.ndarray:
    return np.full((2, samples_for(beats)), value, dtype=np.float32)


def make_song(
    tmp_path: Path,
    stems: dict[str, np.ndarray | None],
    beats: float = 1,
    gains: dict[str, float] | None = None,
) -> Path:
    """Write a 60 bpm song whose tracks each hold one note, then write the given stems.

    Each written stem gets a render stamp for the song as written. A stem of None is left
    unwritten. A track named in `gains` gets that `gain`.
    """
    gains = gains or {}
    tracks = [
        {
            "name": name,
            "engine": "pedalboard",
            "sound": "Serum",
            **({"gain": gains[name]} if name in gains else {}),
            "notes": [{"beat": 0, "pitch": 60, "length": beats, "velocity": 100}],
        }
        for name in stems
    ]
    tmp_path.mkdir(parents=True, exist_ok=True)
    song_path = tmp_path / "song.yaml"
    song_path.write_text(yaml.safe_dump({"bpm": 60, "tracks": tracks}))

    song_dir = tmp_path / "renders" / "song"
    song_dir.mkdir(parents=True, exist_ok=True)
    for name, audio in stems.items():
        if audio is not None:
            write_wav(song_dir / f"{name}.wav", audio)
            stamp(song_path, name)
    return song_path


def stamp(song_path: Path, name: str, sound: str = SERUM_KEY) -> None:
    """Write the render stamp `render-song` would write for the track's stem as it is now, with
    the given sound key."""
    song = load_song(song_path)
    (track,) = (track for track in song.tracks if track.name == name)
    directory = song_dir(song_path)
    render.write_stamp(
        render.stamp_path(directory, name),
        render.stem_path(directory, name),
        render.track_key(song, track),
        sound,
        render.TrackStatus.RENDERED,
        -20.0,
    )


def run(song_path: Path) -> analyze.Report:
    return analyze_song(
        load_song(song_path),
        song_path,
        SPECS,
        song_path.parent / "renders",
        song_path.parent / "sounds",
    )


def cli_args(song_path: Path) -> list[str]:
    """Return `analyze-song` arguments for the song, with a plugin list naming only Serum."""
    plugins = song_path.parent / "plugins.toml"
    plugins.write_text('[[plugin]]\nname = "Serum"\npath = "/serum.vst3"\n')
    return [
        str(song_path),
        "--config",
        str(plugins),
        "--output-dir",
        str(song_path.parent / "renders"),
        "--sounds-dir",
        str(song_path.parent / "sounds"),
    ]


def run_main(song_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, object]:
    code = analyze.main(cli_args(song_path))
    capsys.readouterr()
    code_json = analyze.main([*cli_args(song_path), "--json"])
    assert code_json == code
    return code, json.loads(capsys.readouterr().out)


def at(data: object, *path: str | int) -> object:
    """Return the JSON value at a path of keys and indexes, asserting that each step exists."""
    for step in path:
        if isinstance(step, int):
            assert isinstance(data, list)
            data = data[step]
        else:
            assert isinstance(data, dict)
            data = data[step]
    return data


def items(data: object, *path: str | int) -> list[object]:
    """Return the JSON list at a path, asserting that it is a list."""
    value = at(data, *path)
    assert isinstance(value, list)
    return value


def song_dir(song_path: Path) -> Path:
    return song_path.parent / "renders" / "song"


def tracks_by_name(report: analyze.Report) -> dict[str, analyze.TrackReport]:
    return {track.name: track for track in report.tracks}


def test_mix_is_the_sample_sum_of_the_stems(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    lead = rng.uniform(-0.5, 0.5, (2, samples_for(1))).astype(np.float32)
    bass = rng.uniform(-0.5, 0.5, (2, samples_for(1))).astype(np.float32)
    lead[0, 0] = bass[0, 0] = 0.8
    song_path = make_song(tmp_path, {"lead": lead, "bass": bass})

    report = run(song_path)

    assert report.mix is not None
    mix_path = Path(report.mix.path)
    assert mix_path == song_dir(song_path).resolve() / "mix" / "mix.wav"
    with ReadableAudioFile(str(mix_path)) as mix:  # ty: ignore[no-matching-overload]
        assert (mix.samplerate, mix.num_channels, mix.frames) == (RATE, 2, samples_for(1))
        audio = mix.read(mix.frames)
    expected = (lead.astype(np.float64) + bass).astype(np.float32)
    assert np.array_equal(audio, expected)
    assert audio[0, 0] == pytest.approx(1.6)


def test_stray_wav_does_not_enter_the_mix(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.25)})
    write_wav(song_dir(song_path) / "removed.wav", constant(0.5))

    report = run(song_path)

    assert [track.name for track in report.tracks] == ["lead"]
    assert report.mix is not None
    assert np.array_equal(read_wav(Path(report.mix.path)), constant(0.25))


def test_ebu_tech_3341_case_1_measures_minus_23_lufs(tmp_path: Path) -> None:
    # Stereo 1000 Hz sine at -23.0 dBFS per channel, in phase, for 20 seconds.
    t = np.arange(samples_for(18)) / RATE
    tone = 10 ** (-23.0 / 20) * np.sin(2 * np.pi * 1000 * t)
    song_path = make_song(tmp_path, {"tone": np.stack([tone, tone])}, beats=18)
    assert samples_for(18) == 20 * RATE

    report = run(song_path)

    lufs = tracks_by_name(report)["tone"].integrated_lufs
    assert lufs == pytest.approx(-23.0, abs=0.1)


@pytest.mark.filterwarnings("error")
def test_silent_stem_reports_null_loudness_and_a_silent_defect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"quiet": constant(0.0)})

    code, printed = run_main(song_path, capsys)

    assert code == EXIT_SOME_FAILED
    (track,) = items(printed, "tracks")
    assert at(track, "integrated_lufs") is None
    assert at(track, "defects") == [Defect.SILENT]
    assert at(printed, "mix", "defects") == [Defect.SILENT]
    spectrogram = at(track, "spectrogram")
    assert isinstance(spectrogram, str)
    assert Path(spectrogram).read_bytes().startswith(PNG_SIGNATURE)

    def reject(constant_name: str) -> NoReturn:
        raise AssertionError(f"report.json holds {constant_name}")

    report_path = song_dir(song_path) / "analysis" / "report.json"
    assert json.loads(report_path.read_text(), parse_constant=reject) == printed


def test_silent_track_in_an_audible_mix_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.25), "quiet": constant(0.0)})

    code, printed = run_main(song_path, capsys)

    assert code == EXIT_SOME_FAILED
    assert [at(track, "defects") for track in items(printed, "tracks")] == [[], [Defect.SILENT]]
    assert at(printed, "mix", "defects") == []


def test_mix_over_full_scale_is_a_clipping_defect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.6), "bass": constant(0.6)})

    code, printed = run_main(song_path, capsys)

    assert code == EXIT_SOME_FAILED
    assert [at(track, "defects") for track in items(printed, "tracks")] == [[], []]
    assert at(printed, "mix", "defects") == [Defect.CLIPPING, Defect.HEADROOM]
    assert at(printed, "mix", "clipped_samples") == 2 * samples_for(1)


def test_stem_over_full_scale_is_not_a_defect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(1.2), "bass": constant(-0.6)})

    code, printed = run_main(song_path, capsys)

    assert code == EXIT_ALL_PASS
    lead, bass = items(printed, "tracks")
    assert at(lead, "clipped_samples") == 2 * samples_for(1)
    assert at(lead, "defects") == at(bass, "defects") == []
    assert at(printed, "mix", "clipped_samples") == 0
    assert at(printed, "mix", "defects") == []


def make_stale(song_path: Path, case: str) -> None:
    """Spoil the bass stem one way. A case that writes a new stem also stamps it, as a render
    would, so that each case reaches its own check."""
    stem = song_dir(song_path) / "bass.wav"
    match case:
        case "missing":
            stem.unlink()
        case "no_stamp":
            render.stamp_path(song_dir(song_path), "bass").unlink()
        case "stem_changed":
            write_wav(stem, constant(0.2))
            os.utime(stem, ns=(stem.stat().st_atime_ns, stem.stat().st_mtime_ns + 10**9))
        case "song_changed":
            data = yaml.safe_load(song_path.read_text())
            data["tracks"][1]["notes"][0]["pitch"] = 62
            song_path.write_text(yaml.safe_dump(data))
        case "wrong_length":
            write_wav(stem, constant(0.1, beats=2))
        case "mono":
            write_wav(stem, constant(0.1)[:1])
        case "unreadable":
            stem.write_bytes(b"not a wav file")
        case "truncated":
            stem.write_bytes(stem.read_bytes()[: stem.stat().st_size // 2])
        case "not_finite":
            audio = constant(0.1)
            audio[:, 1000:2000] = np.nan
            write_wav(stem, audio)
    if case not in {"missing", "no_stamp", "stem_changed", "song_changed"}:
        stamp(song_path, "bass")


@pytest.mark.parametrize(
    ("case", "status", "detail"),
    [
        ("missing", TrackStatus.MISSING_STEM, None),
        ("no_stamp", TrackStatus.STALE_STEM, "stem has no render stamp"),
        ("stem_changed", TrackStatus.STALE_STEM, "stem changed after it was rendered"),
        ("song_changed", TrackStatus.STALE_STEM, "song changed since this stem was rendered"),
        ("wrong_length", TrackStatus.STALE_STEM, f"samples, expected {samples_for(1)}"),
        ("mono", TrackStatus.STALE_STEM, "shorter than its audio data"),
        ("unreadable", TrackStatus.STALE_STEM, "shorter than its audio data"),
        ("truncated", TrackStatus.STALE_STEM, "shorter than its audio data"),
        ("not_finite", TrackStatus.STALE_STEM, "NaN or infinity"),
    ],
)
def test_unusable_stem_removes_earlier_results(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: str,
    status: TrackStatus,
    detail: str | None,
) -> None:
    song_path = make_song(
        tmp_path, {"lead": constant(0.1), "bass": constant(0.1)}, gains={"bass": -3}
    )
    assert run(song_path).mix is not None
    make_stale(song_path, case)

    code, printed = run_main(song_path, capsys)

    assert code == EXIT_SOME_FAILED
    lead, bass = items(printed, "tracks")
    assert at(lead, "name") == "lead"
    assert at(lead, "status") == TrackStatus.ANALYZED
    assert at(bass, "status") == status
    bass_detail = at(bass, "detail")
    if detail is None:
        assert bass_detail is None
    else:
        assert isinstance(bass_detail, str)
        assert detail in bass_detail
    assert at(bass, "gain_db") == -3.0
    assert at(bass, "peak_dbfs") is None
    assert at(bass, "integrated_lufs") is None
    assert at(bass, "bands_db") is None
    assert at(bass, "bar_dbfs") is None
    assert at(printed, "mix") is None
    assert not (song_dir(song_path) / "mix").exists()
    assert not (song_dir(song_path) / "analysis").exists()


def test_table_shows_each_track_and_a_mix_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": None}, gains={"bass": -3})

    analyze.main(cli_args(song_path))

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:2] == ["NAME", "STATUS"]
    assert [line.split() for line in lines[1:4]] == [
        ["lead", "analyzed", "0.0", "-20.0", *lines[1].split()[4:]],
        ["bass", "missing_stem", "-3.0", "-", "-", "-", "-"],
        ["[mix]", "not_written", "-", "-", "-", "-", "-"],
    ]


def test_track_named_mix_keeps_its_own_stem(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"mix": constant(0.25), "analysis": constant(0.125)})

    report = run(song_path)

    assert [track.status for track in report.tracks] == [TrackStatus.ANALYZED] * 2
    assert np.array_equal(read_wav(song_dir(song_path) / "mix.wav"), constant(0.25))
    assert np.array_equal(read_wav(song_dir(song_path) / "analysis.wav"), constant(0.125))
    assert report.mix is not None
    assert np.array_equal(read_wav(Path(report.mix.path)), constant(0.375))


def test_full_run_writes_spectrograms_and_leaves_no_temporary_files(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": constant(0.2)})

    report = run(song_path)

    assert report.mix is not None
    images = [track.spectrogram for track in report.tracks] + [report.mix.spectrogram]
    directory = song_dir(song_path).resolve()
    assert images == [
        str(directory / "analysis" / "lead.png"),
        str(directory / "analysis" / "bass.png"),
        str(directory / "mix" / "mix.png"),
    ]
    for image in images:
        assert image is not None
        assert Path(image).read_bytes().startswith(PNG_SIGNATURE)
    assert (directory / "analysis" / "report.json").is_file()
    # The lock file stays, because deleting it could let two runs hold different locks.
    leftovers = [path for path in tmp_path.rglob("*") if path.name.startswith(".")]
    assert leftovers == [directory / render.LOCK_NAME]


def test_analysis_never_loads_a_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("analysis loaded a plugin")

    for loader in ("load_plugin", "VST3Plugin", "AudioUnitPlugin"):
        monkeypatch.setattr(pedalboard, loader, refuse)
    assert not {"load_plugin", "VST3Plugin", "AudioUnitPlugin"} & set(vars(analyze))
    song_path = make_song(tmp_path, {"lead": constant(0.1)})

    code, _ = run_main(song_path, capsys)

    assert code == EXIT_ALL_PASS


def test_unreadable_song_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("bpm: -1\ntracks: []\n")

    assert analyze.main([str(tmp_path / "missing.yaml")]) == EXIT_BAD_CONFIG
    assert analyze.main([str(invalid)]) == EXIT_BAD_CONFIG
    assert "analyze-song:" in capsys.readouterr().err


def test_gain_turns_a_clipping_mix_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    unity = make_song(tmp_path / "unity", {"lead": constant(0.6), "bass": constant(0.6)})
    code, data = run_main(unity, capsys)
    assert code == EXIT_SOME_FAILED
    assert at(data, "mix", "defects") == [Defect.CLIPPING, Defect.HEADROOM]

    quiet = make_song(
        tmp_path / "quiet",
        {"lead": constant(0.6), "bass": constant(0.6)},
        gains={"lead": -6, "bass": -6},
    )
    code, data = run_main(quiet, capsys)
    assert code == EXIT_ALL_PASS
    assert at(data, "mix", "defects") == []
    mix_path = at(data, "mix", "path")
    assert isinstance(mix_path, str)
    mix = read_wav(Path(mix_path))
    assert np.allclose(mix, 0.6 * 10 ** (-6 / 20) * 2, rtol=1e-6)


def test_gain_edit_analyzes_without_a_new_render(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.4), "bass": constant(0.4)})
    assert run(song_path).mix is not None
    data = yaml.safe_load(song_path.read_text())
    for track in data["tracks"]:
        track["gain"] = -6
    song_path.write_text(yaml.safe_dump(data))
    info = song_path.stat()
    os.utime(song_path, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))

    report = run(song_path)

    assert analyze.exit_code(report) == EXIT_ALL_PASS
    assert report.mix is not None
    assert np.allclose(read_wav(Path(report.mix.path)), 0.4 * 10 ** (-6 / 20) * 2, rtol=1e-6)


def test_mix_is_the_gain_weighted_sum_of_the_stems(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    lead = rng.uniform(-0.5, 0.5, (2, samples_for(1))).astype(np.float32)
    bass = rng.uniform(-0.5, 0.5, (2, samples_for(1))).astype(np.float32)
    song_path = make_song(tmp_path, {"lead": lead, "bass": bass}, gains={"lead": -6})

    report = run(song_path)

    assert report.mix is not None
    expected = (lead.astype(np.float64) * 10 ** (-6 / 20) + bass.astype(np.float64)).astype(
        np.float32
    )
    assert np.array_equal(read_wav(Path(report.mix.path)), expected)


def test_track_measurements_ignore_gain_and_report_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stems: dict[str, np.ndarray | None] = {"lead": constant(0.5), "bass": constant(0.25)}
    plain = tracks_by_name(run(make_song(tmp_path / "plain", stems)))
    song_path = make_song(tmp_path / "gained", stems, gains={"lead": -12.5})

    _, data = run_main(song_path, capsys)

    reported = {at(track, "name"): track for track in items(data, "tracks")}
    assert at(reported["lead"], "gain_db") == -12.5
    assert at(reported["bass"], "gain_db") == 0.0
    for name in stems:
        assert at(reported[name], "peak_dbfs") == plain[name].peak_dbfs
        assert at(reported[name], "integrated_lufs") == plain[name].integrated_lufs
        assert at(reported[name], "bands_db") == plain[name].bands_db
        assert at(reported[name], "bar_dbfs") == plain[name].bar_dbfs


def test_missing_stem_still_reports_its_gain(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": None}, gains={"bass": 3})
    assert tracks_by_name(run(song_path))["bass"].gain_db == 3


def test_table_shows_gain_per_track_and_a_dash_for_the_mix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(
        tmp_path, {"lead": constant(0.1), "bass": constant(0.1)}, gains={"lead": -6.25}
    )

    analyze.main(cli_args(song_path))

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:4] == ["NAME", "STATUS", "GAIN", "DB"]
    assert [line.split()[:3] for line in lines[1:4]] == [
        ["lead", "analyzed", "-6.2"],
        ["bass", "analyzed", "0.0"],
        ["[mix]", "written", "-"],
    ]


def test_analysis_refuses_to_start_while_the_song_is_locked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1)})

    with render.song_lock(song_dir(song_path).resolve()):
        with pytest.raises(render.SongBusyError):
            run(song_path)
        assert analyze.main(cli_args(song_path)) == render.EXIT_SONG_BUSY
        assert capsys.readouterr().err == (
            "analyze-song: another render-song or analyze-song is running on song\n"
        )
    assert not (song_dir(song_path) / "history").exists()

    assert run(song_path).run == 1


def history(song_path: Path) -> Path:
    return song_dir(song_path).resolve() / "history"


def test_each_analysis_writes_the_next_history_record(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": constant(0.2)})

    runs = [run(song_path).run for _ in range(2)]
    render.clear_results(song_dir(song_path))
    runs.append(run(song_path).run)

    assert runs == [1, 2, 3]
    assert sorted(entry.name for entry in history(song_path).iterdir()) == ["1", "2", "3"]
    for number in runs:
        record = history(song_path) / str(number)
        assert sorted(entry.name for entry in record.iterdir()) == [
            "[mix].png",
            "bass.png",
            "lead.png",
            "report.json",
            "song.yaml",
        ]
        data = json.loads((record / "report.json").read_text())
        assert data["run"] == number
        images = [track["spectrogram"] for track in data["tracks"]] + [data["mix"]["spectrogram"]]
        assert images == [str(record / name) for name in ("lead.png", "bass.png", "[mix].png")]
        for image in images:
            assert Path(image).read_bytes().startswith(PNG_SIGNATURE)


def test_render_leaves_history_intact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1)})
    run(song_path)
    monkeypatch.setattr(render, "worker_command", lambda job_path: ["false"])

    render.render_song(
        load_song(song_path),
        "song",
        SPECS,
        song_path.parent / "renders",
        sounds_dir=song_path.parent / "sounds",
    )

    assert (history(song_path) / "1" / "report.json").is_file()
    assert run(song_path).run == 2


def test_failed_analysis_still_writes_a_record(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": None})

    report = run(song_path)

    assert analyze.exit_code(report) == EXIT_SOME_FAILED
    record = history(song_path) / "1"
    assert sorted(entry.name for entry in record.iterdir()) == ["report.json", "song.yaml"]
    assert json.loads((record / "report.json").read_text())["mix"] is None


def test_record_holds_the_song_and_its_digest(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": constant(0.2)})

    report = run(song_path)

    record_song = history(song_path) / "1" / "song.yaml"
    assert load_song(record_song) == load_song(song_path)
    assert report.song_sha256 == hashlib.sha256(record_song.read_bytes()).hexdigest()


def edit_song(song_path: Path, *path: str | int, value: object) -> None:
    """Set the song file's value at a path of keys and indexes."""
    data = yaml.safe_load(song_path.read_text())
    target = data
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value
    song_path.write_text(yaml.safe_dump(data))


def test_note_edit_changes_the_digest_and_gain_edit_keeps_track_keys(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": constant(0.2)})
    first = run(song_path)

    edit_song(song_path, "tracks", 0, "gain", value=-6)
    gained = run(song_path)
    edit_song(song_path, "tracks", 0, "notes", 0, "pitch", value=62)
    repitched = run(song_path)

    assert gained.song_sha256 != first.song_sha256
    assert [t.track_key for t in gained.tracks] == [t.track_key for t in first.tracks]
    assert repitched.song_sha256 not in {first.song_sha256, gained.song_sha256}
    assert repitched.tracks[0].track_key != first.tracks[0].track_key


def test_report_keys_are_the_stamp_keys(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": None})

    lead, bass = run(song_path).tracks

    stamp_data = render.read_stamp(render.stamp_path(song_dir(song_path), "lead"))
    assert stamp_data is not None
    assert (lead.track_key, lead.sound_key) == (stamp_data.track_key, stamp_data.sound_key)
    assert lead.sound_key == SERUM_KEY
    song = load_song(song_path)
    assert bass.track_key == render.track_key(song, song.tracks[1])
    assert bass.sound_key is None


def test_stem_of_another_sound_is_stale(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1)})
    stamp(song_path, "lead", sound="another sound key")

    (lead,) = run(song_path).tracks

    assert lead.status == TrackStatus.STALE_STEM
    assert lead.detail == "sound changed since this stem was rendered"


def test_stem_of_a_sound_that_no_longer_resolves_is_stale(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1)})
    edit_song(song_path, "tracks", 0, "sound", value="Gone")

    (lead,) = run(song_path).tracks

    assert lead.status == TrackStatus.STALE_STEM
    assert lead.sound_key is None
    assert lead.detail == "no plugin, saved sound, or preset named 'Gone'"


def sine(frequency: float, seconds: float, amplitude: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    tone = (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)
    return np.stack([tone, tone])


@pytest.mark.parametrize(("frequency", "band"), [(100.0, "bass"), (3000.0, "high_mid")])
def test_sine_band_level_leads_every_other_band(frequency: float, band: str) -> None:
    levels = analyze.bands_db(sine(frequency, 2.0, 0.5))

    assert list(levels) == list(analyze.BANDS)
    others = [level for name, level in levels.items() if name != band]
    assert levels[band] >= max(others) + 20


def test_bar_levels_follow_the_song_in_four_beat_spans(tmp_path: Path) -> None:
    # 120 bpm: a beat is 0.5 s, a bar 2 s, and the 8 beats of notes span two bars.
    audio = np.zeros((2, int((4.0 + 2.0) * RATE)), dtype=np.float32)
    audio[:, : 2 * RATE] = sine(1000.0, 2.0)
    song_path = make_song(tmp_path, {"lead": audio}, beats=8)
    edit_song(song_path, "bpm", value=120)
    stamp(song_path, "lead")

    report = run(song_path)

    (lead,) = report.tracks
    assert lead.status == TrackStatus.ANALYZED
    assert lead.bar_dbfs is not None
    assert len(lead.bar_dbfs) == 2
    assert lead.bar_dbfs[0] == pytest.approx(-3.01, abs=0.02)
    assert lead.bar_dbfs[1] == -120.0
    assert report.mix is not None
    assert report.mix.bar_dbfs == lead.bar_dbfs
    assert lead.bands_db is not None
    assert all(round(level, 2) == level for level in lead.bands_db.values())


@pytest.mark.parametrize(
    ("peak_dbfs", "defects"),
    [
        (-1.5, []),
        (-0.5, [Defect.HEADROOM]),
        (1.0, [Defect.CLIPPING, Defect.HEADROOM]),
    ],
)
def test_mix_above_the_ceiling_has_the_headroom_defect(
    tmp_path: Path, peak_dbfs: float, defects: list[Defect]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(10 ** (peak_dbfs / 20))})

    report = run(song_path)

    assert report.mix is not None
    assert report.mix.defects == defects
    assert report.tracks[0].defects == []
    assert (analyze.exit_code(report) == EXIT_ALL_PASS) == (not defects)


def test_spectrogram_has_a_log_axis_from_20_hz_to_20_khz_and_a_db_scale() -> None:
    figure = analyze.spectrogram_figure(sine(1000.0, 1.0), "lead")

    axes, colorbar = figure.axes
    assert axes.get_yscale() == "log"
    assert axes.get_ylim() == (20.0, 20000.0)
    assert axes.get_title() == "lead"
    assert (axes.get_xlabel(), axes.get_ylabel()) == ("Time (s)", "Frequency (Hz)")
    assert colorbar.get_ylabel() == "dB"
    # Only the bins at or above 20 Hz are drawn.
    (mesh,) = axes.collections
    bins = np.fft.rfftfreq(analyze.SPECGRAM_NFFT, 1 / RATE)
    assert np.asarray(mesh.get_array()).shape[0] == np.count_nonzero(bins >= 20.0)


def test_record_number_follows_the_largest_existing_record(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1)})
    (history(song_path) / "7").mkdir(parents=True)
    (history(song_path) / "notes").mkdir()

    assert run(song_path).run == 8


@pytest.mark.parametrize("bpm", [90, 100, 140])
@pytest.mark.parametrize("bars", [1, 3, 8])
def test_bar_count_ends_at_the_last_beat(bpm: float, bars: int) -> None:
    note = Note(beat=0, pitch=60, length=4 * bars, velocity=100)
    song = Song(bpm=bpm, tracks=(Track("lead", "pedalboard", "Serum", (note,)),))
    audio = np.full(
        (2, render.expected_samples(render.song_duration_s(song))), 0.5, dtype=np.float32
    )

    assert len(analyze.measure(audio, song).bar_dbfs) == bars
