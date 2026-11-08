"""The gain mixer: stems scaled by their gains and summed, with nothing else applied."""

from collections.abc import Sequence

import numpy as np

from poser.core.audio import NUM_CHANNELS, Audio
from poser.core.ports import MixInput

OVERFLOW = "mix is not finite: a sum exceeds the float32 range"


class GainMixer:
    """Sums stems after each track's gain, with no limiter or normalization."""

    def mix(self, stems: Sequence[MixInput], frames: int) -> Audio:
        """Return the mix, raising ValueError when a sum exceeds float32's range.

        Overflow is an error, not clipped, so a mix is never quietly changed.
        """
        # Each stem is scaled by its track's gain, then summed, with no limiter or normalization.
        # Accumulating in place keeps one float64 buffer, not a float64 copy of every stem.
        total = np.zeros((NUM_CHANNELS, frames), dtype=np.float64)
        # A value beyond range becomes infinite, or not a number when infinities cancel, which
        # numpy reports as warnings; the check below reports it instead.
        with np.errstate(over="ignore", invalid="ignore"):
            for stem in stems:
                try:
                    scale = 10 ** (stem.gain_db / 20)
                except OverflowError:
                    raise ValueError(OVERFLOW) from None
                total += stem.audio.samples.astype(np.float64) * scale
            mixed = total.astype(np.float32)
        if not np.isfinite(mixed).all():
            raise ValueError(OVERFLOW)
        return Audio(mixed)
