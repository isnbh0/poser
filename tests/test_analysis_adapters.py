"""Tests for the gain mixer, the standard measurer, and the spectrogram visualizer."""

import warnings
from pathlib import Path

import numpy as np
import pytest
from contracts import check_measurer_contract, check_mixer_contract, check_visualizer_contract

from poser.adapters.measuring import StandardMeasurer, bands_db
from poser.adapters.mixing import GainMixer
from poser.adapters.spectrogram import SPECGRAM_NFFT, SpectrogramVisualizer, spectrogram_figure
from poser.core.analysis import measure
from poser.core.audio import NUM_CHANNELS, SAMPLE_RATE, Audio, stem_samples
from poser.core.names import TrackName
from poser.core.ports import MixInput
from poser.core.report import BANDS
from poser.core.song import Note, Song, Track

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def sine(frequency: float, seconds: float, amplitude: float = 1.0) -> np.ndarray:
    tone = amplitude * np.sin(
        2 * np.pi * frequency * np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    )
    return np.stack([tone, tone]).astype(np.float32)


def test_analysis_adapters_meet_their_contracts(tmp_path: Path) -> None:
    check_mixer_contract(GainMixer())
    check_measurer_contract(StandardMeasurer())
    check_visualizer_contract(SpectrogramVisualizer(), tmp_path)


def test_gain_mixer_sums_stems_after_their_gains() -> None:
    rng = np.random.default_rng(0)
    frames = 1000
    stems = [rng.uniform(-1, 1, (NUM_CHANNELS, frames)).astype(np.float32) for _ in range(3)]
    gains = [0.0, -6.0, 3.5]
    inputs = [
        MixInput(TrackName(f"t{index}"), Audio(stem), gain)
        for index, (stem, gain) in enumerate(zip(stems, gains, strict=True))
    ]
    expected = sum(
        stem.astype(np.float64) * 10 ** (gain / 20) for stem, gain in zip(stems, gains, strict=True)
    )
    mixed = GainMixer().mix(inputs, frames)
    assert mixed.samples.dtype == np.float32
    np.testing.assert_array_equal(mixed.samples, np.asarray(expected).astype(np.float32))


def test_gain_mixer_refuses_a_sum_beyond_float32() -> None:
    loud = Audio(np.full((NUM_CHANNELS, 10), 3e38, dtype=np.float32))
    inputs = [MixInput(TrackName("a"), loud, 12.0), MixInput(TrackName("b"), loud, 12.0)]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(ValueError, match="float32 range"):
            GainMixer().mix(inputs, 10)


@pytest.mark.parametrize(
    "stems",
    [
        [(7000.0, 0.5)],
        [(6100.0, 3e38)],
        [(6160.0, 1.0), (6160.0, 1.0)],
        [(6100.0, 3e38), (6100.0, -3e38)],
    ],
    ids=["gain overflows", "product overflows", "sum overflows", "infinities cancel"],
)
def test_gain_mixer_refuses_an_extreme_gain(stems: list[tuple[float, float]]) -> None:
    inputs = [
        MixInput(
            TrackName(f"t{index}"), Audio(np.full((NUM_CHANNELS, 10), value, np.float32)), gain
        )
        for index, (gain, value) in enumerate(stems)
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(ValueError, match="float32 range"):
            GainMixer().mix(inputs, 10)


@pytest.mark.parametrize(("frequency", "band"), [(100.0, "bass"), (3000.0, "high_mid")])
def test_sine_band_level_leads_every_other_band(frequency: float, band: str) -> None:
    levels = bands_db(sine(frequency, 2.0, 0.5))

    assert list(levels) == list(BANDS)
    others = [level for name, level in levels.items() if name != band]
    assert levels[band] >= max(others) + 20


# The values below pin the measurements, so any change to what the measurer reports shows here.


def test_ebu_tech_3341_case_1_measures_minus_23_lufs() -> None:
    # Stereo 1000 Hz sine at -23.0 dBFS per channel, in phase, for 20 seconds.
    levels = StandardMeasurer().measure(Audio(sine(1000.0, 20.0, 10 ** (-23.0 / 20))))

    assert levels.integrated_lufs == pytest.approx(-23.0, abs=0.1)


def test_standard_measurer_measures_a_sine() -> None:
    levels = StandardMeasurer().measure(Audio(sine(1000.0, 3.0, 0.5)))
    # A 1 kHz sine at half scale in both channels is about -6 LUFS.
    assert levels.integrated_lufs == pytest.approx(-6.055315916234416, abs=1e-6)
    assert dict(levels.bands_db) == pytest.approx(
        {
            "sub": -120.0,
            "bass": -120.0,
            "low_mid": -120.0,
            "mid": -40.79,
            "high_mid": -120.0,
            "high": -120.0,
        },
        abs=0.011,
    )


MEASURED_SONG = Song(bpm=120, tracks=(Track("lead", "Serum", (Note(0, 60, 8, 100),)),))


def rising_chord() -> np.ndarray:
    """Return 100 Hz and 1 kHz on the left and 1 kHz and 5 kHz on the right, rising over 4 s."""
    t = np.arange(stem_samples(MEASURED_SONG)) / SAMPLE_RATE
    rise = np.minimum(t / 4.0, 1.0)
    left = rise * (0.3 * np.sin(2 * np.pi * 100 * t) + 0.2 * np.sin(2 * np.pi * 1000 * t))
    right = rise * (0.2 * np.sin(2 * np.pi * 1000 * t) + 0.1 * np.sin(2 * np.pi * 5000 * t))
    return np.stack([left, right]).astype(np.float32)


def test_measure_with_the_standard_measurer_gives_the_old_measurements() -> None:
    measured = measure(Audio(rising_chord()), MEASURED_SONG, StandardMeasurer())
    assert measured.peak_dbfs == pytest.approx(-6.08500058477018, abs=1e-6)
    assert measured.integrated_lufs == pytest.approx(-12.88901437814406, abs=1e-6)
    assert measured.clipped_samples == 0
    assert dict(measured.bands_db) == pytest.approx(
        {
            "sub": -111.42,
            "bass": -44.78,
            "low_mid": -120.0,
            "mid": -51.31,
            "high_mid": -67.61,
            "high": -120.0,
        },
        abs=0.011,
    )
    assert measured.bar_dbfs == pytest.approx((-25.67, -17.22), abs=0.011)


def test_standard_measurer_gives_no_loudness_for_silence() -> None:
    levels = StandardMeasurer().measure(Audio(np.zeros((NUM_CHANNELS, SAMPLE_RATE), np.float32)))
    assert levels.integrated_lufs is None
    assert set(levels.bands_db.values()) == {-120.0}


@pytest.mark.parametrize("bpm", [90, 100, 140])
@pytest.mark.parametrize("bars", [1, 3, 8])
def test_bar_count_ends_at_the_last_beat(bpm: float, bars: int) -> None:
    note = Note(beat=0, pitch=60, length=4 * bars, velocity=100)
    song = Song(bpm=bpm, tracks=(Track("lead", "Serum", (note,)),))
    audio = Audio(np.full((2, stem_samples(song)), 0.5, dtype=np.float32))

    assert len(measure(audio, song, StandardMeasurer()).bar_dbfs) == bars


def test_spectrogram_has_a_log_axis_from_20_hz_to_20_khz_and_a_db_scale() -> None:
    figure = spectrogram_figure(sine(1000.0, 1.0), "lead")

    axes, colorbar = figure.axes
    assert axes.get_yscale() == "log"
    assert axes.get_ylim() == (20.0, 20000.0)
    assert axes.get_title() == "lead"
    assert (axes.get_xlabel(), axes.get_ylabel()) == ("Time (s)", "Frequency (Hz)")
    assert colorbar.get_ylabel() == "dB"
    # Only the bins at or above 20 Hz are drawn.
    (mesh,) = axes.collections
    bins = np.fft.rfftfreq(SPECGRAM_NFFT, 1 / SAMPLE_RATE)
    assert np.asarray(mesh.get_array()).shape[0] == np.count_nonzero(bins >= 20.0)


@pytest.mark.parametrize("amplitude", [0.5, 0.0], ids=["audio", "silence"])
def test_spectrogram_visualizer_writes_a_png(tmp_path: Path, amplitude: float) -> None:
    path = tmp_path / "lead.png"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        SpectrogramVisualizer().draw(Audio(sine(440.0, 1.0, amplitude)), path, "lead")
    assert path.read_bytes().startswith(PNG_MAGIC)


def test_spectrogram_guide_is_a_section() -> None:
    assert SpectrogramVisualizer().guide.startswith("## ")
