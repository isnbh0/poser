"""WAV files in the stem format, read and written through Pedalboard."""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pedalboard.io import ReadableAudioFile, WriteableAudioFile

from poser.adapters.loading import refuse_options
from poser.core.audio import (
    NUM_CHANNELS,
    SAMPLE_RATE,
    STEM_BIT_DEPTH,
    Audio,
    AudioProblem,
    make_audio,
)
from poser.core.jsonvalue import JsonValue
from poser.core.names import Suffix
from poser.core.ports import AdapterContext, AudioFileError


class WavAudioFiles:
    """Stem-format audio as 32-bit float WAV files."""

    @property
    def suffix(self) -> Suffix:
        return Suffix(".wav")

    def read(self, path: Path, frames: int) -> Audio:
        # The reader takes the frame count from the header and pads missing data with zeros, so a
        # truncated file would otherwise pass as quieter audio.
        try:
            size = path.stat().st_size
        except OSError as error:
            raise AudioFileError(f"cannot read stem: {error}") from error
        if size < frames * NUM_CHANNELS * STEM_BIT_DEPTH // 8:
            raise AudioFileError("stem file is shorter than its audio data")
        try:
            # Pedalboard's stubs mark `__new__` as a classmethod, which ty cannot match to any
            # call.
            with ReadableAudioFile(str(path)) as stem:  # ty: ignore[no-matching-overload]
                rate, channels, length = stem.samplerate, stem.num_channels, stem.frames
                if rate != SAMPLE_RATE:
                    raise AudioFileError(f"sample rate {rate:g} Hz, expected {SAMPLE_RATE}")
                if channels != NUM_CHANNELS:
                    raise AudioFileError(f"{channels} channels, expected {NUM_CHANNELS}")
                if length != frames:
                    raise AudioFileError(f"{length} samples, expected {frames}")
                data = stem.read(length)
        except (OSError, ValueError, RuntimeError) as error:
            raise AudioFileError(f"cannot read stem: {error}") from error
        audio = make_audio(data, frames)
        if isinstance(audio, AudioProblem):
            raise AudioFileError(audio.message)
        return audio

    def write(self, path: Path, audio: Audio) -> None:
        # Pedalboard picks the file format from the extension, so the name must end in .wav.
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".wav")
        os.close(fd)
        temp = Path(name)
        # mkstemp creates the file owner-only; give it the usual umask-based mode.
        umask = os.umask(0)
        os.umask(umask)
        try:
            temp.chmod(0o666 & ~umask)
            with WriteableAudioFile(  # ty: ignore[no-matching-overload]
                str(temp),
                samplerate=SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
                bit_depth=STEM_BIT_DEPTH,
            ) as output:
                output.write(audio.samples)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def create(options: Mapping[str, JsonValue], _context: AdapterContext) -> WavAudioFiles:
    """The factory for `wav`. Takes no options."""
    refuse_options(options)
    return WavAudioFiles()
