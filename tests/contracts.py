"""Contract checks that every adapter of a port must pass.

Every audio a suite passes is at least TAIL_S long, the precondition of measurers and visualizers.
"""

import dataclasses
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

from poser.adapters.pedalboard.formats import PresetError, PresetFormat
from poser.core.audio import (
    NUM_CHANNELS,
    SAMPLE_RATE,
    TAIL_S,
    Audio,
    read_raw_audio,
)
from poser.core.graph import Graph
from poser.core.names import Identifier, SongId, Suffix, TrackName
from poser.core.performance import PitchSupport, perform
from poser.core.ports import (
    AudioFileError,
    AudioFiles,
    Captured,
    Instrument,
    Measurer,
    Mixer,
    MixInput,
    RecordWriter,
    RenderRequest,
    RenderStore,
    ResolvedSound,
    SongBusyError,
    SongFormat,
    SongStore,
    SoundCapture,
    SoundFailure,
    SoundListing,
    StemMeta,
    Visualizer,
)
from poser.core.report import BANDS, Levels
from poser.core.song import Note, Song, SongError, Track
from poser.core.sounds import SoundRef
from poser.core.statuses import SoundFailureStatus, StemStatus

CONTRACT_FRAMES = int(TAIL_S * SAMPLE_RATE)


def check_song_format_contract(fmt: object, sample_song: Song, tmp_path: Path) -> None:
    """Check a SongFormat: its suffixes, its failures, and, if it writes songs, a round trip."""
    assert isinstance(fmt, SongFormat)
    assert fmt.suffixes
    for suffix in fmt.suffixes:
        assert suffix == suffix.lower()
    unreadable = tmp_path / f"unreadable{fmt.suffixes[0]}"
    unreadable.write_text("this is not a song: [\n")
    with pytest.raises(SongError):
        fmt.load(unreadable)
    with pytest.raises(SongError):
        fmt.load(tmp_path / f"missing{fmt.suffixes[0]}")
    if isinstance(fmt, RecordWriter):
        written = tmp_path / f"written{fmt.suffix}"
        written.write_text(fmt.dump(sample_song))
        assert fmt.load(written) == sample_song


def check_audio_files_contract(files: object, tmp_path: Path) -> None:
    """Check an AudioFiles adapter: round trips, temporary files, and read failures."""
    assert isinstance(files, AudioFiles)
    frames = 1000
    ramp = np.linspace(-1.5, 1.5, NUM_CHANNELS * frames, dtype=np.float32)
    audio = Audio(ramp.reshape(NUM_CHANNELS, frames))

    directory = tmp_path / "stems"
    directory.mkdir()
    path = directory / f"track{files.suffix}"
    files.write(path, audio)
    assert sorted(directory.iterdir()) == [path]
    read = files.read(path, frames)
    assert np.array_equal(read.samples, audio.samples)
    assert float(np.max(np.abs(read.samples))) > 1.0

    files.write(path, audio)  # Replacing an existing file also leaves nothing else behind.
    assert sorted(directory.iterdir()) == [path]

    absent = tmp_path / "absent"
    with pytest.raises(OSError):  # noqa: PT011 -- any OSError is the contract.
        files.write(absent / f"track{files.suffix}", audio)
    assert not absent.exists()

    with pytest.raises(AudioFileError):
        files.read(directory / f"missing{files.suffix}", frames)
    with pytest.raises(AudioFileError):
        files.read(path, frames + 1)


def constant_audio(amplitude: float, frames: int = CONTRACT_FRAMES) -> Audio:
    return Audio(np.full((NUM_CHANNELS, frames), amplitude, dtype=np.float32))


def check_instrument_contract(
    instrument: object, ref: SoundRef, tmp_path: Path, timeout_s: float = 120.0
) -> None:
    """Check an Instrument: its listing resolves, identities repeat, and a render is usable."""
    assert isinstance(instrument, Instrument)
    assert isinstance(instrument.adapter_id, Identifier)
    assert isinstance(instrument.adapter_version, str)
    assert instrument.adapter_version
    assert isinstance(instrument.pitches, PitchSupport)
    listing = instrument.sounds()
    assert isinstance(listing, SoundListing)
    for entry in listing.entries:
        resolved = instrument.resolve(entry.ref)
        unknown = (
            isinstance(resolved, SoundFailure)
            and resolved.status is SoundFailureStatus.UNKNOWN_SOUND
        )
        assert not unknown, f"listed {entry.ref} does not resolve: {resolved}"

    first = instrument.resolve(ref)
    second = instrument.resolve(ref)
    assert isinstance(first, ResolvedSound), first
    assert isinstance(second, ResolvedSound), second
    assert first.identity == second.identity

    notes = (Note(beat=0, pitch=60, length=1, velocity=100), Note(1, 64, 1, 100))
    song = Song(bpm=120, tracks=(Track("contract", str(ref), notes),))
    performance = perform(song.graph(), song.tracks[0])
    path = tmp_path / "contract-render.npy"
    failure = instrument.render(RenderRequest(first, performance, path), timeout_s)
    assert failure is None, failure
    audio = read_raw_audio(path, performance.samples)
    assert isinstance(audio, Audio), audio


def check_sound_capture_contract(
    make: Callable[[Path], object],
    make_captured: Callable[[object], Captured],
    tmp_path: Path,
) -> None:
    """Check SoundCapture.store on instruments from `make(directory)`.

    `make` is called with an empty directory, and sometimes again with one it was given before:
    instances on one directory share their saved sounds, as two processes on one workspace do.
    `make_captured(instrument)` supplies data `store` accepts, since a real `capture` needs the
    owner.
    """
    made = iter(range(1000))

    def new_directory() -> Path:
        directory = tmp_path / f"instrument-{next(made)}"
        directory.mkdir(parents=True)
        return directory

    def build(directory: Path) -> SoundCapture:
        instrument = make(directory)
        assert isinstance(instrument, Instrument)
        assert isinstance(instrument, SoundCapture)
        return instrument

    def resolve(instrument: SoundCapture, ref: SoundRef) -> object:
        assert isinstance(instrument, Instrument)
        return instrument.resolve(ref)

    def store(instrument: SoundCapture, *, replace: bool = False) -> SoundRef | str:
        captured = make_captured(instrument)
        return instrument.store(name, "a contract sound", captured, replace=replace)

    def race(instruments: list[SoundCapture]) -> None:
        barrier = threading.Barrier(len(instruments))
        results: list[SoundRef | str] = []

        def save(instrument: SoundCapture) -> None:
            barrier.wait()
            results.append(store(instrument))

        threads = [threading.Thread(target=save, args=(each,)) for each in instruments]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        kinds = sorted(type(result).__name__ for result in results)
        assert kinds == ["SoundRef", "str"], f"two racing saves of one name gave {results}"

    name = Identifier("contract-sound")

    instrument = build(new_directory())
    assert instrument.check_name(name, replace=False) is None
    ref = store(instrument)
    assert isinstance(ref, SoundRef), ref
    assert isinstance(resolve(instrument, ref), ResolvedSound)

    # An instrument that already read its sources still finds the new sound.
    instrument = build(new_directory())
    assert isinstance(instrument, Instrument)
    instrument.sounds()
    ref = store(instrument)
    assert isinstance(ref, SoundRef), ref
    assert isinstance(resolve(instrument, ref), ResolvedSound)
    assert ref in [entry.ref for entry in instrument.sounds().entries]

    # check_name passing earlier does not let store overwrite a sound saved since.
    instrument = build(new_directory())
    assert instrument.check_name(name, replace=False) is None
    assert isinstance(store(instrument), SoundRef)
    second = store(instrument)
    assert isinstance(second, str), f"store saved over an existing sound without replace: {second}"
    assert isinstance(store(instrument, replace=True), SoundRef)

    # The same holds when another instance on the same directory saved the sound.
    directory = new_directory()
    first, other = build(directory), build(directory)
    assert first.check_name(name, replace=False) is None
    assert isinstance(store(other), SoundRef)
    late = store(first)
    assert isinstance(late, str), f"store saved over another instance's sound: {late}"
    later = build(directory)
    assert later.check_name(name, replace=False) is not None, "check_name missed a saved sound"
    assert isinstance(store(later), str), "store saved over a sound saved before it was built"

    instrument = build(new_directory())
    race([instrument, instrument])
    directory = new_directory()
    race([build(directory), build(directory)])


def check_preset_format_contract(fmt: object, sample_path: Path, tmp_path: Path) -> None:
    """Check a Pedalboard PresetFormat: its suffix, a state from a sample, and its failures.

    Every failure must be a PresetError, so a broken file never escapes as another exception.
    """
    assert isinstance(fmt, PresetFormat)
    assert isinstance(fmt.suffix, Suffix)
    state = fmt.read_state(sample_path)
    assert isinstance(state, bytes), f"read_state returned a {type(state).__name__}"
    assert state, "read_state returned an empty state"
    description = fmt.describe(sample_path)
    assert description is None or (isinstance(description, str) and description)

    empty = tmp_path / f"empty{fmt.suffix}"
    empty.write_bytes(b"")
    garbage = tmp_path / f"garbage{fmt.suffix}"
    garbage.write_bytes(b"\x00\xffnot a preset of any kind\n" * 4)
    for path in (tmp_path / f"missing{fmt.suffix}", empty, garbage):
        try:
            fmt.read_state(path)
        except PresetError:
            continue
        except Exception as error:
            message = f"read_state of {path.name} raised {type(error).__name__}: {error}"
            raise AssertionError(message) from error
        raise AssertionError(f"read_state of {path.name} raised nothing")


def check_record_writer_contract(writer: object, sample_song: Song | Graph) -> None:
    """Check a RecordWriter: its suffix, and one non-empty text for equal songs or graphs."""
    assert isinstance(writer, RecordWriter)
    assert isinstance(writer.suffix, Suffix)
    text = writer.dump(sample_song)
    assert isinstance(text, str)
    assert text
    assert writer.dump(sample_song) == text, "dump gave two texts for one song"
    assert writer.dump(dataclasses.replace(sample_song)) == text, (
        "dump gave two texts for equal songs"
    )


class _InterruptedError(Exception):
    """Raised inside a store's context to stand in for a failure mid-write."""


KEY_A, KEY_B, KEY_C = ("a" * 64, "b" * 64, "c" * 64)


def _interrupt_unit(store: SongStore, key: str) -> None:
    with store.write_unit(key) as draft:
        draft.stem_path(0).write_bytes(b"partial")
        draft.describe(0, StemStatus.RENDERED, -6.0)
        raise _InterruptedError


def _interrupt_record(store: SongStore, suffix: Suffix) -> None:
    with store.write_record("interrupted", suffix, latest=True) as record:
        record.path("report.json").write_text("partial", encoding="utf-8")
        raise _InterruptedError


def check_render_store_contract(make: Callable[[Path], object], tmp_path: Path) -> None:
    """Check a RenderStore built by `make(root)` and its song stores."""
    renders = make(tmp_path / "renders")
    assert isinstance(renders, RenderStore)
    suffix = renders.stem_suffix
    assert isinstance(suffix, Suffix)
    store = renders.song(SongId("songs/contract.yaml"))
    assert isinstance(store, SongStore)

    # A unit is found only once it publishes, with its stems in order.
    assert store.find(KEY_A) is None
    with store.write_unit(KEY_A) as draft:
        for index, data in enumerate((b"first stem", b"second stem")):
            path = draft.stem_path(index)
            assert path.parent.is_dir()
            path.write_bytes(data)
            draft.describe(index, StemStatus.RENDERED if index == 0 else StemStatus.SILENT, -6.0)
        assert store.find(KEY_A) is None, "a unit was found before it published"
    unit = store.find(KEY_A)
    assert unit is not None, "a published unit was not found"
    assert unit.stems() == (
        StemMeta(StemStatus.RENDERED, -6.0),
        StemMeta(StemStatus.SILENT, -6.0),
    )
    assert unit.stem_path(0).read_bytes() == b"first stem"
    assert unit.stem_path(1).read_bytes() == b"second stem"

    # An exception publishes nothing.
    with pytest.raises(_InterruptedError):
        _interrupt_unit(store, KEY_B)
    assert store.find(KEY_B) is None, "an exception left a unit"
    store.sweep()

    # Without replace, an existing unit is kept; with it, the unit is replaced.
    with store.write_unit(KEY_A) as draft:
        draft.stem_path(0).write_bytes(b"other")
        draft.describe(0, StemStatus.RENDERED, -1.0)
    kept = store.find(KEY_A)
    assert kept is not None
    assert kept.stem_path(0).read_bytes() == b"first stem", "write_unit replaced a unit"
    with store.write_unit(KEY_A, replace=True) as draft:
        draft.stem_path(0).write_bytes(b"forced")
        draft.describe(0, StemStatus.RENDERED, -1.0)
    replaced = store.find(KEY_A)
    assert replaced is not None
    assert replaced.stems() == (StemMeta(StemStatus.RENDERED, -1.0),)
    assert replaced.stem_path(0).read_bytes() == b"forced", "replace kept the old unit"

    # keep_units deletes exactly the units not given.
    for key in (KEY_B, KEY_C):
        with store.write_unit(key) as draft:
            draft.stem_path(0).write_bytes(key.encode())
            draft.describe(0, StemStatus.RENDERED, -3.0)
    store.keep_units({KEY_A, KEY_C})
    assert store.find(KEY_A) is not None
    assert store.find(KEY_B) is None, "keep_units kept a unit outside the keys"
    assert store.find(KEY_C) is not None, "keep_units deleted a kept unit"

    # Records count up from 1, hold the song text and what the draft wrote, and an exception
    # publishes no record and uses no number.
    records = renders.song(SongId("songs/records.yaml"))
    with records.write_record("song 1", Suffix(".yaml"), latest=True) as record:
        assert record.run == 1
        image = record.path("images/node-0.png")
        assert image.parent.is_dir()
        image.write_bytes(b"image")
        assert record.relative(image) == "images/node-0.png"
        record.path("report.json").write_text("report 1", encoding="utf-8")
    first = records.record_path(1)
    assert (first / "song.yaml").read_text(encoding="utf-8") == "song 1"
    assert (first / "images" / "node-0.png").read_bytes() == b"image"
    assert (first / "report.json").read_text(encoding="utf-8") == "report 1"
    with pytest.raises(_InterruptedError):
        _interrupt_record(records, suffix)
    assert not records.record_path(2).exists(), "an exception left a record"
    records.sweep()
    for run in (2, 3):
        with records.write_record(f"song {run}", suffix, latest=False) as record:
            assert record.run == run, f"record {record.run} published where {run} was next"
        text = (records.record_path(run) / f"song{suffix}").read_text(encoding="utf-8")
        assert text == f"song {run}"
    assert (first / "song.yaml").read_text(encoding="utf-8") == "song 1", "a record changed"
    records.clear_latest()

    with store.lock():
        with pytest.raises(SongBusyError), renders.song(SongId("songs/contract.yaml")).lock():
            pass
        with renders.song(SongId("songs/other.yaml")).lock():
            pass
    with store.lock():
        pass

    # No two songs' units or records, and no preview, share a path.
    paths = [unit.stem_path(0), first / "song.yaml", first / "report.json"]
    other = renders.song(SongId("songs/other.yaml"))
    with other.write_unit(KEY_A) as draft:
        draft.stem_path(0).write_bytes(b"other song")
        draft.describe(0, StemStatus.RENDERED, -6.0)
    with other.write_record("song", Suffix(".yaml"), latest=True) as record:
        record.path("report.json").write_text("report", encoding="utf-8")
    other_unit = other.find(KEY_A)
    assert other_unit is not None
    paths += [other_unit.stem_path(0), other.record_path(1) / "song.yaml"]
    preview = renders.preview_path(Identifier("lead"), Identifier("song"))
    assert preview.parent.is_dir()
    paths.append(preview)
    shared = sorted({str(path) for path in paths if paths.count(path) > 1})
    assert not shared, f"two files share a path: {shared}"
    assert unit.stem_path(0).read_bytes() == b"forced", "another song's unit replaced this one's"


def check_mixer_contract(mixer: object) -> None:
    """Check a Mixer: the mix of one stem and of three has the stems' length."""
    assert isinstance(mixer, Mixer)
    for count in (1, 3):
        stems = [
            MixInput(TrackName(f"track{index}"), constant_audio(0.1), -3.0)
            for index in range(count)
        ]
        mixed = mixer.mix(stems, CONTRACT_FRAMES)
        assert isinstance(mixed, Audio)
        assert mixed.frames == CONTRACT_FRAMES, f"a mix of {count} has {mixed.frames} frames"


def check_measurer_contract(measurer: object) -> None:
    """Check a Measurer: levels for every band, and no loudness for silence."""
    assert isinstance(measurer, Measurer)
    tone = np.sin(2 * np.pi * 440 * np.arange(CONTRACT_FRAMES) / SAMPLE_RATE) * 0.5
    for audio in (Audio(np.stack([tone, tone]).astype(np.float32)), constant_audio(0.0)):
        levels = measurer.measure(audio)
        assert isinstance(levels, Levels), f"measure returned a {type(levels).__name__}"
        assert set(levels.bands_db) == set(BANDS), f"measured bands {sorted(levels.bands_db)}"
    assert measurer.measure(constant_audio(0.0)).integrated_lufs is None


def check_visualizer_contract(visualizer: object, tmp_path: Path) -> None:
    """Check a Visualizer: it writes a non-empty file at the path."""
    assert isinstance(visualizer, Visualizer)
    assert isinstance(visualizer.suffix, Suffix)
    path = tmp_path / f"image{visualizer.suffix}"
    visualizer.draw(constant_audio(0.1), path, "contract")
    assert path.is_file(), "draw wrote no file"
    assert path.stat().st_size > 0, "draw wrote an empty file"
