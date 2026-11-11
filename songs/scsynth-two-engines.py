"""A two-engine demo: a scsynth synthesis track beside a plugin track.

The two engines render as independent leaves that mix as one song. The script needs a workspace
whose `poser.toml` names the scsynth adapter as `sc` and a working plugin instrument as `surge`;
`render-song` and `analyze-song` run it where both engines exist. Both instrument tables must
be configured; a missing engine binary fails that track independently.
"""

from poser import Note, Song, Track


def build() -> Song:
    return Song(
        100,
        (
            Track(
                "synth",
                "sc:builtin/additive-voice",
                (
                    Note(0, 69, 2, 100),
                    Note(2, 76, 1.5, 90),
                    Note(4, 72, 2, 100),
                ),
                gain=-12,
            ),
            Track(
                "strings",
                "surge",
                (
                    Note(0, 57, 4, 90),
                    Note(4, 53, 4, 90),
                ),
            ),
        ),
    )
