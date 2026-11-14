"""The fakes pass every contract suite, and a broken fake fails its suite."""

from collections.abc import Callable, Collection, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast, override

import pytest
from contracts import (
    check_audio_files_contract,
    check_instrument_contract,
    check_measurer_contract,
    check_mixer_contract,
    check_record_writer_contract,
    check_render_store_contract,
    check_song_format_contract,
    check_sound_capture_contract,
    check_visualizer_contract,
)
from fakes import (
    FakeInstrument,
    FakeMeasurer,
    FakeMixer,
    FakeRecordWriter,
    FakeRenderStore,
    FakeSongFormat,
    FakeSongStore,
    FakeSound,
    FakeUnitDraft,
    FakeVisualizer,
    NpyAudioFiles,
    SavedSounds,
)
from test_song import gain_song

from poser.core.audio import Audio
from poser.core.graph import Graph
from poser.core.jsonvalue import Json
from poser.core.names import Identifier, SongId
from poser.core.ports import (
    Captured,
    MixInput,
    SoundListing,
)
from poser.core.report import Levels
from poser.core.song import Song
from poser.core.sounds import SoundEntry, SoundRef

FAKE = Identifier("fake")


def fake_instrument(saved: SavedSounds | None = None) -> FakeInstrument:
    sounds = {None: FakeSound(), "pad": FakeSound("a pad", "pad", 0.2)}
    return FakeInstrument("fake", sounds, saved=saved)


def fake_instruments() -> Callable[[Path], FakeInstrument]:
    """Return a factory whose instruments on one directory share their saved sounds."""
    libraries: dict[Path, SavedSounds] = {}
    return lambda directory: fake_instrument(libraries.setdefault(directory, SavedSounds()))


def captured(instrument: object) -> Captured:
    assert isinstance(instrument, FakeInstrument)
    return Captured(Json.of({"name": "contract", "amplitude": 0.3}))


def test_fake_instrument_meets_the_instrument_contract(tmp_path: Path) -> None:
    check_instrument_contract(fake_instrument(), SoundRef(FAKE, "pad"), tmp_path)


def test_fake_instrument_meets_the_sound_capture_contract(tmp_path: Path) -> None:
    check_sound_capture_contract(fake_instruments(), captured, tmp_path)


def test_fake_render_store_meets_the_render_store_contract(tmp_path: Path) -> None:
    check_render_store_contract(FakeRenderStore, tmp_path)
    check_render_store_contract(lambda root: FakeRenderStore(root, ".wav"), tmp_path / "wav")


def test_npy_audio_files_meet_the_audio_files_contract(tmp_path: Path) -> None:
    check_audio_files_contract(NpyAudioFiles(), tmp_path)


def test_fake_record_writer_meets_the_record_writer_contract() -> None:
    check_record_writer_contract(FakeRecordWriter(), gain_song())


def test_fake_song_format_meets_the_song_format_contract(tmp_path: Path) -> None:
    check_song_format_contract(FakeSongFormat(), gain_song(), tmp_path)
    check_record_writer_contract(FakeSongFormat(), gain_song())


def test_fake_analysis_adapters_meet_their_contracts(tmp_path: Path) -> None:
    check_mixer_contract(FakeMixer())
    check_measurer_contract(FakeMeasurer())
    check_visualizer_contract(FakeVisualizer(), tmp_path)


# Broken fakes


class UnknownListedInstrument(FakeInstrument):
    @override
    def sounds(self) -> SoundListing:
        listing = super().sounds()
        unknown = SoundEntry(SoundRef(self.name, "gone"), "a sound that does not resolve")
        return SoundListing((*listing.entries, unknown), listing.problems, listing.notes)


class ReplacingInstrument(FakeInstrument):
    @override
    def store(
        self, name: Identifier, description: str, captured: Captured, *, replace: bool
    ) -> SoundRef | str:
        return super().store(name, description, captured, replace=True)


class LeakingSongStore(FakeSongStore):
    """Publishes a unit even when its writer raises."""

    @override
    @contextmanager
    def write_unit(self, key: str, *, replace: bool = False) -> Generator[FakeUnitDraft]:
        error: Exception | None = None
        with super().write_unit(key, replace=replace) as draft:
            try:
                yield draft
            except Exception as raised:  # noqa: BLE001 -- the broken behavior under test.
                error = raised
        if error is not None:
            raise error


class HoardingSongStore(FakeSongStore):
    """Keeps every unit."""

    @override
    def keep_units(self, keys: Collection[str]) -> None:
        pass


def broken_store(kind: type[FakeSongStore]) -> Callable[[Path], FakeRenderStore]:
    """Return a factory of render stores whose song stores are `kind`."""

    class BrokenRenderStore(FakeRenderStore):
        @override
        def song(self, song_id: SongId) -> FakeSongStore:
            store = super().song(song_id)
            return kind(store.directory, self.stem_suffix, store.state)

    return BrokenRenderStore


class ShortMixer(FakeMixer):
    @override
    def mix(self, stems: Sequence[MixInput], frames: int) -> Audio:
        return Audio(super().mix(stems, frames).samples[:, :-1])


class BandlessLevels:
    """Levels missing a band, which a real Levels refuses to hold."""

    integrated_lufs = -14.0
    bands_db = {"bass": -20.0}  # noqa: RUF012 -- a fixed value for one test.


class BandlessMeasurer:
    def measure(self, audio: Audio) -> Levels:
        assert audio.frames
        return cast("Levels", BandlessLevels())


class SilentVisualizer(FakeVisualizer):
    @override
    def draw(self, audio: Audio, path: Path, title: str) -> None:
        pass


class CountingRecordWriter(FakeRecordWriter):
    def __init__(self) -> None:
        self.calls = 0

    @override
    def dump(self, song: Song | Graph) -> str:
        self.calls += 1
        return f"{super().dump(song)}\n# dump {self.calls}"


def test_contracts_can_fail(tmp_path: Path) -> None:
    unknown = UnknownListedInstrument("fake", {None: FakeSound()})
    with pytest.raises(AssertionError, match="does not resolve"):
        check_instrument_contract(unknown, SoundRef(FAKE), tmp_path / "instrument")
    with pytest.raises(AssertionError, match="over an existing sound"):
        check_sound_capture_contract(
            lambda directory: ReplacingInstrument("fake", {None: FakeSound()}),
            captured,
            tmp_path / "capture",
        )
    # Each instance keeps its own sounds, so one instance's store misses another's save.
    with pytest.raises(AssertionError, match="over another instance's sound"):
        check_sound_capture_contract(
            lambda directory: fake_instrument(), captured, tmp_path / "unshared"
        )
    cases = [
        (LeakingSongStore, "an exception left a unit"),
        (HoardingSongStore, "kept a unit outside the keys"),
    ]
    for index, (kind, message) in enumerate(cases):
        with pytest.raises(AssertionError, match=message):
            check_render_store_contract(broken_store(kind), tmp_path / f"store-{index}")
    with pytest.raises(AssertionError, match="a mix of 1 has"):
        check_mixer_contract(ShortMixer())
    with pytest.raises(AssertionError, match="returned a BandlessLevels"):
        check_measurer_contract(BandlessMeasurer())
    with pytest.raises(AssertionError, match="wrote no file"):
        check_visualizer_contract(SilentVisualizer(), tmp_path)
    with pytest.raises(AssertionError, match="two texts for one song"):
        check_record_writer_contract(CountingRecordWriter(), gain_song())
