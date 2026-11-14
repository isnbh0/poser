"""Seeded song generators, so every generated case is reproducible from its seed.

The framework calibration tests read them, and the lens laws transform them.
"""

import random
from typing import Literal

from poser.core.rules import Note
from poser.core.song import Song, Track

type Mode = Literal["major", "minor"]

SOUND = "synth"
BPM = 120
LENGTHS = (0.25, 0.5, 1, 2)
OFFSETS = (0.25, 0.5)
SCALES: dict[Mode, tuple[int, ...]] = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),  # Natural minor.
}
PROGRESSIONS = ((1, 4, 5, 1), (1, 6, 4, 5))


def random_song(seed: int) -> Song:
    """One to three pitched tracks of 4 to 40 random notes in the first 48 beats.

    Beats are multiples of 0.25, and a fifth of the pitches are a quarter or half semitone off a
    whole pitch. Every value is exact in binary floating point.
    """
    rng = random.Random(seed)
    tracks = []
    for index in range(rng.randint(1, 3)):
        notes = []
        for _ in range(rng.randint(4, 40)):
            beat = rng.randrange(192) * 0.25
            length = rng.choice(LENGTHS)
            pitch: float = rng.randint(24, 103)
            if rng.random() < 0.2:
                pitch += rng.choice(OFFSETS)
            notes.append(Note(beat, pitch, length, rng.randint(1, 127)))
        tracks.append(Track(f"t{index}", SOUND, tuple(notes)))
    return Song(BPM, tuple(tracks))


def noise_cluster(seed: int) -> Song:
    """A 16-beat cluster: one to three notes of each of the twelve pitch classes, at random beats
    and octaves, about a beat long."""
    rng = random.Random(seed)
    notes = []
    for pc in range(12):
        for _ in range(rng.randint(1, 3)):
            beat = rng.uniform(0, 16)
            pitch = rng.choice((48, 60, 72)) + pc
            notes.append(Note(beat, pitch, rng.uniform(0.7, 1.3), 100))
    return Song(BPM, (Track("cluster", SOUND, tuple(notes)),))


def _scale_pitch(base: int, mode: Mode, index: int) -> int:
    """The pitch of scale step `index`, counted from 0 at `base` and continuing into octaves."""
    return base + 12 * (index // 7) + SCALES[mode][index % 7]


def tonal_song(seed: int, bar: int = 4) -> tuple[Song, tuple[int, Mode]]:
    """A song in a random key, in bars of `bar` beats, and that key as `(tonic, mode)`.

    Four repeats of a progression, each chord a one-bar triad under one quarter-note melody note
    per beat of the bar, every note on the mode's scale. Each melody note is the chord's root,
    third, or fifth, or a random scale degree. The song declares no meter.
    """
    rng = random.Random(seed)
    tonic = rng.randrange(12)
    mode: Mode = rng.choice(("major", "minor"))
    progression = rng.choice(PROGRESSIONS)
    chords: list[Note] = []
    melody: list[Note] = []
    beat = 0
    for _ in range(4):
        for degree in progression:
            root = degree - 1
            chords.extend(
                Note(beat, _scale_pitch(48 + tonic, mode, step), bar, 90)
                for step in (root, root + 2, root + 4)
            )
            for quarter in range(bar):
                choice = rng.randrange(4)
                step = rng.randrange(7) if choice == 3 else root + 2 * choice
                melody.append(Note(beat + quarter, _scale_pitch(72 + tonic, mode, step), 1, 100))
            beat += bar
    tracks = (Track("chords", SOUND, tuple(chords)), Track("melody", SOUND, tuple(melody)))
    return Song(BPM, tracks), (tonic, mode)


NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def chord_labels(song: Song) -> tuple[tuple[float, float, str], ...]:
    """The chords of a `tonal_song`, in order, as `(beat, end, label)`.

    Each label names the major or minor triad on the lowest of the notes its `chords` track
    starts at one beat, such as `F#m`.
    """
    (track,) = (track for track in song.tracks if track.name == "chords")
    by_beat: dict[float, list[Note]] = {}
    for note in track.notes:
        by_beat.setdefault(note.beat, []).append(note)
    labels = []
    for beat, notes in sorted(by_beat.items()):
        root, third, _ = sorted(note.pitch for note in notes)
        suffix = {3: "m", 4: ""}[int(third - root)]
        labels.append((beat, beat + notes[0].length, NAMES[int(root) % 12] + suffix))
    return tuple(labels)
