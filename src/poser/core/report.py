"""The analysis report: its schema, its JSON form, and the built-in analysis policy."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, cast

import numpy as np

from poser.core.audio import SAMPLE_RATE, SILENCE_DBFS
from poser.core.graph import NODE_KINDS, NodeKind
from poser.core.jsonvalue import JsonValue
from poser.core.song import is_int, is_number
from poser.core.statuses import SkipStatus, SoundFailureStatus, UnrenderedStatus

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


def _key(owner: str, name: str, value: object, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not (isinstance(value, str) and value):
        raise ValueError(f"{owner}.{name} must be a non-empty str, got {value!r}")


def _detail(owner: str, value: object) -> None:
    if not (isinstance(value, str) and value):
        raise ValueError(f"{owner}.detail must be a non-empty str, got {value!r}")


@dataclass(frozen=True)
class _Node:
    """What every node record holds: the node as the graph has it."""

    name: str
    kind: NodeKind
    upstream: tuple[str, ...]
    gain_db: float

    def __post_init__(self) -> None:
        owner = type(self).__name__
        if not isinstance(self.name, str):
            raise ValueError(f"{owner}.name must be a str, got {self.name!r}")
        if self.kind not in NODE_KINDS:
            raise ValueError(f"{owner}.kind must be one of {NODE_KINDS}, got {self.kind!r}")
        _finite(owner, "gain_db", self.gain_db)
        object.__setattr__(self, "upstream", _members(owner, "upstream", self.upstream, str))


@dataclass(frozen=True)
class _Measured(_Node):
    """A node whose stem was read and measured.

    The keys are the ones a fresh stem of the node would carry, so two reports show which nodes
    changed between song versions. `node_key` is the key of the node's render unit, shared by a
    cycle group's members. `sound_key` digests its sound or recipe, and is None for a delay, which
    has neither.
    """

    node_key: str
    sound_key: str | None
    measurements: Measurements
    defects: tuple[Defect, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        owner = type(self).__name__
        _key(owner, "node_key", self.node_key)
        _key(owner, "sound_key", self.sound_key, optional=True)
        if not isinstance(self.measurements, Measurements):
            raise ValueError(f"{owner}.measurements must be Measurements: {self.measurements!r}")
        object.__setattr__(self, "defects", _members(owner, "defects", self.defects, Defect))


@dataclass(frozen=True)
class AnalyzedNode(_Measured):
    """A node of a complete analysis, drawn as `spectrogram`, a path relative to the record."""

    spectrogram: str
    status: ClassVar[str] = "analyzed"

    def __post_init__(self) -> None:
        super().__post_init__()
        _key("AnalyzedNode", "spectrogram", self.spectrogram)


@dataclass(frozen=True)
class MeasuredNode(_Measured):
    """A readable node of an incomplete analysis, which draws no images."""

    status: ClassVar[str] = "measured"


@dataclass(frozen=True)
class UnrenderedNode(_Node):
    """A node the plan keys with no readable stem under its key."""

    node_key: str
    sound_key: str | None
    status: UnrenderedStatus
    detail: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _key("UnrenderedNode", "node_key", self.node_key)
        _key("UnrenderedNode", "sound_key", self.sound_key, optional=True)
        if not isinstance(self.status, UnrenderedStatus):
            raise ValueError(f"UnrenderedNode.status must be an UnrenderedStatus: {self.status!r}")
        _detail("UnrenderedNode", self.detail)


@dataclass(frozen=True)
class UnplannedNode(_Node):
    """A node the plan cannot key, with the status render-song gives it. `sound_key` is None when
    the node's own sound or recipe does not resolve, or for a delay."""

    sound_key: str | None
    status: SoundFailureStatus | SkipStatus
    detail: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _key("UnplannedNode", "sound_key", self.sound_key, optional=True)
        if not isinstance(self.status, SoundFailureStatus | SkipStatus):
            raise ValueError(f"UnplannedNode.status must be a plan failure: {self.status!r}")
        _detail("UnplannedNode", self.detail)


type NodeRecord = AnalyzedNode | MeasuredNode | UnrenderedNode | UnplannedNode

# Every node status value a report can hold.
ANALYSIS_STATUSES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            AnalyzedNode.status,
            MeasuredNode.status,
            *(str(status) for status in UnrenderedStatus),
            *(str(status) for status in SoundFailureStatus),
            *(str(status) for status in SkipStatus),
        ]
    )
)


@dataclass(frozen=True)
class MixReport:
    path: str
    measurements: Measurements
    spectrogram: str
    defects: tuple[Defect, ...]

    def __post_init__(self) -> None:
        _key("MixReport", "path", self.path)
        if not isinstance(self.measurements, Measurements):
            raise ValueError(f"MixReport.measurements must be Measurements: {self.measurements!r}")
        _key("MixReport", "spectrogram", self.spectrogram)
        object.__setattr__(self, "defects", _members("MixReport", "defects", self.defects, Defect))


def _identity(owner: str, song: object, run: object, song_sha256: object) -> None:
    if not isinstance(song, str):
        raise ValueError(f"{owner}.song must be a str, got {song!r}")
    if isinstance(run, bool) or not isinstance(run, int) or run < 1:
        raise ValueError(f"{owner}.run must be a positive int, got {run!r}")
    if not isinstance(song_sha256, str):
        raise ValueError(f"{owner}.song_sha256 must be a str, got {song_sha256!r}")


@dataclass(frozen=True)
class CompleteReport:
    """An analysis in which every node analyzed, so it has a mix. `run` numbers its record under
    `history/`, and `song_sha256` digests the song text that record holds, so one song version
    always has one digest."""

    song: str
    run: int
    song_sha256: str
    nodes: tuple[AnalyzedNode, ...]
    mix: MixReport

    def __post_init__(self) -> None:
        _identity("CompleteReport", self.song, self.run, self.song_sha256)
        nodes = _members("CompleteReport", "nodes", self.nodes, AnalyzedNode)
        object.__setattr__(self, "nodes", nodes)
        if not isinstance(self.mix, MixReport):
            raise ValueError(f"CompleteReport.mix must be a MixReport, got {self.mix!r}")


@dataclass(frozen=True)
class IncompleteReport:
    """An analysis in which some node has no readable stem, so nothing is mixed or drawn."""

    song: str
    run: int
    song_sha256: str
    nodes: tuple[MeasuredNode | UnrenderedNode | UnplannedNode, ...]

    def __post_init__(self) -> None:
        _identity("IncompleteReport", self.song, self.run, self.song_sha256)
        nodes = _members("IncompleteReport", "nodes", self.nodes, _Node)
        if any(isinstance(node, AnalyzedNode) for node in nodes):
            raise ValueError("an IncompleteReport holds measured nodes, not analyzed ones")
        if all(isinstance(node, MeasuredNode) for node in nodes):
            raise ValueError("an IncompleteReport needs a node that is not measured")
        object.__setattr__(self, "nodes", nodes)


type Report = CompleteReport | IncompleteReport


def _measurements_to_data(measurements: Measurements) -> JsonValue:
    return {
        "peak_dbfs": measurements.peak_dbfs,
        "integrated_lufs": measurements.integrated_lufs,
        "clipped_samples": measurements.clipped_samples,
        "bands_db": dict(measurements.bands_db),
        "bar_dbfs": list(measurements.bar_dbfs),
    }


def _node_to_data(node: NodeRecord) -> JsonValue:
    data: dict[str, JsonValue] = {
        "name": node.name,
        "kind": node.kind,
        "upstream": list(node.upstream),
        "gain_db": node.gain_db,
        "status": str(node.status),
    }
    match node:
        case AnalyzedNode() | MeasuredNode():
            data.update(
                node_key=node.node_key,
                sound_key=node.sound_key,
                measurements=_measurements_to_data(node.measurements),
                defects=[str(defect) for defect in node.defects],
            )
            if isinstance(node, AnalyzedNode):
                data["spectrogram"] = node.spectrogram
        case UnrenderedNode():
            data.update(node_key=node.node_key, sound_key=node.sound_key, detail=node.detail)
        case UnplannedNode():
            data.update(sound_key=node.sound_key, detail=node.detail)
    return data


def _mix_to_data(mix: MixReport) -> JsonValue:
    return {
        "path": mix.path,
        "measurements": _measurements_to_data(mix.measurements),
        "spectrogram": mix.spectrogram,
        "defects": [str(defect) for defect in mix.defects],
    }


def report_to_data(report: Report) -> JsonValue:
    """Return the report as JSON data: tuples as lists, and mappings as objects."""
    data: dict[str, JsonValue] = {
        "status": "complete" if isinstance(report, CompleteReport) else "incomplete",
        "song": report.song,
        "run": report.run,
        "song_sha256": report.song_sha256,
        "nodes": [_node_to_data(node) for node in report.nodes],
    }
    if isinstance(report, CompleteReport):
        data["mix"] = _mix_to_data(report.mix)
    return data


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
    """Return whether the analysis failed: it is incomplete, or a node or the mix has a defect."""
    match report:
        case IncompleteReport():
            return True
        case CompleteReport():
            return bool(report.mix.defects) or any(node.defects for node in report.nodes)
