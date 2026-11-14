"""Optional 16-bar miniature; factory IDs discovered on Surge XT 1.3.4.

See docs/showcase.md for rendering evidence and listening limitations.
This is not an initialization resource or an advice-free syntax example.
"""

from poser import Mark, Marks, Meter, MeterSpan, Note, Song, Track


def build() -> Song:
    """A sparse opening, layered middle, and closing return."""
    progression = (("Am", 45), ("F", 41), ("C", 48), ("G", 43)) * 4
    roots = tuple(root for _, root in progression)
    bass = tuple(
        Note(bar * 4 + offset, root, 1.5, 76 if offset == 0 else 65)
        for bar, root in enumerate(roots)
        for offset in (0, 2)
    )
    harmony = tuple(
        Note(bar * 4 + offset, pitch, 0.7, 62)
        for bar, root in enumerate(roots)
        if 4 <= bar < 12
        for offset in (0.5, 2.5)
        for pitch in (root + 12, root + 19, root + 24)
    )
    motif = (69, 72, 76, 74, 72, 71, 67, 69)
    melody = (
        *(
            Note(bar * 4 + offset, motif[(bar + step) % len(motif)], 0.75, 72 + step * 3)
            for bar in range(2, 15)
            for step, offset in enumerate((0, 1.5, 3))
        ),
        Note(60, 69, 3.5, 66),
    )
    return Song(
        108,
        (
            Track("bass", "surge:presets/Basses/Digibass", bass, gain=-15),
            Track("pulse", "surge:presets/Polysynths/Past Tense", harmony, gain=-21),
            Track("melody", "surge:presets/Winds/Flute 1", melody, gain=-12),
        ),
        marks=Marks(
            {
                "chords": [
                    Mark(bar * 4, bar * 4 + 4, name) for bar, (name, _) in enumerate(progression)
                ],
                "key": [Mark(0, 64, "A minor")],
            }
        ),
        meter=Meter([MeterSpan(0, 4)]),
    )
