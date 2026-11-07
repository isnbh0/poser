"""Tests that write stems directly and load no plugins."""

import json
import os
import time
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
import pedalboard
import pytest
import yaml
from pedalboard.io import ReadableAudioFile, WriteableAudioFile

from vsti_composer import analyze
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


def make_song(tmp_path: Path, stems: dict[str, np.ndarray | None], beats: float = 1) -> Path:
    """Write a 60 bpm song whose tracks each hold one note, then write the given stems.

    The song file is dated in the past, so freshly written stems are newer than it. A stem of
    None is left unwritten.
    """
    tracks = [
        {
            "name": name,
            "engine": "pedalboard",
            "sound": "Serum",
            "notes": [{"beat": 0, "pitch": 60, "length": beats, "velocity": 100}],
        }
        for name in stems
    ]
    song_path = tmp_path / "song.yaml"
    song_path.write_text(yaml.safe_dump({"bpm": 60, "tracks": tracks}))
    past = time.time() - 100
    os.utime(song_path, (past, past))

    song_dir = tmp_path / "renders" / "song"
    song_dir.mkdir(parents=True, exist_ok=True)
    for name, audio in stems.items():
        if audio is not None:
            write_wav(song_dir / f"{name}.wav", audio)
    return song_path


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
    stem = song_dir(song_path) / "bass.wav"
    match case:
        case "missing":
            stem.unlink()
        case "wrong_length":
            write_wav(stem, constant(0.1, beats=2))
        case "mono":
            write_wav(stem, constant(0.1)[:1])
        case "older_than_song":
            past = song_path.stat().st_mtime - 10
            os.utime(stem, (past, past))
        case "unreadable":
            stem.write_bytes(b"not a wav file")
        case "truncated":
            stem.write_bytes(stem.read_bytes()[: stem.stat().st_size // 2])
        case "not_finite":
            audio = constant(0.1)
            audio[:, 1000:2000] = np.nan
            write_wav(stem, audio)


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("missing", TrackStatus.MISSING_STEM),
        ("wrong_length", TrackStatus.STALE_STEM),
        ("mono", TrackStatus.STALE_STEM),
        ("older_than_song", TrackStatus.STALE_STEM),
        ("unreadable", TrackStatus.STALE_STEM),
        ("truncated", TrackStatus.STALE_STEM),
        ("not_finite", TrackStatus.STALE_STEM),
    ],
)
def test_unusable_stem_removes_earlier_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str, status: TrackStatus
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": constant(0.1)})
    assert run(song_path).mix is not None
    make_stale(song_path, case)

    code, printed = run_main(song_path, capsys)

    assert code == EXIT_SOME_FAILED
    lead, bass = printed["tracks"]
    assert lead["name"] == "lead"
    assert lead["status"] == TrackStatus.ANALYZED
    assert bass["status"] == status
    assert bass["peak_dbfs"] is None
    assert bass["integrated_lufs"] is None
    assert printed["mix"] is None
    assert not (song_dir(song_path) / "mix").exists()
    assert not (song_dir(song_path) / "analysis").exists()


def test_table_shows_each_track_and_a_mix_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    song_path = make_song(tmp_path, {"lead": constant(0.1), "bass": None})

    analyze.main([str(song_path), "--output-dir", str(song_path.parent / "renders")])

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:2] == ["NAME", "STATUS"]
    assert [line.split()[:2] for line in lines[1:4]] == [
        ["lead", "analyzed"],
        ["bass", "missing_stem"],
        ["[mix]", "not_written"],
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
