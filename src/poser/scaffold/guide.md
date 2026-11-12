# Composing with poser

## Operational contract

poser renders songs through configured instruments and measures the result.

- Creator intent and owner requirements in `AGENTS.md` override this guide and any advice.
- Preserve owner work.
- If composition was requested, continue after successful setup without asking again; ask only
  for missing consequential intent or permissions.
- Playback requires permission. Report a song as "not auditioned" unless someone actually
  listened; starting a player is not listening.
- A passing analysis means no defects were found.
- Never deliver an earlier mix as current after a failed attempt.

## Songs

Defaults are Python (`.py`, with `build()` returning a `Song`) and YAML (`.yaml` or `.yml`).
`adapters.song_formats` can enable either, both, or other formats; consult `poser.toml` and the
adapters. If present and YAML is enabled, `songs/demo.yaml` shows that form. Both use the same
song checks. Times are beats from 0; pitches are MIDI numbers, middle C is 60. A song's last note
end or controller change must fall within 3600 seconds.

When the script format is enabled, song scripts import `poser` directly, without a PEP 723
header, in this project's environment. Loading a Python song runs its `build()` code, so load
only trusted scripts. This short song shows the form:

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
| `beat` | note, controller change | number | 0 or later |
| `pitch` | note | integer | 0 to 127 |
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
Each instrument has its own names. See "Instruments here" for this workspace.
`uv run list-sounds --json` returns machine-readable listings. Repeat `--instrument <name>`
for exact configured instrument names; `--match <substring>` filters IDs and descriptions
case-insensitively, preserving listing order.
`list-sounds` exits 0 for success (including empty matches), 1 for selected-source problems even
when entries are filtered out, and 2 for unreadable config or unknown instruments.

## Rendering

Core audio is stereo (2 channels) at 48000 Hz. Rendering adds a fixed 2 seconds after the last
note end or controller change, so longer releases may be cut off.

`uv run render-song <song>` writes and prints a stem path per node, caching by inputs. Only
changed nodes render; gain-only edits render nothing. `--force` re-renders all nodes; `--json`
prints a machine-readable report. Inputs include tempo and total length, so lengthening the song
re-renders every node. Changing a track re-renders its chain.

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

Track statuses from `render-song`: `rendered`, `silent`, `unknown_sound`, `load_failed`,
`crashed`, `timed_out`, and `upstream_failed`, which means a node's upstream did not render, so
neither does this one. Node statuses from
`analyze-song`: `analyzed`, `missing_stem`, and `stale_stem`, which means the stem no longer
matches the song or its sound; `detail` says why.

Defects fail the analysis: `silent`, for a node or the mix; `clipping`, for mix samples beyond
full scale; and `headroom`, for a mix peak above -1 dBFS. A section's defects and errors also
fail the analysis.

`run` and `song_sha256` tell versions apart; `node_key` and `sound_key` show which nodes changed.

## Verified delivery

To deliver a song, run `uv run export-song <song> --destination <new-directory>`. Export loads
once, renders, analyzes, and publishes matching source, audio, report, images, and a hash
manifest only on success. The destination must be unused and outside mutable
stores. Repeat `--include <source-relative-file>` for declared local source dependencies.
Export accepts `--config`, `--sounds-dir`, `--output-dir`, `--force`, and `--json`.
Exit codes are 0 for publication; 1 when rendering, analysis, or I/O fails or inputs change
during export; 2 for invalid arguments or configuration; and 3 when another run holds the song's
lock. The manifest records
`listening: "not_auditioned"`; report any actual audition separately. See the
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

Opt into loops with `build()` returning `Graph(mode="feedback")`. Removing wires leaving `delay`
nodes must make the graph acyclic. Each loop's `delay_samples` must be at least one render block
(512 samples); smaller in-loop delays are refused, while off-loop delays allow any length.
Rendering uses previous-block feedback; timing is the declared delay, without latency compensation.

## Piano roll

`uv run piano-roll <song> --output artifacts/piano-roll.png` writes a static PNG of authored
notes, without plugins or an audio render. Each source track has a panel in source order, with
beats horizontally and MIDI pitch vertically. Rectangle widths show authored durations; opacity
is `0.25 + 0.75 * velocity / 127`. Tempo and track names are shown, and empty graphs show
`No authored notes`. Any configured song format works, including direct graphs.
`--config path/to/poser.toml` selects a workspace explicitly; the song may be outside it.

Choose a `.png` output path; missing parents are created. Existing destinations are refused
unless `--replace` is supplied. Input aliases, symlinks, and nonregular files are refused.
Success prints the destination and returns 0; configuration, song, drawing, and I/O errors
before the image is written return 2.

## Commands

- `uv run render-song` and `uv run analyze-song`: render and analyze a song, as above.
- `uv run piano-roll`: create an authored-note PNG with required `--output`, as above.
- `uv run list-sounds`: every sound a song can name, with a description.
- `uv run check-instruments`: checks that each instrument loads and plays notes. Its
  repeatability result (exact sample-value equality, no tolerance) and clipping result are
  informational and never fail the check.
- `uv run update-guide`: rewrite `POSER.md` after `poser.toml` changes; `--check` compares it.
- `uv run save-sound`: for owners, not agents; only for instruments supporting capture.

`POSER.md` is generated from poser's guide, adapter guides, instrument `guide` notes in
`poser.toml`, and packaged advice when enabled. Put studio preferences in `AGENTS.md` and
instrument instructions in those notes. `[guidance] advice = false` in `poser.toml` excludes the
packaged creative and workflow advice after `uv run update-guide`; a missing setting means true.
