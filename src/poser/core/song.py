"""Songs: the composition as values, independent of how it is stored or rendered."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NotRequired, TypedDict, cast

from poser.core.graph import EffectNode, Graph, GraphNode, TrackNode, Wire
from poser.core.marks import (
    NO_MARKS,
    Mark,
    Marks,
    MarksData,
    layer_list_problems,
    layer_name_problems,
    layer_order_problems,
    layers_to_data,
    mark_problems,
    marks_field_problems,
)
from poser.core.names import TrackName
from poser.core.rules import (
    ControlChange,
    ControlData,
    Note,
    NoteData,
    SongError,
    cc_order_problems,
    cc_problems,
    control_change_problems,
    control_to_data,
    duplicate_name_problems,
    effects_problems,
    field_path,
    is_int,
    is_number,
    is_shape,
    last_beat,
    length_problems,
    note_problems,
    note_to_data,
    notes_problems,
    plain_number,
    song_field_problems,
    track_field_problems,
    track_problems,
    unpitched_problems,
)
from poser.core.sounds import SoundRef, parse_sound_ref

SONG_KEYS = {"bpm", "tracks"}
OPTIONAL_SONG_KEYS = {"marks"}
MARK_KEYS = {"beat", "end", "text"}
TRACK_KEYS = {"name", "sound", "notes"}
OPTIONAL_TRACK_KEYS = {"gain", "cc", "effects", "pitched"}
NOTE_KEYS = {"beat", "pitch", "length", "velocity"}
CC_KEYS = {"beat", "control", "value"}
OPTIONAL_CC_KEYS = {"shape"}


@dataclass(frozen=True)
class Track:
    name: str
    sound: str
    notes: tuple[Note, ...]
    gain: float = 0.0
    cc: tuple[ControlChange, ...] = ()
    effects: tuple[str, ...] = ()
    # False when the track's pitches select keys of its sound, such as drums in a kit, rather
    # than frequencies; its pitches are then whole.
    pitched: bool = True

    def __post_init__(self) -> None:
        values = {
            "name": self.name,
            "sound": self.sound,
            "gain": self.gain,
            "notes": self.notes,
            "cc": self.cc,
            "effects": self.effects,
            "pitched": self.pitched,
        }
        if problems := track_problems(values, "Track"):
            raise SongError(problems)
        # A list becomes a tuple, so a track built from lists equals one loaded from YAML.
        object.__setattr__(self, "notes", tuple(self.notes))
        object.__setattr__(self, "cc", tuple(self.cc))
        object.__setattr__(self, "effects", tuple(self.effects))
        object.__setattr__(self, "gain", float(self.gain))

    @property
    def sound_ref(self) -> SoundRef:
        """The track's sound as a reference; the constructor checked that it parses."""
        return parse_sound_ref(self.sound)

    @property
    def track_name(self) -> TrackName:
        """The track's name as a name; the constructor checked that it is one."""
        return TrackName(self.name)


@dataclass(frozen=True)
class Song:
    bpm: float
    tracks: tuple[Track, ...]
    # The author's marks, which no render, key, or reading uses.
    marks: Marks = NO_MARKS

    def __post_init__(self) -> None:
        values = {"bpm": self.bpm, "tracks": self.tracks, "marks": self.marks}
        if problems := _song_problems(values, "Song"):
            raise SongError(problems)
        object.__setattr__(self, "tracks", tuple(self.tracks))

    def seconds(self, beat: float) -> float:
        return beat * 60 / self.bpm

    def graph(self) -> Graph:
        """Compile the song to its graph: each track's node followed by its effect chain, wired
        in series, in song order, with the song's marks.

        Chain nodes are named `<track>.<effect>`, and a recipe a track lists twice takes an
        ordinal: `<track>.drive.2`. The track's gain compiles onto the chain's leaf, so the fader
        survives chains; the leaf's gain is in no key, so a gain-only edit renders nothing. Effect
        recipes are not checked here; `check_effect_recipes` checks them against a composition.
        """
        authored = {track.name for track in self.tracks}
        generated: set[str] = set()
        nodes: list[GraphNode] = []
        wires: list[Wire] = []
        for track in self.tracks:
            nodes.append(
                TrackNode(
                    name=track.name,
                    gain_db=0.0 if track.effects else track.gain,
                    sound=track.sound,
                    notes=track.notes,
                    cc=track.cc,
                    pitched=track.pitched,
                )
            )
            previous = track.name
            counts: dict[str, int] = {}
            for last, recipe in _leaf_flags(track.effects):
                counts[recipe] = counts.get(recipe, 0) + 1
                name = _instance_name(f"{track.name}.{recipe}", counts[recipe], generated)
                if name in authored:
                    raise SongError(
                        [
                            (
                                f"tracks: effect instance {name!r} collides with the authored "
                                f"node named {name!r}"
                            )
                        ]
                    )
                generated.add(name)
                nodes.append(
                    EffectNode(name=name, effect=recipe, gain_db=track.gain if last else 0.0)
                )
                wires.append(Wire(previous, name, "audio"))
                previous = name
        return Graph(bpm=self.bpm, nodes=tuple(nodes), wires=tuple(wires), marks=self.marks)


def song_last_beat(graph: Graph) -> float:
    """Return the latest note end or controller change across a graph's nodes."""
    return last_beat(graph.track_nodes)


def _leaf_flags(recipes: Sequence[str]) -> Sequence[tuple[bool, str]]:
    """Pair each of a chain's recipes with whether it is the chain's leaf."""
    return [(index == len(recipes) - 1, recipe) for index, recipe in enumerate(recipes)]


def _instance_name(base: str, ordinal: int, generated: set[str]) -> str:
    """Return a chain node's name: `<track>.<effect>`, a repeat taking the next ordinal.

    Ordinals keep instances distinct even from one recipe. A name an earlier chain already took
    also moves to the next ordinal, so compilation never emits two nodes with one name.
    """
    name = base if ordinal == 1 else f"{base}.{ordinal}"
    while name in generated:
        ordinal += 1
        name = f"{base}.{ordinal}"
    return name


class TrackData(TypedDict):
    name: str
    sound: str
    effects: NotRequired[list[str]]
    gain: NotRequired[float]
    notes: list[NoteData]
    cc: NotRequired[list[ControlData]]
    pitched: NotRequired[bool]


class SongData(TypedDict):
    bpm: float
    tracks: list[TrackData]
    marks: NotRequired[MarksData]


def _song_problems(values: Mapping[str, object], where: str) -> list[str]:
    """Check every field of a song built in code, whose tracks are objects."""
    problems = song_field_problems(values, where)
    if "marks" in values:
        problems += marks_field_problems(values["marks"], where)
    tracks = values["tracks"]
    if isinstance(tracks, list | tuple):
        items = cast("Sequence[object]", tracks)
        problems += [
            f"{field_path(where, 'tracks')}[{index}]: must be a Track, got {track!r}"
            for index, track in enumerate(items)
            if not isinstance(track, Track)
        ]
        problems += duplicate_name_problems([t for t in items if isinstance(t, Track)], where)
        bpm = values["bpm"]
        if not problems and is_number(bpm):
            problems += length_problems(bpm, [t for t in items if isinstance(t, Track)], where)
    return problems


def _mapping(value: object) -> Mapping[object, object] | None:
    """Return loaded YAML as a mapping of untyped keys and values, or None when it is not one."""
    return value if isinstance(value, dict) else None


def _present(data: Mapping[object, object], keys: set[str]) -> dict[str, object]:
    """Return the values of the song-format keys present in loaded data."""
    return {key: data[key] for key in keys if key in data}


def _check_keys(
    data: Mapping[object, object],
    required: set[str],
    where: str,
    problems: list[str],
    optional: frozenset[str] | set[str] = frozenset(),
) -> None:
    prefix = f"{where}: " if where else ""
    problems.extend(
        f"{prefix}unknown key {key!r}"
        for key in data
        if key not in required and key not in optional
    )
    problems.extend(f"{prefix}missing key {key!r}" for key in sorted(required - set(data)))


def _parse_note(value: object, where: str, problems: list[str]) -> Note | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, NOTE_KEYS, where, problems)

    field_problems = note_problems(_present(data, NOTE_KEYS), where)
    problems += field_problems
    beat, pitch = data.get("beat"), data.get("pitch")
    length, velocity = data.get("length"), data.get("velocity")
    # These fail only when a problem was reported. A missing key reads as None.
    if field_problems or not (
        is_number(beat) and is_number(pitch) and is_number(length) and is_int(velocity)
    ):
        return None
    return Note(beat=beat, pitch=pitch, length=length, velocity=velocity)


def _parse_control_change(value: object, where: str, problems: list[str]) -> ControlChange | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, CC_KEYS, where, problems, OPTIONAL_CC_KEYS)

    field_problems = control_change_problems(_present(data, CC_KEYS | OPTIONAL_CC_KEYS), where)
    problems += field_problems
    beat, control, cc_value = data.get("beat"), data.get("control"), data.get("value")
    shape = data.get("shape", "step")
    # These fail only when a problem was reported. A missing key reads as None.
    if field_problems or not (
        is_number(beat) and is_int(control) and is_int(cc_value) and is_shape(shape)
    ):
        return None
    return ControlChange(beat=beat, control=control, value=cc_value, shape=shape)


def _parse_cc(value: object, where: str, problems: list[str]) -> tuple[ControlChange, ...]:
    """Parse a track's controller changes, which must be in time order."""
    container_problems = cc_problems(value, where)
    problems += container_problems
    if container_problems or not isinstance(value, list):
        return ()
    parsed = [
        (index, _parse_control_change(item, f"{where}.cc[{index}]", problems))
        for index, item in enumerate(value)
    ]
    changes = [(index, change) for index, change in parsed if change is not None]
    problems += cc_order_problems(changes, where, complete=len(changes) == len(value))
    return tuple(change for _, change in changes)


def _parse_track(value: object, where: str, problems: list[str]) -> Track | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, TRACK_KEYS, where, problems, OPTIONAL_TRACK_KEYS)

    before = len(problems)
    problems += track_field_problems(_present(data, TRACK_KEYS | OPTIONAL_TRACK_KEYS), where)
    if "effects" in data:
        problems += effects_problems(data["effects"], where)

    raw_notes = data.get("notes")
    notes: list[Note | None] = []
    if "notes" in data:
        note_list_problems = notes_problems(raw_notes, where)
        problems += note_list_problems
        if not note_list_problems and isinstance(raw_notes, list):
            notes = [
                _parse_note(note, f"{where}.notes[{index}]", problems)
                for index, note in enumerate(raw_notes)
            ]

    cc = _parse_cc(data["cc"], where, problems) if "cc" in data else ()

    pitched = data.get("pitched", True)
    if pitched is False:
        valid = [(index, note) for index, note in enumerate(notes) if note is not None]
        problems += unpitched_problems(valid, where)

    name, sound = data.get("name"), data.get("sound")
    gain = data.get("gain", 0.0)
    raw_effects = data.get("effects", [])
    effects = tuple(raw_effects) if isinstance(raw_effects, list) else ()
    # Each of these fails only when a problem was reported. A missing key reads as None.
    if len(problems) > before or not is_number(gain) or "notes" not in data:
        return None
    if not (isinstance(name, str) and isinstance(sound, str) and isinstance(pitched, bool)):
        return None
    return Track(
        name=name,
        sound=sound,
        notes=tuple(n for n in notes if n is not None),
        gain=gain,
        cc=cc,
        effects=effects,
        pitched=pitched,
    )


def _parse_mark(value: object, where: str, problems: list[str]) -> Mark | None:
    data = _mapping(value)
    if data is None:
        problems.append(f"{where}: must be a mapping, got {value!r}")
        return None
    _check_keys(data, MARK_KEYS, where, problems)

    field_problems = mark_problems(_present(data, MARK_KEYS), where)
    problems += field_problems
    beat, end, text = data.get("beat"), data.get("end"), data.get("text")
    # These fail only when a problem was reported. A missing key reads as None.
    if field_problems or not (is_number(beat) and is_number(end) and isinstance(text, str)):
        return None
    return Mark(beat=beat, end=end, text=text)


def _parse_marks(value: object, problems: list[str]) -> Marks:
    """Parse a song's marks, reporting every problem at its path, such as `marks.chords[1]`."""
    data = _mapping(value)
    if data is None:
        problems.append(f"marks: must be a mapping of layer names to marks, got {value!r}")
        return Marks()
    before = len(problems)
    layers: dict[str, list[Mark]] = {}
    for name, raw in data.items():
        where = f"marks.{name}"
        problems += layer_name_problems(name, "marks")
        list_problems = layer_list_problems(raw, where)
        problems += list_problems
        if list_problems or not isinstance(raw, list):
            continue
        parsed = [
            (index, _parse_mark(item, f"{where}[{index}]", problems))
            for index, item in enumerate(raw)
        ]
        valid = [(index, mark) for index, mark in parsed if mark is not None]
        problems += layer_order_problems(valid, where)
        if isinstance(name, str):
            layers[name] = [mark for _, mark in valid]
    # Marks are built only from a layer set without problems, which the constructor accepts.
    return Marks() if len(problems) > before else Marks(layers)


def parse_song(value: object) -> Song:
    """Validate loaded song data, reporting every problem found rather than only the first."""
    data = _mapping(value)
    if data is None:
        raise SongError([f"song must be a mapping, got {value!r}"])

    problems: list[str] = []
    _check_keys(data, SONG_KEYS, "", problems, OPTIONAL_SONG_KEYS)
    problems += song_field_problems(_present(data, SONG_KEYS), "")

    raw_tracks = data.get("tracks")
    tracks: list[Track] = []
    if isinstance(raw_tracks, list):
        parsed = [
            _parse_track(track, f"tracks[{index}]", problems)
            for index, track in enumerate(raw_tracks)
        ]
        tracks = [track for track in parsed if track is not None]
    problems += duplicate_name_problems(tracks, "")
    marks = _parse_marks(data["marks"], problems) if "marks" in data else Marks()

    bpm = data.get("bpm")
    # `bpm` fails `is_number` only when a problem was reported.
    if problems or not is_number(bpm):
        raise SongError(problems)
    if problems := length_problems(bpm, tracks, "song"):
        raise SongError(problems)
    return Song(bpm=bpm, tracks=tuple(tracks), marks=marks)


def _track_to_data(track: Track) -> TrackData:
    data: TrackData = {
        "name": track.name,
        "sound": track.sound,
        "effects": list(track.effects),
        "gain": track.gain,
        "notes": [note_to_data(note) for note in track.notes],
        "cc": [control_to_data(change) for change in track.cc],
        "pitched": track.pitched,
    }
    # Defaults are left out, so a song without gains or controller changes keeps its old text.
    if not track.effects:
        del data["effects"]
    if track.gain == 0:
        del data["gain"]
    if not track.cc:
        del data["cc"]
    if track.pitched:
        del data["pitched"]
    return data


def song_to_data(song: Song) -> SongData:
    """Return a song as plain data in the song format's key order, the inverse of `parse_song`.

    Numbers are written in one spelling, so equal songs give equal data.
    """
    data: SongData = {
        "bpm": plain_number(song.bpm),
        "tracks": [_track_to_data(track) for track in song.tracks],
    }
    # A song without marks keeps the record text it had before marks existed.
    if song.marks:
        data["marks"] = layers_to_data(song.marks)
    return data
