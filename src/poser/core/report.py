"""The analysis report: its schema, its JSON form, and the built-in analysis policy."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import cast

import numpy as np

from poser.core.audio import SAMPLE_RATE, SILENCE_DBFS
from poser.core.graph import NODE_KINDS, NodeKind
from poser.core.jsonvalue import JsonValue
from poser.core.song import is_int, is_number

MIX_NAME = "mix"

# Mixing's usual frequency bands, in Hz, for comparing a track's balance between versions.
BANDS: Mapping[str, tuple[float, float]] = MappingProxyType(
    {
        "sub": (20.0, 60.0),
        "bass": (60.0, 250.0),
        "low_mid": (250.0, 500.0),
        "mid": (500.0, 2000.0),
        "high_mid": (2000.0, 6000.0),
        "high": (6000.0, 20000.0),
    }
)
# Songs have no meter, so a bar is always this many beats.
BAR_BEATS = 4
LEVEL_FLOOR_DBFS = -120.0
# Streaming platforms and EBU R128 cap true peak at -1 dBTP. Analysis measures sample peak, which
# can sit below true peak, so this ceiling is necessary for that headroom but not sufficient.
MIX_CEILING_DBFS = -1.0


class AnalysisStatus(StrEnum):
    ANALYZED = "analyzed"
    MISSING_STEM = "missing_stem"
    STALE_STEM = "stale_stem"


class Defect(StrEnum):
    """A problem that fails the analysis.

    `headroom` means the mix's sample peak is above MIX_CEILING_DBFS. Sample peak stands in for
    true peak, which analysis does not measure.
    """

    SILENT = "silent"
    CLIPPING = "clipping"
    HEADROOM = "headroom"


def _finite(owner: str, name: str, value: object, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not is_number(value):
        raise ValueError(f"{owner}.{name} must be a finite number, got {value!r}")


def _bands(owner: str, value: object) -> Mapping[str, float]:
    """Return a read-only copy of band levels, which must have exactly the keys of BANDS."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{owner}.bands_db must be a mapping, got {value!r}")
    bands = dict(cast("Mapping[object, object]", value))
    if set(bands) != set(BANDS):
        raise ValueError(f"{owner}.bands_db must have the keys {list(BANDS)}, got {list(bands)}")
    for name, level in bands.items():
        _finite(owner, f"bands_db[{name!r}]", level)
    return MappingProxyType(cast("dict[str, float]", bands))


def _bars(owner: str, value: object) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError(f"{owner}.bar_dbfs must be a sequence, got {value!r}")
    bars = tuple(cast("Sequence[object]", value))
    for index, level in enumerate(bars):
        _finite(owner, f"bar_dbfs[{index}]", level)
    return cast("tuple[float, ...]", bars)


def _members[T](owner: str, name: str, value: object, kind: type[T]) -> tuple[T, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise ValueError(f"{owner}.{name} must be a sequence, got {value!r}")
    items = tuple(cast("Sequence[object]", value))
    for item in items:
        if not isinstance(item, kind):
            raise ValueError(f"{owner}.{name} must hold only {kind.__name__}, got {item!r}")
    return cast("tuple[T, ...]", items)


def _clipped(owner: str, value: object, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not (is_int(value) and value >= 0):
        raise ValueError(f"{owner}.clipped_samples must be an integer of at least 0, got {value!r}")


@dataclass(frozen=True)
class Levels:
    """What a level measurer returns for some audio."""

    integrated_lufs: float | None
    bands_db: Mapping[str, float]

    def __post_init__(self) -> None:
        _finite("Levels", "integrated_lufs", self.integrated_lufs, optional=True)
        object.__setattr__(self, "bands_db", _bands("Levels", self.bands_db))


@dataclass(frozen=True)
class Measurements:
    peak_dbfs: float
    integrated_lufs: float | None
    clipped_samples: int
    bands_db: Mapping[str, float]
    bar_dbfs: tuple[float, ...]

    def __post_init__(self) -> None:
        _finite("Measurements", "peak_dbfs", self.peak_dbfs)
        _finite("Measurements", "integrated_lufs", self.integrated_lufs, optional=True)
        _clipped("Measurements", self.clipped_samples)
        object.__setattr__(self, "bands_db", _bands("Measurements", self.bands_db))
        object.__setattr__(self, "bar_dbfs", _bars("Measurements", self.bar_dbfs))


@dataclass(frozen=True)
class NodeReport:
    """One node's analysis. The keys are the ones a fresh stem of the node would carry, so two
    reports show which nodes changed between song versions. `node_key` is the key of the node's
    render unit, None when the unit cannot be planned. `sound_key` is None when the node has no
    resolved sound or recipe: its sound does not resolve, or it is a delay."""

    name: str
    status: AnalysisStatus
    node_key: str | None
    sound_key: str | None
    kind: NodeKind
    upstream: tuple[str, ...]
    gain_db: float = 0.0
    peak_dbfs: float | None = None
    integrated_lufs: float | None = None
    clipped_samples: int | None = None
    bands_db: Mapping[str, float] | None = None
    bar_dbfs: tuple[float, ...] | None = None
    spectrogram: str | None = None
    defects: tuple[Defect, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AnalysisStatus):
            raise ValueError(f"NodeReport.status must be an AnalysisStatus, got {self.status!r}")
        if self.kind not in NODE_KINDS:
            raise ValueError(f"NodeReport.kind must be one of {NODE_KINDS}, got {self.kind!r}")
        _finite("NodeReport", "gain_db", self.gain_db)
        _finite("NodeReport", "peak_dbfs", self.peak_dbfs, optional=True)
        _finite("NodeReport", "integrated_lufs", self.integrated_lufs, optional=True)
        _clipped("NodeReport", self.clipped_samples, optional=True)
        if self.bands_db is not None:
            object.__setattr__(self, "bands_db", _bands("NodeReport", self.bands_db))
        if self.bar_dbfs is not None:
            object.__setattr__(self, "bar_dbfs", _bars("NodeReport", self.bar_dbfs))
        defects = _members("NodeReport", "defects", self.defects, Defect)
        object.__setattr__(self, "defects", defects)
        object.__setattr__(self, "upstream", _members("NodeReport", "upstream", self.upstream, str))


@dataclass(frozen=True)
class MixReport:
    path: str
    peak_dbfs: float
    integrated_lufs: float | None
    clipped_samples: int
    bands_db: Mapping[str, float]
    bar_dbfs: tuple[float, ...]
    spectrogram: str
    defects: tuple[Defect, ...]

    def __post_init__(self) -> None:
        _finite("MixReport", "peak_dbfs", self.peak_dbfs)
        _finite("MixReport", "integrated_lufs", self.integrated_lufs, optional=True)
        _clipped("MixReport", self.clipped_samples)
        object.__setattr__(self, "bands_db", _bands("MixReport", self.bands_db))
        object.__setattr__(self, "bar_dbfs", _bars("MixReport", self.bar_dbfs))
        object.__setattr__(self, "defects", _members("MixReport", "defects", self.defects, Defect))


@dataclass(frozen=True)
class Report:
    """An analysis. `run` numbers its record under `history/`, and `song_sha256` digests the song
    text that record holds, so one song version always has one digest."""

    song: str
    run: int
    song_sha256: str
    nodes: tuple[NodeReport, ...]
    mix: MixReport | None

    def __post_init__(self) -> None:
        if not isinstance(self.song, str):
            raise ValueError(f"Report.song must be a str, got {self.song!r}")
        if isinstance(self.run, bool) or not isinstance(self.run, int) or self.run < 1:
            raise ValueError(f"Report.run must be a positive int, got {self.run!r}")
        if not isinstance(self.song_sha256, str):
            raise ValueError(f"Report.song_sha256 must be a str, got {self.song_sha256!r}")
        object.__setattr__(self, "nodes", _members("Report", "nodes", self.nodes, NodeReport))
        if not (self.mix is None or isinstance(self.mix, MixReport)):
            raise ValueError(f"Report.mix must be a MixReport or None, got {self.mix!r}")


def _node_to_data(node: NodeReport) -> JsonValue:
    return {
        "name": node.name,
        "status": str(node.status),
        "kind": node.kind,
        "upstream": list(node.upstream),
        "node_key": node.node_key,
        "sound_key": node.sound_key,
        "gain_db": node.gain_db,
        "peak_dbfs": node.peak_dbfs,
        "integrated_lufs": node.integrated_lufs,
        "clipped_samples": node.clipped_samples,
        "bands_db": None if node.bands_db is None else dict(node.bands_db),
        "bar_dbfs": None if node.bar_dbfs is None else list(node.bar_dbfs),
        "spectrogram": node.spectrogram,
        "defects": [str(defect) for defect in node.defects],
        "detail": node.detail,
    }


def _mix_to_data(mix: MixReport) -> JsonValue:
    return {
        "path": mix.path,
        "peak_dbfs": mix.peak_dbfs,
        "integrated_lufs": mix.integrated_lufs,
        "clipped_samples": mix.clipped_samples,
        "bands_db": dict(mix.bands_db),
        "bar_dbfs": list(mix.bar_dbfs),
        "spectrogram": mix.spectrogram,
        "defects": [str(defect) for defect in mix.defects],
    }


def report_to_data(report: Report) -> JsonValue:
    """Return the report as JSON data: tuples as lists, and mappings as objects."""
    return {
        "song": report.song,
        "run": report.run,
        "song_sha256": report.song_sha256,
        "nodes": [_node_to_data(node) for node in report.nodes],
        "mix": None if report.mix is None else _mix_to_data(report.mix),
    }


def bar_dbfs(audio: np.ndarray, bar_s: float, bars: int) -> list[float]:
    """Return the RMS level in dBFS of each of the first `bars` bars."""
    mono = audio.mean(axis=0, dtype=np.float64)
    levels = []
    for index in range(bars):
        start = int(index * bar_s * SAMPLE_RATE)
        stop = min(int((index + 1) * bar_s * SAMPLE_RATE), mono.size)
        span = mono[start:stop]
        rms = math.sqrt(float(np.mean(span**2))) if span.size else 0.0
        level = 20.0 * math.log10(rms) if rms > 0 else LEVEL_FLOOR_DBFS
        levels.append(round(max(level, LEVEL_FLOOR_DBFS), 2))
    return levels


def silent_defects(measurements: Measurements) -> list[Defect]:
    return [Defect.SILENT] if measurements.peak_dbfs < SILENCE_DBFS else []


def mix_defects(measurements: Measurements) -> list[Defect]:
    defects = silent_defects(measurements)
    if measurements.clipped_samples > 0:
        defects.append(Defect.CLIPPING)
    if measurements.peak_dbfs > MIX_CEILING_DBFS:
        defects.append(Defect.HEADROOM)
    return defects


def report_failed(report: Report) -> bool:
    """Return whether the analysis failed: no mix, a node not analyzed, or any defect on a node
    or the mix."""
    if report.mix is None or report.mix.defects:
        return True
    return any(node.status is not AnalysisStatus.ANALYZED or node.defects for node in report.nodes)
