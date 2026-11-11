"""Contract checks that every adapter of a port must pass.

Every audio a suite passes is at least TAIL_S long, the precondition of measurers and visualizers.
"""

import dataclasses
import os
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
    stem_samples,
)
from poser.core.cache import Stamp, StampProblem
from poser.core.names import Identifier, NodeName, SongId, Suffix, TrackName
from poser.core.performance import perform
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
    ReportSection,
    ResolvedSound,
    SectionInput,
    SectionResult,
    SongBusyError,
    SongFormat,
    SongStore,
    SoundCapture,
    SoundFailure,
    SoundListing,
    Visualizer,
)
from poser.core.report import BANDS, Levels, Measurements
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
    performance = perform(song, song.tracks[0])
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


def check_record_writer_contract(writer: object, sample_song: Song) -> None:
    """Check a RecordWriter: its suffix, and one non-empty text for equal songs."""
    assert isinstance(writer, RecordWriter)
    assert isinstance(writer.suffix, Suffix)
    text = writer.dump(sample_song)
    assert isinstance(text, str)
    assert text
    assert writer.dump(sample_song) == text, "dump gave two texts for one song"
    assert writer.dump(dataclasses.replace(sample_song)) == text, (
        "dump gave two texts for equal songs"
    )


def _rewrite(path: Path, data: bytes) -> None:
    """Write new contents and move the modification time forward, as a later write would."""
    info = path.stat()
    path.write_bytes(data)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))


def check_render_store_contract(
    make: Callable[[Path], object],
    tmp_path: Path,
    spoil_stamp: Callable[[SongStore, NodeName], None],
) -> None:
    """Check a RenderStore built by `make(root)` and its song stores.

    `spoil_stamp(store, track)` replaces the track's stamp with garbage, since only the adapter
    knows where its stamps live.
    """
    renders = make(tmp_path / "renders")
    assert isinstance(renders, RenderStore)
    suffix = renders.stem_suffix
    assert isinstance(suffix, Suffix)
    png = Suffix(".png")
    store = renders.song(SongId("songs/contract.yaml"))
    assert isinstance(store, SongStore)
    lead = TrackName("lead")
    stamp = Stamp("a" * 64, "b" * 64, StemStatus.RENDERED, -6.0)

    stem = store.stem_path(lead)
    assert stem.parent.is_dir()
    assert store.read_stamp(lead) is StampProblem.MISSING
    stem.write_bytes(b"first stem")
    store.write_stamp(lead, stamp)
    assert store.read_stamp(lead) == stamp
    _rewrite(stem, b"second, longer stem")
    assert store.read_stamp(lead) is StampProblem.CHANGED
    store.write_stamp(lead, stamp)
    spoil_stamp(store, lead)
    assert store.read_stamp(lead) is StampProblem.MISSING, "a garbage stamp was not MISSING"

    store.write_stamp(lead, stamp)
    store.discard(lead)
    assert not stem.exists(), "discard left the stem"
    assert store.read_stamp(lead) is StampProblem.MISSING, "discard left the stamp"

    # clear_latest removes the latest results and nothing else.
    stem.write_bytes(b"stem")
    store.write_stamp(lead, stamp)
    record = store.start_record("song text", Suffix(".yaml"))
    latest = [store.image_path(lead, png), store.image_path(None, png)]
    latest += [store.mix_path(), store.report_path()]
    for path in latest:
        assert path.parent.is_dir()
        path.write_bytes(b"latest")
    store.clear_latest()
    assert not [path for path in latest if path.exists()]
    # The latest paths stay writable after clear_latest.
    again = [store.image_path(lead, png), store.image_path(None, png)]
    again += [store.mix_path(), store.report_path()]
    missing = [path for path in again if not path.parent.is_dir()]
    assert not missing, f"no parent directory after clear_latest: {missing}"
    assert store.read_stamp(lead) == stamp
    assert (record.path / "song.yaml").read_text(encoding="utf-8") == "song text"

    with store.lock():
        with pytest.raises(SongBusyError), renders.song(SongId("songs/contract.yaml")).lock():
            pass
        with renders.song(SongId("songs/other.yaml")).lock():
            pass
    with store.lock():
        pass

    counted = renders.song(SongId("songs/counted.yaml"))
    records = [counted.start_record(f"song {n}", suffix) for n in range(3)]
    assert [record.run for record in records] == [1, 2, 3]
    for n, record in enumerate(records):
        assert (record.path / f"song{suffix}").read_text(encoding="utf-8") == f"song {n}"
        counted.finish_record(record, f"report {n}")
        assert [
            path
            for path in record.path.rglob("*")
            if path.is_file() and path.read_text(encoding="utf-8") == f"report {n}"
        ]

    counted.image_path(lead, png).write_bytes(b"track image")
    counted.image_path(None, png).write_bytes(b"mix image")
    kept_track = counted.keep_image(records[0], lead, png)
    kept_mix = counted.keep_image(records[0], None, png)
    assert kept_track.read_bytes() == b"track image"
    assert kept_mix.read_bytes() == b"mix image"

    # No track name, image suffix, or record suffix makes two files share a path.
    names = [TrackName(name) for name in ("song", "mix", "lead")]
    clashing = renders.song(SongId("songs/clashing.yaml"))
    record = clashing.start_record("song text", suffix)
    paths = [clashing.mix_path(), clashing.report_path(), record.path / f"song{suffix}"]
    paths += [clashing.stem_path(name) for name in names]
    paths += [store.stem_path(lead)]
    for image_suffix in {suffix, png}:
        for name in [*names, None]:
            image = clashing.image_path(name, image_suffix)
            assert image.parent.is_dir()
            image.write_bytes(b"image")
            paths += [image, clashing.keep_image(record, name, image_suffix)]
    preview = renders.preview_path(Identifier("lead"), Identifier("song"))
    assert preview.parent.is_dir()
    paths.append(preview)
    shared = sorted({str(path) for path in paths if paths.count(path) > 1})
    assert not shared, f"two files share a path: {shared}"


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


def check_section_contract(section: object) -> None:
    """Check a ReportSection: its metadata, and a result for a track and the mix."""
    assert isinstance(section, ReportSection)
    assert isinstance(section.name, Identifier)
    assert isinstance(section.version, int)
    assert section.version >= 1
    assert section.guide.startswith("## ")
    note = (Note(beat=0, pitch=60, length=1, velocity=100),)
    song = Song(bpm=120, tracks=(Track("lead", "fake", note),))
    measurements = Measurements(-20.0, -23.0, 0, dict.fromkeys(BANDS, -30.0), (-20.0,))
    frames = stem_samples(song)  # Analysis hands sections whole stems, tail included.
    assert frames >= CONTRACT_FRAMES
    audio = constant_audio(0.1, frames)
    track = section.track(SectionInput(song, song.graph().nodes[0], audio, measurements))
    assert isinstance(track, SectionResult), f"track returned a {type(track).__name__}"
    mix = section.mix(SectionInput(song, None, audio, measurements))
    assert isinstance(mix, SectionResult), f"mix returned a {type(mix).__name__}"
