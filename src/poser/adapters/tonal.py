"""The `tonal` framework: major and minor keys, read per window by Krumhansl-Kessler profiles.

Each window's notes are weighted by how long they sound in it and counted by their nearest
twelve-tone pitch class. The thresholds below were calibrated against random notes, evenly spread
clusters, and generated tonal songs (`tests/test_tonal.py`); changing one means rerunning that
calibration.

The module also holds the `key` vocabulary, which reads stated keys such as `A minor` from a
song's `key` mark layer.
"""

import dataclasses
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from poser.core.evidence import (
    BETWEEN,
    BETWEEN_SHARE,
    FLAT_NAMES,
    SILENCE,
    TOO_LITTLE_BEATS,
    PitchTally,
    percent_above,
    tally_pitches,
    twelve_tone_obstacle,
)
from poser.core.jsonvalue import JsonValue
from poser.core.meter import Stretch
from poser.core.music import LETTER_SEMITONES
from poser.core.music import MODES as SCALES
from poser.core.names import Identifier
from poser.core.ports import AdapterContext, ConfigError
from poser.core.reading import (
    Agrees,
    Ambiguous,
    Contradicts,
    Known,
    Outcome,
    Reading,
    ReadingSpan,
    Rejected,
    ScoreRequest,
    Unchecked,
    Unknowable,
)
from poser.core.song import is_int, is_number

type Mode = Literal["major", "minor"]

NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
MODES: tuple[Mode, ...] = ("major", "minor")
# The Krumhansl-Kessler key profiles, indexed from the tonic.
MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88)
MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17)
PROFILES: Mapping[Mode, tuple[float, ...]] = {"major": MAJOR, "minor": MINOR}

MIN_CLASSES = 5  # Fewer sounding pitch classes than this imply no key.
MIN_SPREAD = 0.4  # Pitch-class weights whose deviation over mean is below this are even.
MIN_FIT = 0.75  # A best correlation below this fits no key.
STRONG_FIT = 0.85  # A lone best key at or above this is known.
LEAD = 0.05  # Keys within this of the best correlation are candidates.

DEFAULT_WINDOW_BARS = 4
MIN_WINDOW_BARS = 1
DEFAULT_WINDOW_BEATS = 16.0
MIN_WINDOW_BEATS = 1.0

TOO_FEW = "too few pitch classes to imply a key"
EVEN = "pitch classes are close to evenly weighted"
NO_FIT = "no key fits"

GUIDE = f"""\
## Tonal reading

The `tonal` framework reads the major or minor key of each window of the song, by correlating
how long each pitch class sounds with the Krumhansl-Kessler key profiles. Where the song
declares a meter, a window is `window_bars` bars ({DEFAULT_WINDOW_BARS} unless configured, at
least 1), counted from the first bar of each meter span, so a change of meter starts a new
window. Where it declares none, a window is `window_beats` beats ({DEFAULT_WINDOW_BEATS:g}
unless configured, at least 1), counted from the start of the unmetered stretch. A last window
shorter than half a window is read together with the one before it. A stretch shorter than half
a window, such as a pickup bar given its own meter span, is read alone as one short window and
usually reads `unknowable`. The printed beats of each span are the window that was read. Set
the options with `frameworks = [{{ use = "tonal", window_bars = 8, window_beats = 32 }}]` in
`[adapters]`.

A window shorter than the song's chord cycle can catch only part of the cycle and read
`ambiguous` or `unknowable` where a longer window reads the key. An unclear span in a song with
a long cycle is a reason to raise `window_bars` before concluding the passage has no key.

Each note counts for the beats it sounds in the window, whatever its velocity, under its
nearest twelve-tone pitch class, so a long held note can outweigh a melody: a scale over a held
tonic pedal reads "no key fits". Repeat `--track` to read a melody without its drone. A note
more than 40 cents from the nearest class lies between two classes. Classes sit at twelve-tone
equal temperament from A4 = 440 Hz, so a just, meantone, or 19-EDO scale reads as its key when
its tonic sits near a twelve-tone pitch, and the same music tuned 45 cents away does not. A
key's value reports `correlation` and `deviation_cents`, the window's sounding-time-weighted
mean distance from twelve-tone equal temperament.

A window is, in this order:
- unknowable, "no selected notes sound", when nothing sounds in it;
- unknowable, "pitches lie between twelve-tone pitch classes", when more than a quarter of its
  sounding time lies between classes;
- unknowable, "too few pitch classes to imply a key", with fewer than 5 sounding classes, since
  a triad alone belongs to several keys;
- unknowable, "pitch classes are close to evenly weighted", when the standard deviation of the
  class weights is below 0.4 of their mean;
- unknowable, "no key fits", when the best of the 24 correlations is below 0.75;
- known, when exactly one key is within 0.05 of the best correlation and that is at least 0.85;
- otherwise ambiguous, listing every key within 0.05 of the best, most likely first. A lone key
  between 0.75 and 0.85 is too weak to call, and is listed with the key or keys tied for the
  next correlation, however poorly they fit; compare the correlations to see a weak lone fit.

Only major and minor keys are read. A modal melody that dwells on its final reads as the major
or minor key on that final, so a D dorian chant reads as D minor.
Unpitched tracks are never read.
"""


def _plain(value: float) -> int | float:
    return int(value) if float(value).is_integer() else float(value)


@dataclass(frozen=True)
class TonalKey:
    """A key, with its profile correlation and the window's distance from equal temperament."""

    tonic: int  # 0 is C, 11 is B.
    mode: Mode
    correlation: float
    deviation_cents: float  # The same for every candidate in one window.

    def __post_init__(self) -> None:
        if not (is_int(self.tonic) and 0 <= self.tonic <= 11):
            raise ValueError(f"TonalKey.tonic must be an int from 0 to 11, got {self.tonic!r}")
        if self.mode not in MODES:
            raise ValueError(f"TonalKey.mode must be major or minor, got {self.mode!r}")
        for field in ("correlation", "deviation_cents"):
            value = getattr(self, field)
            if not (is_number(value) and math.isfinite(value)):
                raise ValueError(f"TonalKey.{field} must be a finite number, got {value!r}")

    @property
    def name(self) -> str:
        return f"{NAMES[self.tonic]} {self.mode}"

    def to_data(self) -> JsonValue:
        return {
            "key": self.name,
            "tonic": self.tonic,
            "mode": self.mode,
            "correlation": _plain(round(self.correlation, 3)),
            "deviation_cents": _plain(round(self.deviation_cents, 1)),
        }

    def describe(self) -> str:
        text = f"{self.name} (r {self.correlation:.2f}"
        if self.deviation_cents >= 1:
            text += f", {self.deviation_cents:.0f} cents from equal temperament"
        return text + ")"


def _correlation(weights: list[float], tonic: int, profile: tuple[float, ...]) -> float:
    xs = [weights[(tonic + i) % 12] for i in range(12)]
    mx = math.fsum(weights) / 12
    my = math.fsum(profile) / 12
    covariance = math.fsum((x - mx) * (y - my) for x, y in zip(xs, profile, strict=True))
    spread = math.fsum((x - mx) ** 2 for x in xs) * math.fsum((y - my) ** 2 for y in profile)
    return covariance / math.sqrt(spread)


def _order(key: TonalKey) -> tuple[float, int, int]:
    return (-key.correlation, key.tonic, MODES.index(key.mode))


def _distance(request: ScoreRequest, start: float, end: float) -> float:
    """The span's sounding time, each note's weighted by its distance in semitones from its
    nearest twelve-tone pitch class."""
    distance: list[float] = []
    for note in request.notes():
        weight = max(0.0, min(note.beat + note.length, end) - max(note.beat, start))
        if weight > 0:
            distance.append(weight * abs(note.pitch - math.floor(note.pitch + 0.5)))
    return math.fsum(distance)


def _window_outcome(request: ScoreRequest, start: float, end: float) -> Outcome[TonalKey]:
    tally = tally_pitches(request, start, end)
    weights = list(tally.weights)
    total = tally.total
    if total <= 0:
        return Unknowable(SILENCE)
    if tally.between > BETWEEN_SHARE * total:
        return Unknowable(BETWEEN)
    if sum(1 for weight in weights if weight > 0) < MIN_CLASSES:
        return Unknowable(TOO_FEW)
    mean = math.fsum(weights) / 12
    deviation = math.sqrt(math.fsum((weight - mean) ** 2 for weight in weights) / 12)
    if deviation / mean < MIN_SPREAD:
        return Unknowable(EVEN)
    cents = 100 * _distance(request, start, end) / total
    keys = sorted(
        (
            TonalKey(tonic, mode, _correlation(weights, tonic, PROFILES[mode]), cents)
            for tonic in range(12)
            for mode in MODES
        ),
        key=_order,
    )
    best = keys[0].correlation
    if best < MIN_FIT:
        return Unknowable(NO_FIT)
    candidates = [key for key in keys if key.correlation >= best - LEAD]
    if len(candidates) == 1:
        if best >= STRONG_FIT:
            return Known(keys[0])
        second = keys[1].correlation
        candidates = [keys[0], *(key for key in keys[1:] if key.correlation == second)]
    return Ambiguous(tuple(candidates))


def _joined(windows: list[tuple[float, float]], full: float) -> list[tuple[float, float]]:
    """Join a last window shorter than half of `full` beats to the window before it."""
    if len(windows) > 1 and windows[-1][1] - windows[-1][0] < full / 2:
        windows[-2:] = [(windows[-2][0], windows[-1][1])]
    return windows


def _windows(stretch: Stretch, window_beats: float, window_bars: int) -> list[tuple[float, float]]:
    """The `(start, end)` beats of each window of one stretch, the last ending at its end."""
    if stretch.bar is None:
        windows: list[tuple[float, float]] = []
        index = 0
        while (start := stretch.start + index * window_beats) < stretch.end:
            stop = min(stretch.start + (index + 1) * window_beats, stretch.end)
            windows.append((start, stop))
            index += 1
        return _joined(windows, window_beats)
    bars = stretch.bars()
    windows = [
        (bars[first][0], bars[min(first + window_bars, len(bars)) - 1][1])
        for first in range(0, len(bars), window_bars)
    ]
    return _joined(windows, window_bars * stretch.bar)


def read_keys(
    request: ScoreRequest, window_beats: float, window_bars: int = DEFAULT_WINDOW_BARS
) -> Reading[TonalKey]:
    """Read the key of each window of the request's song.

    Each stretch of the song's meter is cut on its own: a metered stretch into windows of
    `window_bars` bars, an unmetered one into windows of `window_beats` beats. A last window
    shorter than half a window joins the one before it.

    Deterministic, and independent of the order notes are visited in: every sum is exact.
    """
    if not (is_number(window_beats) and math.isfinite(window_beats) and window_beats > 0):
        raise ValueError(f"window_beats must be a finite positive number, got {window_beats!r}")
    if not (is_int(window_bars) and window_bars >= MIN_WINDOW_BARS):
        raise ValueError(f"window_bars must be a whole number of at least 1, got {window_bars!r}")
    end = request.end
    spans = [
        ReadingSpan(start, stop, _window_outcome(request, start, stop))
        for stretch in request.song.meter.stretches(end)
        for start, stop in _windows(stretch, window_beats, window_bars)
    ]
    return Reading(end, tuple(spans))


class TonalFramework:
    """The `tonal` framework adapter."""

    def __init__(self, window_beats: float, window_bars: int = DEFAULT_WINDOW_BARS) -> None:
        self._window_beats = window_beats
        self._window_bars = window_bars

    @property
    def name(self) -> Identifier:
        return Identifier("tonal")

    @property
    def guide(self) -> str:
        return GUIDE

    @property
    def window_beats(self) -> float:
        return self._window_beats

    @property
    def window_bars(self) -> int:
        return self._window_bars

    def read(self, request: ScoreRequest) -> Reading[TonalKey]:
        return read_keys(request, self._window_beats, self._window_bars)


def create_tonal(options: Mapping[str, JsonValue], _context: AdapterContext) -> TonalFramework:
    """The factory for `tonal`.

    Its options are `window_bars`, a whole number of at least 1, and `window_beats`, a finite
    number of at least 1.
    """
    for key in options:
        if key not in ("window_bars", "window_beats"):
            raise ConfigError(
                f"unknown option {key!r}; tonal takes only 'window_bars' and 'window_beats'"
            )
    bars = options.get("window_bars", DEFAULT_WINDOW_BARS)
    if not (is_int(bars) and bars >= MIN_WINDOW_BARS):
        raise ConfigError(f"window_bars: must be a whole number of at least 1, got {bars!r}")
    window = options.get("window_beats", DEFAULT_WINDOW_BEATS)
    if not (is_number(window) and math.isfinite(window) and window >= MIN_WINDOW_BEATS):
        raise ConfigError(f"window_beats: must be a finite number of at least 1, got {window!r}")
    return TonalFramework(float(window), bars)


type KeyMode = Literal["major", "minor", "dorian", "phrygian", "lydian", "mixolydian", "locrian"]

# The modes a `key` mark may state. Each key has one spelling, so `ionian` and `aeolian` are out.
KEY_MODES: tuple[KeyMode, ...] = (
    "major",
    "minor",
    "dorian",
    "phrygian",
    "lydian",
    "mixolydian",
    "locrian",
)
KEY_MODES_BY_NAME: Mapping[str, KeyMode] = {mode: mode for mode in KEY_MODES}
KEY_MODE_LIST = f"{', '.join(KEY_MODES[:-1])}, or {KEY_MODES[-1]}"

KEY_PATTERN = re.compile(rf"([A-G])([#b]?) ({'|'.join(KEY_MODES)})")
ACCIDENTALS = {"": 0, "#": 1, "b": -1}
KEY_REJECTION = (
    f"must be a tonic A to G with optional # or b, then {KEY_MODE_LIST}, such as A minor"
)

KEY_OUTSIDE_LIMIT = 0.15  # More than this share of sounding time off the scale contradicts.
# The scales whose pitch classes a mode allows; `minor` allows the raised sixth and seventh.
KEY_SCALES: Mapping[KeyMode, tuple[str, ...]] = {
    **{mode: (mode,) for mode in KEY_MODES},
    "minor": ("minor", "harmonic_minor", "melodic_minor"),
}

KEY_GUIDE = """\
## Key marks

The `key` layer states the author's key, written as a tonic `A` to `G` with an optional `#` or
`b`, a space, and a mode, such as `A minor`, `F# major`, or `D dorian`. The modes are `major`,
`minor`, `dorian`, `phrygian`, `lydian`, `mixolydian`, and `locrian`. `read-song` shows it above
the readings, as written. It never changes `tonal`'s reading. A `warning` names the key as
written and spells notes with flats when the tonic is written with a flat, and with sharps
otherwise.

`read-song` checks each key mark against the notes inside its span, counting each note for the
beats it sounds there under its nearest twelve-tone pitch class. The mark is contradicted, with
a `warning`, when more than {limit} of the sounding time is outside the mode's scale on the
stated tonic. `minor` also allows the raised sixth and seventh. The mark is `unchecked` when
no selected notes sound in its span, when less than {little} beats of sound do, or when more than
{between} of the sounding time lies between twelve-tone pitch classes.

A passing check is weak evidence. The mark is contradicted only when more than {limit} of the
sounding time is outside the scale, so a label one scale degree off usually passes: over a C
major scale `C lydian`, `C mixolydian`, and `C minor` all pass, and over C, F, and G triads
`C lydian` and `C mixolydian` still pass while `C minor` is contradicted.

The check tests which notes sound, never which note is home, so `C major`, `A minor`, and
`D dorian` all pass over the same passage. `minor` allows nine of the twelve pitch classes, so
a wrong `minor` label often passes, and so does a label a fourth or fifth from the true key. A
blues in dominant sevenths contradicts `major` and agrees with `mixolydian`. A line in the wrong
key passes under a thick texture; `--track` checks the line alone, which is how to find one part
that disagrees with a key the full mix satisfies. Where that track is otherwise silent in a
mark's span, its notes that ring briefly into the span from before it are all that is checked,
and together they can pass {little} beats and give a warning.
""".format(
    limit=f"{KEY_OUTSIDE_LIMIT:.0%}",
    little=f"{TOO_LITTLE_BEATS:g}",
    between=f"{BETWEEN_SHARE:.0%}",
)


@dataclass(frozen=True)
class KeyMark:
    """A key an author states in a `key` mark.

    `label` is the key as its author wrote it, which check sentences echo; it is empty for a key
    built from a pitch class, and two keys that differ only in it are equal.
    """

    tonic: int  # 0 is C, 11 is B.
    mode: KeyMode
    label: str = dataclasses.field(default="", compare=False)

    def __post_init__(self) -> None:
        if not (is_int(self.tonic) and 0 <= self.tonic <= 11):
            raise ValueError(f"KeyMark.tonic must be an int from 0 to 11, got {self.tonic!r}")
        if self.mode not in KEY_MODES:
            raise ValueError(f"KeyMark.mode must be {KEY_MODE_LIST}, got {self.mode!r}")

    @property
    def name(self) -> str:
        return f"{NAMES[self.tonic]} {self.mode}"

    def to_data(self) -> JsonValue:
        return {"key": self.name, "tonic": self.tonic, "mode": self.mode}

    def describe(self) -> str:
        return self.name

    @property
    def allowed(self) -> frozenset[int]:
        """The pitch classes the key allows."""
        return frozenset(
            (self.tonic + step) % 12 for name in KEY_SCALES[self.mode] for step in SCALES[name]
        )

    def check(self, tally: PitchTally) -> Agrees | Contradicts | Unchecked:
        """Compare the key with what sounds in its span: which notes sound, not which is home."""
        if obstacle := twelve_tone_obstacle(tally):
            return Unchecked(obstacle)
        outside = set(range(12)) - self.allowed
        share = tally.share(outside)
        if share <= KEY_OUTSIDE_LIMIT:
            return Agrees()
        written = self.label or self.name
        names = FLAT_NAMES if written[1:2] == "b" else NAMES
        return Contradicts(
            f"{percent_above(share, KEY_OUTSIDE_LIMIT)}% of the sounding time is outside "
            f"{written}, most on {tally.loudest(outside, names)}"
        )


class KeyVocabulary:
    """The `key` vocabulary: reads stated keys such as `A minor` or `D dorian`."""

    @property
    def name(self) -> Identifier:
        return Identifier("key")

    @property
    def guide(self) -> str:
        return KEY_GUIDE

    def parse(self, text: str) -> KeyMark | Rejected:
        match = KEY_PATTERN.fullmatch(text)
        if match is None:
            return Rejected(KEY_REJECTION)
        letter, accidental, mode = match.groups()
        tonic = (LETTER_SEMITONES[letter] + ACCIDENTALS[accidental]) % 12
        return KeyMark(tonic, KEY_MODES_BY_NAME[mode], text)


def create_key(options: Mapping[str, JsonValue], _context: AdapterContext) -> KeyVocabulary:
    """The factory for `key`. It takes no options."""
    for key in options:
        raise ConfigError(f"unknown option {key!r}; key takes no options")
    return KeyVocabulary()
