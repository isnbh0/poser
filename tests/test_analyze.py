"""Tests that write stems directly and load no plugins."""

import json
import os
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
import pedalboard
import pytest
import yaml
from pedalboard.io import ReadableAudioFile, WriteableAudioFile

from vsti_composer import analyze, render
from vsti_composer.analyze import Defect, TrackStatus, analyze_song
from vsti_composer.plugin_check import EXIT_ALL_PASS, EXIT_BAD_CONFIG, EXIT_SOME_FAILED
from vsti_composer.song import load_song

RATE = 48000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


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


def stamp(song_path: Path, name: str) -> None:
    """Write the render stamp `render-song` would write for the track's stem as it is now."""
    song = load_song(song_path)
    (track,) = (track for track in song.tracks if track.name == name)
    directory = song_dir(song_path)
    render.write_stamp(
        render.stamp_path(directory, name),
        render.stem_path(directory, name),
        render.track_key(song, track),
        "any sound key",
        render.TrackStatus.RENDERED,
        -20.0,
    )


def run(song_path: Path) -> analyze.Report:
    return analyze_song(load_song(song_path), song_path, song_path.parent / "renders")


def run_main(song_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    code = analyze.main([str(song_path), "--output-dir", str(song_path.parent / "renders")])
    capsys.readouterr()
    code_json = analyze.main(
        [str(song_path), "--output-dir", str(song_path.parent / "renders"), "--json"]
    )
    assert code_json == code
    return code, json.loads(capsys.readouterr().out)


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
    (track,) = printed["tracks"]
    assert track["integrated_lufs"] is None
    assert track["defects"] == [Defect.SILENT]
    assert printed["mix"]["defects"] == [Defect.SILENT]
    assert Path(track["spectrogram"]).read_bytes().startswith(PNG_SIGNATURE)

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
    assert [track["defects"] for track in printed["tracks"]] == [[], [Defect.SILENT]]
    assert printed["mix"]["defects"] == []


def test_mix_over_full_scale_is_a_clipping_defect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.6), "bass": constant(0.6)})

    code, printed = run_main(song_path, capsys)

    assert code == EXIT_SOME_FAILED
    assert [track["defects"] for track in printed["tracks"]] == [[], []]
    assert printed["mix"]["defects"] == [Defect.CLIPPING]
    assert printed["mix"]["clipped_samples"] == 2 * samples_for(1)


def test_stem_over_full_scale_is_not_a_defect(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(1.2), "bass": constant(-0.6)})

    code, printed = run_main(song_path, capsys)

    assert code == EXIT_ALL_PASS
    lead, bass = printed["tracks"]
    assert lead["clipped_samples"] == 2 * samples_for(1)
    assert lead["defects"] == bass["defects"] == []
    assert printed["mix"]["clipped_samples"] == 0
    assert printed["mix"]["defects"] == []


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
    lead, bass = printed["tracks"]
    assert lead["name"] == "lead"
    assert lead["status"] == TrackStatus.ANALYZED
    assert bass["status"] == status
    assert (bass["detail"] is None) if detail is None else (detail in bass["detail"])
    assert bass["gain_db"] == -3.0
    assert bass["peak_dbfs"] is None
    assert bass["integrated_lufs"] is None
    assert printed["mix"] is None
    assert not (song_dir(song_path) / "mix").exists()
    assert not (song_dir(song_path) / "analysis").exists()


def test_table_shows_each_track_and_a_mix_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": None}, gains={"bass": -3})

    analyze.main([str(song_path), "--output-dir", str(song_path.parent / "renders")])

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
    assert not [path for path in tmp_path.rglob("*") if path.name.startswith(".")]


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
    assert data["mix"]["defects"] == [Defect.CLIPPING]

    quiet = make_song(
        tmp_path / "quiet",
        {"lead": constant(0.6), "bass": constant(0.6)},
        gains={"lead": -6, "bass": -6},
    )
    code, data = run_main(quiet, capsys)
    assert code == EXIT_ALL_PASS
    assert data["mix"]["defects"] == []
    mix = read_wav(Path(data["mix"]["path"]))
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

    reported = {track["name"]: track for track in data["tracks"]}
    assert reported["lead"]["gain_db"] == -12.5
    assert reported["bass"]["gain_db"] == 0.0
    for name in stems:
        assert reported[name]["peak_dbfs"] == plain[name].peak_dbfs
        assert reported[name]["integrated_lufs"] == plain[name].integrated_lufs


def test_missing_stem_still_reports_its_gain(tmp_path: Path) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": None}, gains={"bass": 3})
    assert tracks_by_name(run(song_path))["bass"].gain_db == 3


def test_table_shows_gain_per_track_and_a_dash_for_the_mix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(
        tmp_path, {"lead": constant(0.1), "bass": constant(0.1)}, gains={"lead": -6.25}
    )

    analyze.main([str(song_path), "--output-dir", str(song_path.parent / "renders")])

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:4] == ["NAME", "STATUS", "GAIN", "DB"]
    assert [line.split()[:3] for line in lines[1:4]] == [
        ["lead", "analyzed", "-6.2"],
        ["bass", "analyzed", "0.0"],
        ["[mix]", "written", "-"],
    ]
