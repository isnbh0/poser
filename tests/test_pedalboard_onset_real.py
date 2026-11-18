"""Collect Surge showcase onset evidence; diagnostic, not an onset acceptance test.

No editor, musical-source edits, retries, or onset pass/fail policy: even a missed first note
must finish the collection. Each replicate loads its own plugin in a fresh isolated worker.
"""

import hashlib
import json
import os
import platform
import sys
from dataclasses import dataclass, replace
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from poser.adapters.isolation import run_isolated
from poser.adapters.pedalboard.instrument import PedalboardInstrument, payload_state, plugin_version
from poser.adapters.pedalboard.state import decode_state
from poser.core.audio import (
    NUM_CHANNELS,
    SAMPLE_RATE,
    Audio,
    measure_peak_dbfs,
    read_raw_audio,
    write_raw_audio,
)
from poser.core.graph import BUFFER_SIZE
from poser.core.jsonvalue import JsonValue
from poser.core.performance import NoteOff, NoteOn, Performance, perform, performance_to_data
from poser.core.ports import ResolvedSound
from poser.core.rendering import late_start_s
from poser.core.sounds import SoundRef
from poser.reference_setups.surge.showcase import build

if TYPE_CHECKING:
    from pedalboard import AudioUnitPlugin, VST3Plugin

pytestmark = pytest.mark.real_plugins


@dataclass(frozen=True)
class _Variant:
    name: str
    before_state_s: float = 0.0
    after_state_s: float = 0.0
    delay_s: float = 0.0
    default_patch: bool = False


VARIANTS = (
    _Variant("production"),
    _Variant("state_then_one_block", after_state_s=BUFFER_SIZE / SAMPLE_RATE),
    _Variant("state_then_silence_0.1s", after_state_s=0.1),
    _Variant("state_then_silence_1.0s", after_state_s=1.0),
    _Variant("silence_0.1s_then_state", before_state_s=0.1),
    _Variant("production_delayed_0.1s", delay_s=0.1),
    _Variant("default_patch", default_patch=True),
)


def showcase_onset_worker(job: JsonValue) -> JsonValue:
    """Importable isolation target; change only loading/priming inside this child process."""
    from poser.adapters.pedalboard import worker

    assert isinstance(job, dict)
    render_job = job["render_job"]
    before_s, after_s = job["before_state_s"], job["after_state_s"]
    assert isinstance(render_job, dict)
    assert isinstance(before_s, int | float)
    assert not isinstance(before_s, bool)
    assert isinstance(after_s, int | float)
    assert not isinstance(after_s, bool)
    assert before_s >= 0
    assert after_s >= 0
    assert not (before_s and after_s), "each diagnostic control has at most one prime"
    prime_path = job.get("prime_audio_path")
    assert prime_path is None or isinstance(prime_path, str)
    if before_s or after_s:
        if prime_path is None:
            audio_path = render_job["audio_path"]
            assert isinstance(audio_path, str)
            prime_path = str(Path(audio_path).with_suffix(".prime.npy"))
    else:
        assert prime_path is None
    prime_metadata: dict[str, JsonValue] | None = None
    original_load = worker.load

    def load_with_prime(
        path: str, plugin_name: str | None, state: bytes | None
    ) -> "VST3Plugin | AudioUnitPlugin":
        nonlocal prime_metadata
        plugin = original_load(path, plugin_name, None if before_s else state)
        primed = plugin(
            [],
            duration=worker.render_duration_s(round((before_s or after_s) * SAMPLE_RATE)),
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            buffer_size=BUFFER_SIZE,
            reset=False,
        )
        assert isinstance(prime_path, str)
        # Persist before another plugin call can reuse its output buffer.
        write_raw_audio(Path(prime_path), primed)
        prime_metadata = dict[str, JsonValue](
            audio_path=prime_path,
            frames=int(primed.shape[1]),
            peak_dbfs=measure_peak_dbfs(primed),
        )
        if before_s:
            assert state is not None
            plugin.raw_state = state
        return plugin

    # Baseline, delayed-event, and default-patch controls use production literally unchanged.
    with pytest.MonkeyPatch.context() as child_patch:
        if before_s or after_s:
            child_patch.setattr(worker, "load", load_with_prime)
        result = worker.render_job(render_job)
    return {"render_result": result, "worker_pid": os.getpid(), "priming": prime_metadata}


def test_surge_showcase_onset_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Save seven controls times three workers, asserting audio validity but never onset timing."""
    from helpers import real_composition

    # run_isolated imports this module by name; the child's -P ignores pytest's sys.path edits.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parent))
    composition = real_composition(tmp_path)
    song = build()
    (bass,) = [track for track in song.tracks if track.name == "bass"]
    assert str(bass.sound_ref) == "surge:presets/Basses/Digibass"
    instrument = composition.instruments().get(bass.sound_ref.instrument)
    assert isinstance(instrument, PedalboardInstrument), (
        "root poser.toml must configure the real Surge Pedalboard instrument as 'surge'"
    )
    sound = instrument.resolve(bass.sound_ref)
    assert isinstance(sound, ResolvedSound), f"cannot resolve showcase Digibass: {sound}"
    state = payload_state(sound.payload)
    assert state is not None, "showcase Digibass must resolve to preset state"
    default = instrument.resolve(SoundRef(bass.sound_ref.instrument))
    assert isinstance(default, ResolvedSound), default
    assert payload_state(default.payload) is None
    base_job = instrument._job(state)
    plugin_path = base_job["path"]
    assert isinstance(plugin_path, str)

    full = perform(song.graph(), bass)
    frames = 4 * SAMPLE_RATE
    prefix = Performance(frames, tuple(event for event in full.events if event.sample < frames))
    first, second = [event for event in prefix.events if isinstance(event, NoteOn)][:2]
    first_off = next(
        event
        for event in prefix.events
        if isinstance(event, NoteOff) and event.pitch == first.pitch and event.sample > first.sample
    )
    second_off = next(
        event
        for event in prefix.events
        if isinstance(event, NoteOff)
        and event.pitch == second.pitch
        and event.sample > second.sample
    )
    windows = (
        ("first_note", first.sample, first_off.sample),
        ("gap", first_off.sample, second.sample),
        ("second_note", second.sample, second_off.sample),
    )
    runs: list[JsonValue] = []
    summary: dict[str, JsonValue] = {
        "purpose": "diagnostic evidence, not onset acceptance",
        "platform": platform.platform(),
        "sys_platform": sys.platform,
        "python": sys.version,
        "python_executable": sys.executable,
        "pedalboard_version": metadata.version("pedalboard"),
        "plugin_path": plugin_path,
        "plugin_name": base_job["plugin_name"],
        "plugin_version": plugin_version(Path(plugin_path)),
        "preset": str(sound.ref),
        "state_sha256": hashlib.sha256(decode_state(state)).hexdigest(),
        "state_bytes": len(decode_state(state)),
        "state_source": "public resolve payload; no editor or extra capture_state processing",
        "sample_rate": SAMPLE_RATE,
        "num_channels": NUM_CHANNELS,
        "buffer_size": BUFFER_SIZE,
        "settle_s": 0.0,
        "performance": performance_to_data(prefix),
        "windows": {
            name: {
                "start_sample": start,
                "end_sample_exclusive": end,
                "start_s": start / SAMPLE_RATE,
                "end_s": end / SAMPLE_RATE,
            }
            for name, start, end in windows
        },
        "first_crossing_definition": "first sample where max absolute channel exceeds threshold",
        "peak_floor_dbfs": -120.0,
        "runs": runs,
    }
    summary_path = tmp_path / "summary.json"
    print(f"Surge onset summary: {summary_path}", flush=True)
    for variant in VARIANTS:
        delay = round(variant.delay_s * SAMPLE_RATE)
        sent = Performance(
            prefix.samples + delay,
            tuple(replace(event, sample=event.sample + delay) for event in prefix.events),
        )
        for replicate in range(1, 4):
            stem = f"{variant.name}-{replicate}"
            raw_path = tmp_path / f"{stem}.npy"
            prime_path = tmp_path / f"{stem}.prime.npy"
            prime_frames = round((variant.before_state_s or variant.after_state_s) * SAMPLE_RATE)
            render_job = instrument._job(None if variant.default_patch else state)
            render_job.update(
                performance=performance_to_data(sent), audio_path=str(raw_path), settle_s=0.0
            )
            result = run_isolated(
                showcase_onset_worker,
                {
                    "render_job": render_job,
                    "before_state_s": variant.before_state_s,
                    "after_state_s": variant.after_state_s,
                    "prime_audio_path": str(prime_path) if prime_frames else None,
                },
                120,
            )
            assert isinstance(result, dict), (stem, result)
            assert result.get("render_result") == {"status": "ok"}, (stem, result)
            pid = result.get("worker_pid")
            assert isinstance(pid, int), result
            assert not isinstance(pid, bool), result
            prime_metadata = result.get("priming")
            if prime_frames:
                assert isinstance(prime_metadata, dict), (stem, result)
                assert prime_metadata["audio_path"] == str(prime_path)
                assert prime_metadata["frames"] == prime_frames
                primed = read_raw_audio(prime_path, prime_frames)
                assert isinstance(primed, Audio), (stem, primed)
                assert primed.frames == prime_frames
                assert np.isfinite(primed.samples).all()
                assert prime_metadata["peak_dbfs"] == measure_peak_dbfs(primed.samples)
            else:
                assert prime_metadata is None, (stem, result)
                assert not prime_path.exists()
            raw = read_raw_audio(raw_path, sent.samples)
            assert isinstance(raw, Audio), (stem, raw)
            assert raw.frames == sent.samples
            assert np.isfinite(raw.samples).all()
            pre_first_note_peak = measure_peak_dbfs(raw.samples[:, :delay]) if delay else None
            audio_path = raw_path
            audio = raw
            if delay:
                # Preserve the extended raw render as well as the aligned four-second probe.
                audio_path = tmp_path / f"{stem}.cropped.npy"
                write_raw_audio(audio_path, raw.samples[:, delay:])
                cropped = read_raw_audio(audio_path, frames)
                assert isinstance(cropped, Audio), (stem, cropped)
                audio = cropped
            assert audio.frames == frames
            assert np.isfinite(audio.samples).all()
            magnitude = np.abs(audio.samples).max(axis=0)
            crossings: dict[str, JsonValue] = {}
            for threshold in (-100, -80, -60, -40):
                above = magnitude > 10.0 ** (threshold / 20.0)
                sample = int(np.argmax(above)) if above.any() else None
                crossings[str(threshold)] = {
                    "sample": sample,
                    "seconds": None if sample is None else sample / SAMPLE_RATE,
                }
            row: dict[str, JsonValue] = {
                "variant": variant.name,
                "replicate": replicate,
                "worker_pid": pid,
                "state_sha256": None if variant.default_patch else summary["state_sha256"],
                "before_state_s": variant.before_state_s,
                "after_state_s": variant.after_state_s,
                "event_delay_samples": delay,
                "priming": prime_metadata,
                "raw_pre_first_note_peak_dbfs": pre_first_note_peak,
                "sent_performance": performance_to_data(sent),
                "raw_audio_path": str(raw_path),
                "audio_path": str(audio_path),
                "raw_frames": raw.frames,
                "frames": audio.frames,
                "first_crossing_dbfs": crossings,
                "peak_dbfs": measure_peak_dbfs(audio.samples),
                "window_peak_dbfs": {
                    name: measure_peak_dbfs(audio.samples[:, start:end])
                    for name, start, end in windows
                },
                "core_late_start_s": late_start_s(audio.samples, prefix),
            }
            runs.append(row)
            # Keep complete evidence after every successful run, including on the broken baseline.
            summary_path.write_text(
                json.dumps(summary, indent=2, allow_nan=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            print(
                json.dumps(
                    {key: value for key, value in row.items() if key != "sent_performance"},
                    allow_nan=False,
                ),
                flush=True,
            )
    assert len(runs) == 21
    print(f"Surge onset summary: {summary_path}", flush=True)
