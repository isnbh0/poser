# Composing with poser

## Composing here

poser renders songs through configured instruments and measures the result. Compose here by looping:

1. Write or edit a song in a format enabled by `poser.toml`'s `adapters.song_formats`.
2. Render it: `uv run render-song <song>`.
3. Analyze it: `uv run analyze-song <song> --json`.
4. Read the report, the spectrograms, and earlier history records, then revise.

When the script format is enabled, song scripts import `poser` directly and need no PEP 723
header; `render-song` loads them in this project's environment.

## Songs

Defaults are Python (`.py`, with `build()` returning a `Song`) and YAML (`.yaml` or `.yml`).
`adapters.song_formats` can select either or other formats; consult `poser.toml` and the adapters.
If present and YAML is enabled, `songs/demo.yaml` shows that form. Both use the same song checks.
Times are beats from 0; pitches are MIDI numbers, middle C is 60. A song's last note end or
controller change must fall within 3600 seconds.

| Key | Where | Type | Range |
|---|---|---|---|
| `bpm` | song | number | above 0 |
| `tracks` | song | list of tracks | names unique |
| `name` | track | text | letters, digits, `_`, `-` |
| `sound` | track | text | a name from `uv run list-sounds` |
| `effects` | track, optional | list of recipe names | names from `poser.toml` `[effects]` |
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

A track with `effects` runs its sound through those recipes in order, as one chain per track: the
first recipe hears the track, the next hears the first, and so on. A chain's nodes are named
`<track>.<effect>`, with a repeat taking an ordinal (`bass.drive.2`). The track's `gain` still
fades the chain's end, so the fader works as before.

With the script format enabled, this short song shows the form rather than a finished piece:

```python
from poser import ControlChange, Song, Track, chord, degree, humanize, notes, pitch, scale


def build() -> Song:
    key = scale(pitch("A4"), "minor")
    line = notes([degree(key, i) for i in (0, 2, 4, 7, 6, 4, 2, 0)], 0, 0.5, 100, step=0.5)
    lead = Track(
        name="lead",
        sound="{first_instrument}",
        notes=humanize(line, seed=1, velocity=8),
        gain=-2,
        cc=(
            ControlChange(beat=0, control=1, value=0, shape="linear"),
            ControlChange(beat=4, control=1, value=127),
        ),
    )
    bass = Track(
        name="bass",
        sound="{first_instrument}",
        notes=notes([pitch("A2")], 0, 2, 100) + notes([pitch("F2")], 2, 2, 100),
        gain=-5,
    )
    pad = Track(
        name="pad",
        sound="{first_instrument}",
        notes=notes(chord(pitch("A3"), "min"), 0, 4, 70),
        gain=-8,
    )
    return Song(bpm=100, tracks=(lead, bass, pad))
```

Music helpers, from `poser`; their docstrings give the parameters:

- `pitch` — the MIDI pitch of a note name such as `C4`, `F#3`, or `Bb2`.
- `scale` — one octave of a mode (`MODES`, such as major, minor, dorian) from a root.
- `degree` — a scale's nth note from 0, continuing into other octaves.
- `chord` — a chord's pitches (`CHORDS`, such as maj, min7, sus4), optionally inverted.
- `notes` — notes from pitches, together as a chord or `step` beats apart as a line.
- `swing` — notes with every off-beat moved later, for a swung feel.
- `humanize` — notes with seeded, repeatable velocity and timing variation.

## Routing

With the script format enabled, `build()` may return a `Graph` of named `Node`s joined by `Wire`s.
An effect node names a recipe from `poser.toml`; a track node is a source and takes no wires.
A node may feed many nodes. A wire carries audio, scaled by `gain_db`, or `control`, at most one
per node and only into a recipe that documents a control input, such as a sidechain duck.

The mix sums nodes nothing feeds from, each at its own `gain_db`. A bus is an effect node fed by
sends, each scaled by its wire's `gain_db`, fading its group at the bus's own `gain_db`.
Routing a track's output into an effect removes the track's dry sound from the mix unless a dry
branch is authored. For example, wire the track to both a wet effect node and a passthrough
`sum` effect node (configured in `poser.toml` with `builtin = "sum"`). Leave both nodes without
outgoing wires; the mix includes both their outputs. The mix itself is not a wire target.

## Feedback

Loops are opt-in: `build()` returns `Graph(mode="feedback")` instead of the acyclic default. A
loop is admitted when its instantaneous graph — the wires minus every wire leaving a `delay`
node — is acyclic, so every loop passes a `delay` node whose `delay_samples` declares how far it
holds the sound back. On a loop that delay is at least one render block (512 samples); a smaller
in-loop delay is refused, and off a loop any length renders. The loop renders block by block with
previous-block feedback, so loop timing is the declared delay alone; latency compensation is not
attempted.

## Sounds

A `sound` is `<instrument>` or `<instrument>:<name>`, exactly as `uv run list-sounds` prints
it. A bare instrument name is that instrument's default sound, and each instrument has its own
names. Choose sounds by the role each track plays. This workspace's instruments are under
"Instruments here".

`list-sounds` exits 0 when all sounds are listed, 1 for a source problem, and 2 for unreadable config.

## Rendering

`uv run render-song <song>` writes one stem per node and prints each stem's path. Stems
are cached by their inputs, so only changed nodes render, a gain-only edit renders nothing, and
`--force` re-renders every node; `--json` prints a machine-readable report. Every node's inputs
include the song's tempo and total length, so lengthening the song re-renders every node.
Changing a track re-renders its chain.

Exit codes, for both `render-song` and `analyze-song`:

- 0: everything passed.
- 1: from `render-song`, a track did not end `rendered`; from `analyze-song`, a node or the mix
  failed. The report says which.
- 2: the song or `poser.toml` could not be read; the message names the problem.
- 3: another run holds this song's lock; wait and rerun.

## Reading the analysis

`uv run analyze-song <song> --json` mixes the stems with each node's `gain` and prints
the report, which names the files it wrote: the mix, and a spectrogram per node and for the mix.
Every analysis keeps a numbered record, even a failed one, holding the song as analyzed and that
run's report and images. Report fields whose meaning is not obvious from their names:

| Field | Meaning |
|---|---|
| `run` | This analysis's record number |
| `song_sha256` | Digest of the song version analyzed; equal digests mean the same song |
| `node_key` | Digest of the node's render inputs: everything but `gain`, including the song's length, the sound or recipe it resolved to, and the keys of the stems fed into it |
| `sound_key` | Digest of the sound or recipe itself; it changes when a preset, saved sound file, or effect parameters change |
| `gain_db` | The node's `gain` |
| `peak_dbfs` | Highest sample level, in dB relative to full scale |
| `integrated_lufs` | Loudness over the whole song |
| `bands_db` | Level in each frequency band, relative and uncalibrated |
| `bar_dbfs` | RMS level of each 4-beat span, up to the song's last beat |
| `detail` | Why a node has no measurements |
| `sections` | Extra analysis from configured report sections, by name; each has a `version` and either `data` with its own `defects`, or an `error` |

Node measurements are of that node's own output: the sound as played, or the effect's output
before `gain`; mix measurements are after it. Band levels are relative, so compare them between
nodes and versions, not against a target.

The bands are `sub` 20–60 Hz, `bass` 60–250 Hz, `low_mid` 250–500 Hz, `mid` 500–2000 Hz,
`high_mid` 2000–6000 Hz, and `high` 6000–20000 Hz.

Track statuses from `render-song`: `rendered`, `silent`, `unknown_sound`, `load_failed`,
`crashed`, `timed_out`, and `upstream_failed`, which means a node's upstream did not render, so
neither does this one; fix the upstream it names and render again. Node statuses from
`analyze-song`: `analyzed`, `missing_stem`, and `stale_stem`, which means the stem no longer
matches the song or its sound; `detail` says why, and rendering again fixes it.

Defects fail the analysis: `silent`, for a node or the mix; `clipping`, for mix samples beyond
full scale; and `headroom`, for a mix peak above -1 dBFS. A section's defects and errors also
fail the analysis.

`run` and `song_sha256` tell versions apart; `node_key` and `sound_key` show which nodes changed.

## Commands

- `uv run render-song` and `uv run analyze-song`: render and analyze a song, as above.
- `uv run list-sounds`: every sound a song can name, with a description.
- `uv run check-instruments`: whether each instrument loads, sounds, and repeats.
- `uv run update-guide`: rewrite `POSER.md` after `poser.toml` changes; `--check` compares it.
- `uv run save-sound`: for owners, not agents; only for instruments supporting capture.
- `uv run init-workspace --setup surge`: makes a new workspace; not needed here.

Edit `AGENTS.md` for studio preferences and instrument `guide` notes in `poser.toml` for
instrument instructions. `POSER.md` is generated from these instrument notes and poser and adapter
guides; adapter maintainers own their backend-specific guides.

## Judging a revision

Analysis finds defects and measures; it does not say a song is good. Compare a revision against an
earlier record, and treat a band or bar difference under 1 dB as possible render noise. Keep the
mix peak at or below -1 dBFS by lowering track `gain`.
