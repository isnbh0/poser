"""The `tonal` framework: major and minor keys, read per window by Krumhansl-Kessler profiles.

Each window's notes are weighted by how long they sound in it and counted by their nearest
twelve-tone pitch class. The thresholds below were calibrated against random notes, evenly spread
clusters, and generated tonal songs (`tests/test_tonal.py`); changing one means rerunning that
calibration.

The module also holds the `key` vocabulary, which reads stated keys such as `A minor` from a
song's `key` mark layer.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from poser.core.jsonvalue import JsonValue
from poser.core.music import LETTER_SEMITONES
from poser.core.names import Identifier
from poser.core.ports import AdapterContext, ConfigError
from poser.core.reading import (
    Ambiguous,
    Known,
    Outcome,
    Reading,
    ReadingSpan,
    Rejected,
    ScoreRequest,
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

BETWEEN_SEMITONES = 0.4  # A note farther than 40 cents from its nearest class lies between two.
BETWEEN_SHARE = 0.25  # More than this share of sounding time between classes is unknowable.
MIN_CLASSES = 5  # Fewer sounding pitch classes than this imply no key.
MIN_SPREAD = 0.4  # Pitch-class weights whose deviation over mean is below this are even.
MIN_FIT = 0.75  # A best correlation below this fits no key.
STRONG_FIT = 0.85  # A lone best key at or above this is known.
LEAD = 0.05  # Keys within this of the best correlation are candidates.

DEFAULT_WINDOW_BEATS = 16.0
MIN_WINDOW_BEATS = 1.0

SILENCE = "no selected notes sound"
BETWEEN = "pitches lie between twelve-tone pitch classes"
TOO_FEW = "too few pitch classes to imply a key"
EVEN = "pitch classes are close to evenly weighted"
NO_FIT = "no key fits"

GUIDE = """\
## Tonal reading

The `tonal` framework reads the major or minor key of each window of the song, by correlating
how long each pitch class sounds with the Krumhansl-Kessler key profiles. Windows are
`window_beats` long (default 16, at least 1); the last window ends at the song's end. The
default assumes four 4-beat bars per window, the same assumption `bar_dbfs` makes. Set it with
`frameworks = [{ use = "tonal", window_beats = 8 }]` in `[adapters]`.

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


def _window_outcome(request: ScoreRequest, start: float, end: float) -> Outcome[TonalKey]:
    by_class: list[list[float]] = [[] for _ in range(12)]
    between: list[float] = []
    distance: list[float] = []  # weight * distance from the nearest class, in semitones
    for note in request.notes():
        weight = max(0.0, min(note.beat + note.length, end) - max(note.beat, start))
        if weight <= 0:
            continue
        nearest = math.floor(note.pitch + 0.5)
        offset = abs(note.pitch - nearest)
        by_class[nearest % 12].append(weight)
        distance.append(weight * offset)
        if offset > BETWEEN_SEMITONES:
            between.append(weight)
    weights = [math.fsum(items) for items in by_class]
    total = math.fsum(w for items in by_class for w in items)
    if total <= 0:
        return Unknowable(SILENCE)
    if math.fsum(between) > BETWEEN_SHARE * total:
        return Unknowable(BETWEEN)
    if sum(1 for weight in weights if weight > 0) < MIN_CLASSES:
        return Unknowable(TOO_FEW)
    mean = math.fsum(weights) / 12
    deviation = math.sqrt(math.fsum((weight - mean) ** 2 for weight in weights) / 12)
    if deviation / mean < MIN_SPREAD:
        return Unknowable(EVEN)
    cents = 100 * math.fsum(distance) / total
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


def read_keys(request: ScoreRequest, window_beats: float) -> Reading[TonalKey]:
    """Read the key of each `window_beats`-long window of the request's song.

    Deterministic, and independent of the order notes are visited in: every sum is exact.
    """
    if not (is_number(window_beats) and math.isfinite(window_beats) and window_beats > 0):
        raise ValueError(f"window_beats must be a finite positive number, got {window_beats!r}")
    end = request.end
    spans: list[ReadingSpan[TonalKey]] = []
    index = 0
    while (start := index * window_beats) < end:
        stop = min((index + 1) * window_beats, end)
        spans.append(ReadingSpan(start, stop, _window_outcome(request, start, stop)))
        index += 1
    return Reading(end, tuple(spans))


class TonalFramework:
    """The `tonal` framework adapter."""

    def __init__(self, window_beats: float) -> None:
        self._window_beats = window_beats

    @property
    def name(self) -> Identifier:
        return Identifier("tonal")

    @property
    def guide(self) -> str:
        return GUIDE

    @property
    def window_beats(self) -> float:
        return self._window_beats

    def read(self, request: ScoreRequest) -> Reading[TonalKey]:
        return read_keys(request, self._window_beats)


def create_tonal(options: Mapping[str, JsonValue], _context: AdapterContext) -> TonalFramework:
    """The factory for `tonal`. Its one option is `window_beats`, a finite number of at least 1."""
    for key in options:
        if key != "window_beats":
            raise ConfigError(f"unknown option {key!r}; tonal takes only 'window_beats'")
    window = options.get("window_beats", DEFAULT_WINDOW_BEATS)
    if not (is_number(window) and math.isfinite(window) and window >= MIN_WINDOW_BEATS):
        raise ConfigError(f"window_beats: must be a finite number of at least 1, got {window!r}")
    return TonalFramework(float(window))


KEY_PATTERN = re.compile(r"([A-G])([#b]?) (major|minor)")
ACCIDENTALS = {"": 0, "#": 1, "b": -1}
KEY_REJECTION = "must be a tonic A to G with optional # or b, then major or minor, such as A minor"

KEY_GUIDE = """\
## Key marks

The `key` layer states the author's key, written as a tonic `A` to `G` with an optional `#` or
`b`, a space, and `major` or `minor`, such as `A minor`, `F# major`, or `Bb minor`. `read-song`
shows it above the readings, as written. It never changes `tonal`'s reading.
"""


@dataclass(frozen=True)
class KeyMark:
    """A key an author states in a `key` mark."""

    tonic: int  # 0 is C, 11 is B.
    mode: Mode

    def __post_init__(self) -> None:
        if not (is_int(self.tonic) and 0 <= self.tonic <= 11):
            raise ValueError(f"KeyMark.tonic must be an int from 0 to 11, got {self.tonic!r}")
        if self.mode not in MODES:
            raise ValueError(f"KeyMark.mode must be major or minor, got {self.mode!r}")

    @property
    def name(self) -> str:
        return f"{NAMES[self.tonic]} {self.mode}"

    def to_data(self) -> JsonValue:
        return {"key": self.name, "tonic": self.tonic, "mode": self.mode}

    def describe(self) -> str:
        return self.name


class KeyVocabulary:
    """The `key` vocabulary: reads stated major and minor keys such as `A minor`."""

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
        return KeyMark(tonic, "major" if mode == "major" else "minor")


def create_key(options: Mapping[str, JsonValue], _context: AdapterContext) -> KeyVocabulary:
    """The factory for `key`. It takes no options."""
    for key in options:
        raise ConfigError(f"unknown option {key!r}; key takes no options")
    return KeyVocabulary()
