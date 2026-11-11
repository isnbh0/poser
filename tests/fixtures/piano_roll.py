"""Plugin-free visual acceptance fixture: authored events, not audio."""

from poser import Note, Song, Track


def build() -> Song:
    return Song(
        bpm=117.5,
        tracks=(
            Track(
                "Sparse_low_register_with_long_sustained_notes",
                "unused",
                (Note(0, 0, 8, 25), Note(12, 12, 0.5, 127), Note(24, 24, 6, 80)),
            ),
            Track(
                "Dense_overlapping_middle_register_velocity_study",
                "unused",
                tuple(Note(i / 4, 48 + i % 24, 1.5, 1 + i % 127) for i in range(128)),
            ),
            Track(
                "High_register_short_and_long_authored_durations",
                "unused",
                (Note(1, 127, 0.25, 127), Note(4, 120, 6, 65), Note(20, 110, 10, 100)),
            ),
            Track(
                "Duplicate_events_and_fractional_timing_overlap",
                "unused",
                (
                    Note(0.125, 60, 2.75, 30),
                    Note(0.125, 60, 2.75, 30),
                    Note(1.5, 60, 4, 110),
                    Note(8, 64, 0.25, 127),
                    Note(16, 67, 12, 70),
                ),
            ),
        ),
    )
