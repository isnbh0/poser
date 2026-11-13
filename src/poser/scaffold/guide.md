# Composing with poser

## Operational contract

poser renders songs through configured instruments and measures the result.

- Creator intent and owner requirements in `AGENTS.md` override this guide and any advice.
- Preserve owner work.
- If composition was requested, continue after successful setup without asking again; ask only
  for missing consequential intent or permissions.
- Playback requires permission. Report a song as "not auditioned" unless someone listened;
  starting a player is not listening.
- A passing analysis means no defects were found.
- Never deliver an earlier mix as current after a failed attempt.

## Songs

Defaults are Python (`.py`, with `build()` returning a `Song`) and YAML (`.yaml` or `.yml`).
`adapters.song_formats` can enable either, both, or other formats; consult `poser.toml` and the
adapters. If present and YAML is enabled, `songs/demo.yaml` shows that form. Every song format
uses the same keys and checks. Times are beats from 0; pitches are numbers on the MIDI scale:
middle C is 60, A4 is 69 = 440 Hz, and each whole step of 1 is a twelve-tone equal-tempered
semitone. Any number from 0 to 127 is allowed, so 60.5 is a quarter tone above middle C. An
instrument plays either any pitch or only whole pitches; its adapter's section of this guide says
which. Mark a track `pitched: false` when its pitches pick drums or other keys rather than notes;
its pitches must then be whole.
A song's last note end or controller change must fall within 3600 seconds.

When the script format is enabled, write song scripts that import `poser` directly and run in
this project's environment; omit any PEP 723 header. Loading a Python song runs its `build()`
code, so load only trusted scripts. This short song shows the form:

```python
from poser import Song, Track, notes


def build() -> Song:
    track = Track(name="example", sound="{first_instrument}", notes=notes([60], 0, 1, 100))
    return Song(bpm=100, tracks=(track,))
```

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
| `pitched` | track, optional | true or false | false for drums and other key-mapped sounds |
| `beat` | note, controller change | number | 0 or later |
| `pitch` | note | number | 0 to 127 |
| `length` | note | beats | above 0 |
| `velocity` | note | integer | 1 to 127 |
| `control` | controller change | integer | 0 to 119 |
| `value` | controller change | integer | 0 to 127 |
| `shape` | controller change, optional | `step` or `linear` | |

A `step` holds until that controller's next change; `linear` ramps to it and needs a later one.

Track `effects` form a recipe chain: the first hears the track, each next hears its predecessor.
Nodes are named `<track>.<effect>`, with repeats numbered (`bass.drive.2`). Track `gain` fades
the chain's end.

Music helpers, from `poser`; their docstrings give the parameters:
- `pitch` — the MIDI pitch of a note name such as `C4`, `F#3`, or `Bb2`.
- `scale` — one octave of a mode (such as major, minor, dorian) from a root.
- `degree` — a scale's nth note from 0, continuing into other octaves.
- `chord` — a chord's pitches (such as maj, min7, sus4), optionally inverted.
- `notes` — notes from pitches, together as a chord or `step` beats apart as a line.
- `swing` — notes with every off-beat moved later, for a swung feel.
- `humanize` — notes with seeded, repeatable velocity and timing variation.

## Sounds

A `sound` is `<instrument>` (its default) or `<instrument>:<name>`, as `uv run list-sounds` prints.
See "Instruments here" for this workspace. `uv run list-sounds --json` returns machine-readable
listings. Repeat `--instrument <name>` for exact configured instrument names; `--match
<substring>` filters IDs and descriptions case-insensitively.

## Rendering

Core audio is stereo (2 channels) at 48000 Hz. Rendering adds a fixed 2 seconds after the last
note end or controller change, so longer releases may be cut off.

`uv run render-song <song>` writes and prints a stem path per node, caching by inputs, so only
changed nodes render. Inputs include tempo and total length, so lengthening the song re-renders
every node; changing a track re-renders its chain; gain-only edits render nothing. `--force`
re-renders all nodes, and `--json` prints a machine-readable report.

Exit codes, for both `render-song` and `analyze-song`:
- 0: everything passed.
- 1: from `render-song`, a track did not end `rendered`; from `analyze-song`, a node or the mix
  failed. The report says which.
- 2: the song or `poser.toml` could not be read; the message names the problem.
- 3: another run holds this song's lock; wait and rerun.

## Reading the analysis

`uv run analyze-song <song> --json` mixes leaf stems with each leaf node's `gain_db` and prints
the report, which names the files it wrote: the mix, and a spectrogram per node and for the mix.
Every analysis keeps a numbered record, even a failed one, holding the song as analyzed and that
run's report and images. Report fields whose meaning is not obvious from their names:

| Field | Meaning |
|---|---|
| `run` | This analysis's record number |
| `song_sha256` | Digest of the loaded song as the record writer serializes it; it differs from a hash of the source file |
| `node_key` | Digest of the node's render inputs: everything but the node's `gain_db`, including the song's length, the sound or recipe it resolved to, and the keys of the stems fed into it |
| `sound_key` | Digest of the sound or recipe itself; it changes when a preset, saved sound file, or effect parameters change |
| `gain_db` | The node's terminal mix fader, in decibels |
| `peak_dbfs` | Highest sample level, in dB relative to full scale |
| `integrated_lufs` | Loudness over the whole song |
| `bands_db` | Level in each frequency band, relative and uncalibrated |
| `bar_dbfs` | RMS level of each 4-beat span, up to the song's last beat |
| `detail` | Why a node has no measurements |
| `sections` | Extra analysis from configured report sections, by name; each has a `version` and either `data` with its own `defects`, or an `error` |

Node measurements are of that node's own output: the sound as played, or the effect's output
before its `gain_db`; mix measurements include leaf faders.

The bands are `sub` 20–60 Hz, `bass` 60–250 Hz, `low_mid` 250–500 Hz, `mid` 500–2000 Hz,
`high_mid` 2000–6000 Hz, and `high` 6000–20000 Hz.

Track statuses from `render-song`: `rendered`, `silent`, `unknown_sound`, `load_failed`, `crashed`,
`timed_out`, `late_start`, `unsupported_pitch`, which means the track has a fractional pitch and
its instrument plays only whole pitches, and `upstream_failed`, which means the node was skipped
because an upstream node did not render. A track whose sound begins more than 0.1 s after its first note is
rendered again, up to three times in all, and `render-song` and `export-song` print a line for each
retry. A track that starts at the same moment in all three renders is kept, as starting late by
design; `late_start` means every attempt started late, not all by the same amount, so the track left
no stem. Node statuses from `analyze-song`: `analyzed`, `missing_stem`, and `stale_stem`, which
means the stem no longer matches the song or its sound; `detail` says why.

Defects fail the analysis: `silent`, for a node or the mix; `clipping`, for mix samples beyond
full scale; and `headroom`, for a mix peak above -1 dBFS. A section's defects and errors also
fail the analysis.

## Verified delivery

To deliver a song, run `uv run export-song <song> --destination <new-directory>`. Export
renders, analyzes, and publishes matching source, audio, report, images, and a hash manifest only
on success. Choose a destination that does not exist yet, neither lies inside nor contains the
render store (`renders/` by default) or `.poser-init`, and contains neither `POSER.md` nor the
song's inputs (the song, its includes, `poser.toml`, and `uv.lock`). Repeat `--include
<source-relative-file>` for declared local source dependencies; `uv run export-song --help` lists
the other options. Exit code 0 means published; 3 means another run holds the song's lock, so wait
and rerun; any other code means nothing was published, and the message says why. The manifest
records `listening: "not_auditioned"`; report any actual audition separately. See the
[export contract](https://github.com/isnbh0/poser/blob/main/docs/setup.md#export-contract)
for manifest details, portability, and reproducibility limits.

## Routing

With the script format enabled, `build()` may return a `Graph` of named `Node`s joined by `Wire`s.
An effect node names a recipe from `poser.toml`; a track node is a source and takes no incoming
wires. A node may feed many nodes. A wire carries `audio`, scaled by its `gain_db`, or `control`.
A node accepts at most one control wire, and only if its recipe documents a control input (a
sidechain duck, for example).

The mix sums only leaf nodes (nodes without outgoing wires), applying each leaf's `gain_db`.
A node's `gain_db` is a terminal mix fader: it does not scale outgoing audio. A bus feeding
another node therefore needs wire `gain_db` to change that send's level, or an upstream gain
processing effect to change the signal itself. Wire gain changes render inputs; node fader
changes reuse stems. The mix itself is not a wire target.

Routing a track into an effect removes its dry sound from the mix unless a dry branch is
authored. This example assumes recipes named `reverb` and `sum` in `poser.toml`, with `sum` a
passthrough:

```python
from poser import Graph, Node, Wire, notes


def build() -> Graph:
    return Graph(
        bpm=100,
        nodes=(
            Node("lead", "track", sound="{first_instrument}", notes=notes([60], 0, 1, 100)),
            Node("wet", "effect", effect="reverb", gain_db=-6),
            Node("dry", "effect", effect="sum", gain_db=-3),
        ),
        wires=(
            Wire("lead", "wet", "audio", gain_db=-12),
            Wire("lead", "dry", "audio"),
        ),
    )
```

Only `wet` and `dry` enter the mix. The wet recipe receives the lead at -12 dB; its output
enters the mix at -6 dB. The dry branch receives the unscaled lead and enters at -3 dB.

## Feedback

Opt into loops with `build()` returning `Graph(mode="feedback")`. Every loop must pass through a
`delay` node: removing the wires leaving `delay` nodes must make the graph acyclic. A delay inside
a loop needs `delay_samples` of at least one render block (512 samples); delays outside loops may
be any length. Rendering uses previous-block feedback; timing is the declared delay, without
latency compensation.

## Piano roll

`uv run piano-roll <song> --output artifacts/piano-roll.png` writes a static PNG of authored
notes, without loading plugins or rendering audio. Each source track has a panel in source order,
with beats horizontally and MIDI pitch vertically. Rectangle widths show authored durations, and
opacity rises with velocity. Tempo and track names are shown. Any configured song format works,
including direct graphs.
`--config path/to/poser.toml` selects a workspace explicitly; the song may be outside it.

Choose a `.png` output path; missing parents are created. Existing destinations are refused
unless `--replace` is supplied. Success prints the destination and returns 0; errors return 2.

## Commands

- `uv run render-song` and `uv run analyze-song`: render and analyze a song, as above.
- `uv run piano-roll`: create an authored-note PNG with required `--output`, as above.
- `uv run list-sounds`: every sound a song can name, with a description.
- `uv run check-instruments`: checks that each instrument loads and plays notes. Its
  repeatability and clipping results are informational.
- `uv run update-guide`: rewrite `POSER.md` after `poser.toml` changes; `--check` compares it.
- `uv run save-sound`: do not run it. It opens an instrument's window and waits for a person to
  close it, so it is for owners only, and only on instruments that support capture.

Put studio preferences in `AGENTS.md` and instrument instructions in each instrument's `guide`
in `poser.toml`. `[guidance] advice = false` in `poser.toml` drops the packaged advice at the next
`uv run update-guide`; the default is true.
