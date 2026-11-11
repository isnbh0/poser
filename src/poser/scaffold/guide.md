# Composing with poser

## Operational contract

poser renders songs through configured instruments and measures the result. Creator intent and
owner requirements in `AGENTS.md` remain authoritative with advice on or off. Preserve owner work.
If composition was requested, continue after successful setup; ask only for missing consequential
intent or permissions, not authorization to compose a second time.
Playback requires permission. Starting a player is not listening; disclose "not auditioned"
when no actual listening occurred. Analysis finds defects; it does not say a song is good.
Never deliver an earlier mix as current after a failed attempt.
When the script format is enabled, song scripts import `poser` directly, without a PEP 723
header, in this project's environment.

For a poser-verified matching delivery, use
`uv run export-song <song> --destination <new-directory>`; render/analyze remain available for
experiments. Export loads once, renders, analyzes, and publishes matching source, audio, report,
images, and a hash manifest only on success. The destination must be unused and outside mutable
stores. Repeat `--include <source-relative-file>` for declared local source dependencies.
Export accepts `--config`, `--sounds-dir`, `--output-dir`, `--force`, and `--json`.
Exit codes are 0 for publication, 1 for execution/analysis, changed inputs or I/O failure,
2 for invalid arguments/configuration, and 3 for song contention.
Hashes establish byte correspondence, not musical quality or complete Python reproducibility:
external state, undeclared imports, and plugin nondeterminism remain outside that guarantee.
The manifest records `listening: "not_auditioned"`; actual audition is separate evidence.
Opaque report section payloads are preserved but their embedded paths are not verified portable.
A historical report alone does not prove matching audio when its mix path is mutable.

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

A `step` holds until that controller's next change; `linear` ramps to it and needs a later one.

Track `effects` form a recipe chain: the first hears the track, each next hears its predecessor.
Nodes are named `<track>.<effect>`, with repeats numbered (`bass.drive.2`). Track `gain` fades
the chain's end.

With the script format enabled, this short song shows the form rather than a finished piece:

```python
from poser import Song, Track, notes


def build() -> Song:
    track = Track(name="example", sound="{first_instrument}", notes=notes([60], 0, 1, 100))
    return Song(bpm=100, tracks=(track,))
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

The mix sums leaf nodes at their `gain_db`. A bus is an effect fed by sends scaled by wire
`gain_db`, fading its group at the bus's `gain_db`.
Routing a track's output into an effect removes the track's dry sound from the mix unless a dry
branch is authored. For example, wire the track to both a wet effect node and a passthrough
`sum` effect node (configured in `poser.toml` with `builtin = "sum"`). Leave both nodes without
outgoing wires; the mix includes both their outputs. The mix itself is not a wire target.

## Feedback

Opt into loops with `build()` returning `Graph(mode="feedback")`. Removing wires leaving `delay`
nodes must make the graph acyclic. Each loop's `delay_samples` must be at least one render block
(512 samples); smaller in-loop delays are refused, while off-loop delays allow any length.
Rendering uses previous-block feedback; timing is the declared delay, without latency compensation.

## Sounds

A `sound` is `<instrument>` (its default) or `<instrument>:<name>`, as `uv run list-sounds` prints.
Each instrument has its own names. See "Instruments here" for this workspace.
`uv run list-sounds --json` returns machine-readable listings. Repeat `--instrument <name>`
for exact configured instrument names; `--match <substring>` filters IDs and descriptions
case-insensitively, preserving listing order. Unselected instruments are not scanned.
A listing is not an audition or evidence of musical suitability.
`list-sounds` exits 0 for success (including empty matches), 1 for selected-source problems even
when entries are filtered out, and 2 for unreadable config or unknown instruments.

## Rendering

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

`uv run analyze-song <song> --json` mixes the stems with each node's `gain` and prints
the report, which names the files it wrote: the mix, and a spectrogram per node and for the mix.
Every analysis keeps a numbered record, even a failed one, holding the song as analyzed and that
run's report and images. Report fields whose meaning is not obvious from their names:

| Field | Meaning |
|---|---|
| `run` | This analysis's record number |
| `song_sha256` | Digest of the record writer's serialized loaded song, not a raw editable source-file hash |
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
before `gain`; mix measurements are after it. Band levels are relative and uncalibrated, not absolute targets.

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

## Piano roll

`uv run piano-roll <song> --output artifacts/piano-roll.png` writes a static PNG of authored
notes, without plugins or an audio render. Each source track has a panel in source order, with
beats horizontally and MIDI pitch vertically. Rectangle widths show authored durations; opacity
is `0.25 + 0.75 * velocity / 127`. Tempo and track names are shown. This is not rendered audio:
plugin response and sounding duration are not represented, and no chord, meter, or drum
interpretation is inferred. Empty graphs show `No authored notes`.

Configured song formats are supported, including direct graphs. Python songs execute their
`build()` code: load only trusted scripts. Use `--config path/to/poser.toml` to select a workspace
explicitly; the song itself may be outside it. No render-store artifacts or analysis are required.

The required output is the exact directory entry relative to the current directory, with a
case-insensitive `.png` suffix. Missing parents are created. Existing destinations are refused
unless `--replace` is supplied; replacement preserves the old artifact until the complete new PNG
is atomically published. Input aliases, symlinks (including dangling ones), and nonregular files
are always refused. These are checked rules, not a security boundary against hostile filesystem
mutation; parent-directory mutation and power-loss durability are outside the guarantee.
Concurrent replacements use last-successful-publication-wins behavior.

Success prints the destination and returns 0; configuration, song, drawing, and pre-publication
I/O errors return 2. Temporary cleanup is best-effort: a cleanup warning after publication does
not mean the image failed, and names the residual staging file. Created parents may remain after
failure. The command never adds the image to a render cache.

## Commands

- `uv run render-song` and `uv run analyze-song`: render and analyze a song, as above.
- `uv run piano-roll`: create an authored-note PNG with required `--output`, as above.
- `uv run list-sounds`: every sound a song can name, with a description.
- `uv run check-instruments`: checks load and note output. Repeatability is informational:
  exact sample-value equality with no tolerance; `pass` with `no` succeeds. Clipping is also informational.
- `uv run update-guide`: rewrite `POSER.md` after `poser.toml` changes; `--check` compares it.
- `uv run save-sound`: for owners, not agents; only for instruments supporting capture.
- `uv run init-workspace --setup surge`: makes a new workspace; not needed here.

Edit `AGENTS.md` for studio preferences and instrument `guide` notes in `poser.toml` for
instrument instructions. `POSER.md` is generated from these instrument notes and poser and adapter
guides; adapter maintainers own their backend-specific guides.

Advice selection is persistent: `[guidance] advice = false` in `poser.toml` excludes packaged
creative/workflow advice after `uv run update-guide`; missing settings default to true.
`uv run init-workspace --setup surge --advice off` selects it for a new studio.
Instrument `guide` notes and legacy adapter `guide` properties are required reference, not
semantically sanitized. Adapter authors put optional coaching in a package-data-only `advice`
property; inline instrument `advice` is reserved and rejected. This switch does not rewrite
owner instructions, control repository access, or erase a model's previous exposure.
