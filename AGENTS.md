# Composing with poser

## Composing here

This project turns a song written as code into audio played by real VST instruments, then measures
the result. Run commands from this directory. You compose by looping:

1. Write or edit a song in `songs/`.
2. Render it: `uv run render-song songs/<name>.py`.
3. Analyze it: `uv run analyze-song songs/<name>.py --json`.
4. Read the report, the spectrograms, and earlier history records, then revise.

Song scripts import `poser` directly and need no PEP 723 header, because `render-song`
loads them in this project's environment.

## Songs

A song is `songs/<name>.py`, whose `build()` returns a `Song`, or `songs/<name>.yaml`.
`songs/demo.yaml` shows the YAML form; both forms use the same keys and checks.
Times are in beats from 0, and pitches are MIDI numbers, where middle C is 60.

| Key | Where | Type | Range |
|---|---|---|---|
| `bpm` | song | number | above 0 |
| `tracks` | song | list of tracks | names unique |
| `name` | track | text | letters, digits, `_`, `-` |
| `engine` | track | text | `pedalboard` |
| `sound` | track | text | a name from `uv run list-sounds` |
| `notes` | track | list of notes | |
| `gain` | track, optional | decibels | -60 to 12 |
| `cc` | track, optional | list of controller changes, in time order | |
| `beat` | note, controller change | number | 0 or later |
| `pitch` | note | integer | 0 to 127 |
| `length` | note | beats | above 0 |
| `velocity` | note | integer | 1 to 127 |
| `control` | controller change | integer | 0 to 119 |
| `value` | controller change | integer | 0 to 127 |
| `shape` | controller change, optional | `step` or `linear` | |

A `step` change holds its value until the next change to that controller. A `linear` change ramps
to the next change to the same controller, so it needs a later one.

A short song script, showing the form rather than a finished piece:

```python
from poser.music import chord, degree, humanize, notes, pitch, scale
from poser.song import ControlChange, Song, Track


def build() -> Song:
    key = scale(pitch("A4"), "minor")
    line = notes([degree(key, i) for i in (0, 2, 4, 7, 6, 4, 2, 0)], 0, 0.5, 100, step=0.5)
    lead = Track(
        name="lead",
        engine="pedalboard",
        sound="Serum:Leads/LD A Bit of Luck [SD]",
        notes=humanize(line, seed=1, velocity=8),
        gain=-2,
        cc=(
            ControlChange(beat=0, control=1, value=0, shape="linear"),
            ControlChange(beat=4, control=1, value=127),
        ),
    )
    bass = Track(
        name="bass",
        engine="pedalboard",
        sound="Serum:Bass/BA Ampology [CFA]",
        notes=notes([pitch("A2")], 0, 2, 100) + notes([pitch("F2")], 2, 2, 100),
        gain=-2,
    )
    pad = Track(
        name="pad",
        engine="pedalboard",
        sound="Serum:Pads/PD Celestial Light [SW]",
        notes=notes(chord(pitch("A3"), "min"), 0, 4, 70),
        gain=-8,
    )
    return Song(bpm=100, tracks=(lead, bass, pad))
```

Music helpers, in `poser.music`; their docstrings give the parameters:

- `pitch` — the MIDI pitch of a note name such as `C4`, `F#3`, or `Bb2`.
- `scale` — one octave of a mode (`MODES`, such as major, minor, dorian) from a root.
- `degree` — a scale's nth note from 0, continuing into other octaves.
- `chord` — a chord's pitches (`CHORDS`, such as maj, min7, sus4), optionally inverted.
- `notes` — notes from pitches, together as a chord or `step` beats apart as a line.
- `swing` — notes with every off-beat moved later, for a swung feel.
- `humanize` — notes with seeded, repeatable velocity and timing variation.

`make check` loads every song in `songs/` and checks song scripts' formatting, lint, and types.

## Sounds

`uv run list-sounds` lists plugin defaults, saved sounds, and installed presets. A `sound` must
match a listed name exactly. Choose presets by the role each track plays. Primer crashes on render,
and Serum's default patch neither repeats between renders nor responds to controllers.

`save-sound` opens a plugin window for the owner to shape a sound by hand, so it is not for agents.

## Rendering

`uv run render-song songs/<name>.py` writes one stem per track to `renders/<song path>/`, such as
`renders/songs/demo.yaml/` for `songs/demo.yaml`. Stems are cached by their inputs, so only
changed tracks render, and a gain-only edit renders nothing.
Every track's inputs include the song's tempo and total length, so lengthening the song re-renders
every track. `--force` re-renders every track. Add `--json` for a machine-readable report.

Exit codes, for both `render-song` and `analyze-song`:

- 0: everything passed.
- 1: from `render-song`, a track did not end `rendered`; from `analyze-song`, a track or the mix
  failed. The report says which.
- 2: the song or plugin list could not be read; the message names the problem.
- 3: another run holds this song's lock; wait and rerun.

## Reading the analysis

`uv run analyze-song songs/<name>.py --json` mixes the stems with each track's `gain` and prints
the report. Report fields whose meaning is not obvious from their names:

| Field | Meaning |
|---|---|
| `run` | This analysis's number under `history/` |
| `song_sha256` | Digest of the song version analyzed; equal digests mean the same song |
| `track_key` | Digest of the track's render inputs: everything but `gain`, including the song's length |
| `sound_key` | Digest of the sound itself; it changes when a preset or saved sound file changes |
| `gain_db` | The track's `gain` |
| `peak_dbfs` | Highest sample level, in dB relative to full scale |
| `integrated_lufs` | Loudness over the whole song |
| `bands_db` | Level in each frequency band, relative and uncalibrated |
| `bar_dbfs` | RMS level of each 4-beat span, up to the song's last beat |
| `detail` | Why a track has no measurements |

Track measurements are of the stem before `gain`; mix measurements are after it. Band levels are
relative, so compare them between tracks and versions, not against a target.

The bands are `sub` 20–60 Hz, `bass` 60–250 Hz, `low_mid` 250–500 Hz, `mid` 500–2000 Hz,
`high_mid` 2000–6000 Hz, and `high` 6000–20000 Hz.

Track statuses from `render-song`: `rendered`, `silent`, `unknown_sound`, `load_failed`,
`crashed`, and `timed_out`. Track statuses from `analyze-song`: `analyzed`, `missing_stem`, and
`stale_stem`, which means the stem no longer matches the song or its sound; `detail` says why, and
rendering again fixes it.

Defects fail the analysis: `silent`, for a track or the mix; `clipping`, for mix samples beyond
full scale; and `headroom`, for a mix peak above -1 dBFS.

Results live in `renders/<song path>/`. `analysis/` and `mix/` exist only when every track
analyzed:

- `analysis/report.json` and `analysis/<track>.png`, one spectrogram per track.
- `mix/mix.wav` and `mix/mix.png`. The PNGs are images you can view.
- `history/<n>/`, one record per analysis, written even when the analysis fails: the song as
  `song.yaml`, also for a script song, `report.json`, and the spectrograms.
  `run` and `song_sha256` tell versions apart; `track_key` and `sound_key` show which tracks
  changed.

## Judging a revision

Analysis finds defects and measures; it does not say a song is good. Compare a revision against an
earlier history record. Treat a band or
bar difference under 1 dB as possible render noise.

Controllers do nothing in Serum here except CC 1, the mod wheel, in presets that route it. CC 1 on
`Serum:Leads/LD A Bit of Luck [SD]` is confirmed. On another preset, compare history records with
the lane low and high to see whether it routes CC 1.

Keep the mix peak at or below -1 dBFS by lowering track `gain`.
