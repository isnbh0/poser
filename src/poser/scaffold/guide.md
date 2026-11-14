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
instrument plays either any pitch or only whole pitches, and a fractional pitch on a whole-pitch
instrument fails as `unsupported_pitch`; poser's own adapters say which in their sections of this
guide. Mark a track `pitched: false` when its pitches pick drums or other mapped sounds rather than
notes; its pitches must then be whole, and it is left out of readings but sounds the same.
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
| `beat` | note, controller change, mark | number | 0 or later |
| `pitch` | note | number | 0 to 127 |
| `length` | note | beats | above 0 |
| `velocity` | note | integer | 1 to 127 |
| `control` | controller change | integer | 0 to 119 |
| `value` | controller change | integer | 0 to 127 |
| `shape` | controller change, optional | `step` or `linear` | |
| `marks` | song, optional | layer names to lists of marks | lowercase letters, digits, `_`, `-`, starting with a letter or digit |
| `end` | mark | number | after `beat` |
| `text` | mark | text | one line, no leading or trailing spaces |

A `step` holds until that controller's next change; `linear` ramps to it and needs a later one.

Marks are the author's labels over spans of beats, in layers named for what they describe, such as
`chords`, `key`, or `parts`. Marks in a layer are in order and do not overlap, and a gap means
nothing was said there. Marks may extend past the last note; anything reading them stops at the
song's end. Marks never change audio or readings. In Python, write
`marks=Marks({"chords": [Mark(0, 4, "Am"), Mark(4, 8, "F")]})`. A configured vocabulary reads the
layer with its name; see Framework readings.

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
- `transpose` — notes moved by an interval in semitones, any number; works on any pitch.
- `transpose_degrees` — notes moved along a scale; refuses off-scale notes unless
  `off_scale="below"`.

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
run's report, mix, and images. `latest` in the song's render directory links to the newest record
in which every node analyzed.

The report's `status` is `complete` when every node analyzed, and only then does it have a `mix`
and images; otherwise it is `incomplete`, with no `mix` key. Every node has `name`, `kind`,
`upstream`, `gain_db`, and `status`, and only the fields its status gives it:

- `analyzed`: `node_key`, `sound_key`, `measurements`, `defects`, and `spectrogram`.
- `measured`, a readable node in an incomplete analysis, which draws no images: `node_key`,
  `sound_key`, `measurements`, and `defects`.
- `not_rendered`, a node with no stem for its current inputs; render again. `unreadable_stem`, a
  stem whose audio cannot be read; run `render-song --force`. Both have `node_key`, `sound_key`,
  and `detail`.
- A plan failure, with the meaning `render-song` gives it: `unknown_sound`, `load_failed`,
  `unsupported_pitch`, or `upstream_failed`. It has `sound_key` and `detail`, and no `node_key`.

`measurements` holds `peak_dbfs`, `integrated_lufs`, `clipped_samples`, `bands_db`, and
`bar_dbfs`, for a node and for the mix. Fields whose meaning is not obvious from their names:

| Field | Meaning |
|---|---|
| `run` | This analysis's record number |
| `song_sha256` | Digest of the loaded song as the record writer serializes it; it differs from a hash of the source file |
| `node_key` | Digest of the node's render input: for a track, its sound and the samples and events it plays; for an effect or delay, its recipe or delay and the stems and gains fed into it; for a cycle group member, the whole group, including its members' names. Renaming a node outside a cycle group or changing its `gain_db` leaves it unchanged, and nodes in one cycle group share it |
| `sound_key` | Digest of the sound or recipe itself; it changes when a preset, saved sound file, or effect parameters change. Null for a delay or a sound that does not resolve |
| `gain_db` | The node's terminal mix fader, in decibels |
| `peak_dbfs` | Highest sample level, in dB relative to full scale |
| `integrated_lufs` | Loudness over the whole song; null when the audio is too short to measure |
| `bands_db` | Level in each frequency band, relative and uncalibrated |
| `bar_dbfs` | RMS level of each 4-beat span, up to the song's last beat |
| `detail` | Why a node has no measurements |
| `spectrogram` | The node's or the mix's spectrogram, as a path relative to the record directory |
| `path` | The mix's audio file, as a path relative to the record directory |

Node measurements are of that node's own output: the sound as played, or the effect's output
before its `gain_db`; mix measurements include leaf faders.

The bands are `sub` 20–60 Hz, `bass` 60–250 Hz, `low_mid` 250–500 Hz, `mid` 500–2000 Hz,
`high_mid` 2000–6000 Hz, and `high` 6000–20000 Hz.

Track statuses from `render-song`: `rendered`, `silent`, `unknown_sound`, `load_failed`, `crashed`,
`timed_out`, `late_start`, `unsupported_pitch`, which means the track has a fractional pitch and its
instrument plays only whole pitches, and `upstream_failed`, which means the node was
skipped because an upstream node did not render. A track whose sound begins more than 0.1 s after
its first note is rendered again, up to three times in all, and `render-song` and `export-song`
print a line for each retry. A track that starts at the same moment in all three renders is kept, as
starting late by design; `late_start` means every attempt started late, not all by the same amount,
so the track left no stem.

Defects fail the analysis: `silent`, for a node or the mix; `clipping`, for mix samples beyond
full scale; and `headroom`, for a mix peak above -1 dBFS.

## Verified delivery

To deliver a song, run `uv run export-song <song> --destination <new-directory>`. Export
renders, analyzes, and publishes matching source, audio, report, images, and a hash manifest only
on success. Choose a destination that does not exist yet, neither lies inside nor contains the
render store (`renders/` by default), and contains neither `POSER.md` nor the song's inputs
(the song, its includes, `poser.toml`, and `uv.lock`). Repeat `--include <source-relative-file>`
for declared local source dependencies; `uv run export-song --help` lists the other options. Exit code 0 means published; 3 means another run holds the song's lock, so wait
and rerun; any other code means nothing was published, and the message says why. The manifest
records `listening: "not_auditioned"`; report any actual audition separately. See the
[export contract](https://github.com/isnbh0/poser/blob/main/docs/setup.md#export-contract)
for manifest details, portability, and reproducibility limits.

## Routing

With the script format enabled, `build()` may return a `Graph` of named nodes joined by `Wire`s.
A `TrackNode` plays a sound and is a source, taking no incoming wires. An `EffectNode` names a
recipe from `poser.toml`. A `DelayNode` delays its summed inputs by `delay_samples`, with no dry
signal. A node may feed many nodes. A wire carries `audio`, scaled by its `gain_db`, or `control`.
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
from poser import DelayNode, EffectNode, Graph, TrackNode, Wire, notes


def build() -> Graph:
    return Graph(
        bpm=100,
        nodes=(
            TrackNode("lead", sound="{first_instrument}", notes=notes([60], 0, 1, 100)),
            DelayNode("pre", delay_samples=2400),
            EffectNode("wet", effect="reverb", gain_db=-6),
            EffectNode("dry", effect="sum", gain_db=-3),
        ),
        wires=(
            Wire("lead", "pre", "audio", gain_db=-12),
            Wire("pre", "wet", "audio"),
            Wire("lead", "dry", "audio"),
        ),
    )
```

Only `wet` and `dry` enter the mix. The wet recipe receives the lead at -12 dB and 2400 samples
(50 ms) late; its output enters the mix at -6 dB. The dry branch receives the unscaled lead and enters at -3 dB.

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

## Framework readings

Frameworks are lenses over the score, the authored notes: each reads the song in spans of beats
covering it from 0 to its last beat, and gives every span one of three outcomes:
- known: one reading, such as one key.
- ambiguous: two or more candidates, most likely first.
- unknowable: a reason the framework cannot tell. It is a result about the music, not a pass.

Tracks marked `pitched: false` are never read. "Frameworks here" lists this workspace's
frameworks, and each framework's own section says what it reads.

`uv run read-song <song>` first prints each of the song's mark layers, up to the song's end, with
each mark's text as written. A layer named like a configured vocabulary shows that vocabulary's
reading of its labels, and other layers show as text; "Frameworks here" lists this workspace's
vocabularies. Frameworks never see marks, so the authored and the inferred appear side by side.

It then prints each framework's reading, without loading plugins or touching renders. Repeat
`--track <name>` to read only those pitched tracks (the default is every pitched track), and
`--framework <name>` to use only those configured frameworks (the default is all); neither
filters marks. `--json` prints the readings as JSON, with a `marks` object when the song has
marks, and `--config` selects a workspace explicitly. Exit codes: 0 when every framework returned
a reading, whatever its outcomes; 1 when a framework failed, printed as `<name>: error: <detail>`,
or a vocabulary failed, printed as `<layer> marks: error: <detail>`; 2 when the song or
`poser.toml` cannot be read, a `--track` or `--framework` name is unknown or the track is
unpitched, or a vocabulary rejects a label, naming the mark, such as `marks.chords[0]`.

`[adapters] frameworks` in `poser.toml` takes a preset name or a list of frameworks. The
`western` preset, Western tonal harmony, is the default; `neutral` turns readings off. A preset
is poser's current recommendation and may gain frameworks as poser evolves; a list, such as
`frameworks = ["tonal"]`, pins them. `[adapters] vocabularies` takes a preset name or a list of
vocabularies the same way: the `western` preset, the default, reads `chords` and `key` layers;
`neutral` reads none, so every layer is text; and a list, such as `vocabularies = ["chords"]`,
pins them.

## Commands

- `uv run render-song` and `uv run analyze-song`: render and analyze a song, as above.
- `uv run piano-roll`: create an authored-note PNG with required `--output`, as above.
- `uv run read-song`: show a song's marks and read its score through the configured frameworks,
  as above.
- `uv run list-sounds`: every sound a song can name, with a description.
- `uv run check-instruments`: checks that each instrument loads and plays notes. An instrument
  that plays is `pass`, `silent`, or `missing_notes`; one that fails is `load_failed`, `crashed`,
  `timed_out`, or `non_finite`. Its repeatability and clipping results are informational.
- `uv run update-guide`: rewrite `POSER.md` after `poser.toml` changes; `--check` compares it.
  To upgrade poser, follow https://github.com/isnbh0/poser/blob/main/docs/upgrading.md.
- `uv run save-sound`: do not run it. It opens an instrument's window and waits for a person to
  close it, so it is for owners only, and only on instruments that support capture.

Put studio preferences in `AGENTS.md` and instrument instructions in each instrument's `guide`
in `poser.toml`. `[guidance] advice = false` in `poser.toml` drops the packaged advice at the next
`uv run update-guide`; the default is true.
