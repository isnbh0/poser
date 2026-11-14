# Upgrade a studio

A studio that depends on poser from Git runs the commit its `uv.lock` pins; one with an editable
path source runs whatever that checkout holds, and the lock step changes nothing. To move a studio
to the latest poser, run these from the studio:

```bash
uv lock --upgrade-package poser &&
uv run update-guide &&
uv run update-guide --check &&
uv run check-instruments
```

Then load every song, which needs no plugins; each song that fails prints its error:

```bash
find songs \( -name '*.yaml' -o -name '*.yml' -o -name '*.py' \) -exec uv run read-song {} \; \
  > /dev/null
```

`update-guide` rewrites `POSER.md` for the new poser; until then, `update-guide --check` reports
`POSER.md is out of date`. `AGENTS.md`, `poser.toml`, and songs are yours and are never rewritten.
If a command fails after the upgrade, find its message below, newest change first, and apply the
fix. A change listed here applies to every studio that last ran a poser from before it.

## Song meter

- **New:** a song may carry an optional `meter` key: spans that each give the length of a bar in
  beats, such as `{beat: 0, bar: 4}`, in Python `meter=Meter([MeterSpan(0, 4)])`. `read-song`
  prints the meter and names the bars each range of beats covers, as in
  `beats 0-16 (bars 1-4)`, and `--json` carries the metered stretches as `meter`.
  `Meter.beat_of` gives the beat a bar starts on. A remainder shorter than 0.25 beats after the
  last bar line joins the bar before it, so a song whose last notes ring just past that line
  declares a plain `{beat: 0, bar: 4}` and gains no extra bar. Meter never changes audio, and a
  song without one loads, renders, and prints as before.

- **Symptom:** a script reading `report.json` finds no `bar_dbfs` under `measurements`.
  **Cause:** levels are `span_dbfs`, one per entry of the report's `spans`, which gives each
  level's beats and, where the song declares a meter, its bar number.
  **Fix:** read `span_dbfs`. For a song without a meter the spans are the old 4-beat bars, except
  that the last one stops at the song's last beat and no longer includes the tail, and a last
  span that would be shorter than 0.25 beats joins the one before it. Each span's bounds are
  computed from its own beats, so a bound can move by one sample and a value can differ slightly
  from the old `bar_dbfs` value. No command reads the studio's own prose, so also search it for
  the old name, as in `grep -rn bar_dbfs .`, and for text that calls the levels bars: song
  docstrings, notes, and instrument notes in `poser.toml`.

- **New:** `tonal` reads windows of whole declared bars: where a song declares a meter, a window
  is `window_bars` bars, four unless set beside `window_beats` under `[adapters] frameworks`,
  and a change of meter starts a new window. Where a song declares none, a window is still
  `window_beats` beats. A last window shorter than half a window is read together with the one
  before it, so a song without a meter reads as before unless its length leaves such a
  remainder.

## Mark checks and key modes

- **New:** `read-song` compares each `key` and `chords` mark with the notes inside its span, and
  prints `warning: <detail>` under a mark the notes contradict and `unchecked: <reason>` under
  one it could not judge. `--json` carries the result as `check` on each mark. Neither line
  changes the exit code, and `POSER.md` states each rule. A mark may set `deliberate: true`, in
  Python `Mark(0, 4, "C", deliberate=True)`, to say its label is intended; its warning then
  prints as `deliberate: <detail>`. A script that passes a graph to
  `poser.core.interpreting.interpret_marks` passes a `ScoreRequest` instead, such as
  `ScoreRequest.default(graph)`.

- **New:** a key names any of the seven modes, as in `D dorian`, so search the songs for marks
  and comments where `minor` or `major` stood in for a mode, as in
  `grep -rnE "minor|major" songs`.

## No initialization journal

- **Symptom:** a studio has a `.poser-init/` directory and a `.poser-init/` line in `.gitignore`.
  **Cause:** `init-workspace` used to keep a recovery journal there; it now creates files without
  overwriting and removes what it wrote on failure, so it keeps no journal.
  **Fix:** delete the `.poser-init/` directory and its `.gitignore` line, or leave them; nothing
  reads them.

## Report statuses and shape

- **Symptom:** a script reading `report.json` finds no `peak_dbfs` on a node or the mix, no
  `mix` key, or no `stale_stem` or `missing_stem` status.
  **Cause:** a report's `status` is `complete` or `incomplete`, and only a complete report has a
  `mix`. Each node carries only the fields its `status` gives it, with metrics nested under
  `measurements`. Analysis reports a node the render plan cannot key with the status
  `render-song` gives it.
  **Fix:** read metrics under `measurements`, and branch on the report's `status` and each node's
  `status`. Treat `not_rendered` as the old `missing_stem` and `stale_stem`; `unreadable_stem` is
  fixed by `render-song --force`; `unknown_sound`, `load_failed`, `unsupported_pitch`, and
  `upstream_failed` mean what `render-song` reports for them. A readable node of an incomplete
  analysis is `measured`, with no `spectrogram`.

## Render store layout

- **Symptom:** `latest/mix.wav` is missing, or a report's `path` and `spectrogram` values are not
  absolute.
  **Cause:** each analysis is now a numbered record in `history/`, laid out like an export
  bundle, and `latest` is a link to the newest complete record.
  **Fix:** the mix is `latest/audio/mix.wav`, and report paths are relative to the record. Old
  `stems/` and `stamps/` directories in a song's render directory may be deleted; poser no
  longer reads them, and every track renders again once. The first analysis replaces an old
  `latest/` directory with the link.

- **Symptom:** a script finds no stems under `stems/`, or no spectrograms under node names.
  **Cause:** a rendered unit is stored once under `objects/<node_key>/`, as `meta.json` and
  numbered stems, and a record numbers its spectrograms in `images/`, as in `node-0.png`.
  **Fix:** read stems through a record's `audio/` directory and `report.json`, whose paths are
  relative to the record. Records in `history/` written before the upgrade keep their old
  layout.

## Fixed adapters and no report sections

- **Symptom:** a command fails with `adapters.<key>: unknown key` for `record_writer`,
  `audio_files`, `render_store`, `mixer`, `measurer`, `visualizer`, or `sections`.
  **Cause:** these parts each have one implementation and are no longer configurable. Custom
  implementations of the record writer, audio files, render store, mixer, measurer, visualizer,
  and report-section ports are no longer supported.
  **Fix:** delete the key; poser uses its built-in adapter.

- **Symptom:** `report.json` no longer has `sections` keys on nodes or the mix.
  **Cause:** report sections are removed, and section defects no longer exist.
  **Fix:** read the remaining fields.

## Render keys digest render inputs

- **Symptom:** every track renders again on the first `render-song` after upgrading.
  **Cause:** a node's `node_key` is now the digest of exactly what its render receives, so every
  stored key stops matching once. Nodes in one cycle group share one key, and a report's
  `sound_key` is null for a delay.
  **Fix:** nothing; the renders after the first are cached as before.

## Typed graph nodes

- **Symptom:** a script song fails with `cannot import name 'Node' from 'poser'`.
  **Cause:** each kind of graph node is now its own type, so a node cannot carry another kind's
  payload.
  **Fix:** replace `Node("x", "track", ...)` with `TrackNode("x", ...)`, and likewise
  `Node("x", "effect", ...)` with `EffectNode` and `Node("x", "delay", ...)` with `DelayNode`,
  dropping the kind argument. For annotations, use `GraphNode`.

- **Symptom:** a custom effect adapter's `render_group` fails with `AttributeError` on
  `member.recipe` or `member.delay_samples`.
  **Cause:** a cycle group's members are now `EffectMember` (with `recipe`) or `DelayMember` (with
  `delay_samples`); `GroupMember` is gone.
  **Fix:** match on the member's type and read only that type's field.

## Frameworks read graphs

- **Symptom:** a custom framework fails with `AttributeError: 'Graph' object has no attribute
  'tracks'`, or its `Song` branch never runs.
  **Cause:** a framework's `ScoreRequest.song` is now always a `Graph`, whether the song is a
  YAML file or a script. A YAML song's tracks are its graph's track nodes.
  **Fix:** read track nodes from `song.track_nodes`, and stop branching on `Song`.

## Song marks

- **Symptom:** a YAML song fails to load with `cannot read <song>: duplicate key '<key>' at line
  <n>`.
  **Cause:** the YAML loader used to keep the last of two equal keys in a mapping and drop the
  other silently; it now refuses any repeated key.
  **Fix:** remove or rename the duplicate key at that line.

- **New:** a song may carry an optional `marks` key: layers of the author's labels over spans of
  beats, such as `chords` or `parts`. Marks never change audio or readings.

- **New:** a song may carry a `chords` layer, such as `{beat: 0, end: 4, text: Am7}`, and
  `read-song` shows the parsed chords above the framework readings; other layers show as text.
  `[adapters] vocabularies` selects what reads layers, by preset or list, as `[adapters]
  frameworks` does; the default `western` preset reads `chords`. A studio that sets
  `frameworks = "neutral"` may also want `vocabularies = "neutral"`.

- **New:** a `key` layer, such as `{beat: 0, end: 64, text: A minor}`, states the song's key, and
  `read-song` shows it above `tonal`'s inferred key. Studios on the `western` preset read it
  automatically; a studio with a pinned `vocabularies` list adds `"key"` to it.

## Framework-neutral pitch and readings

- **Symptom:** `update-guide`, `check-instruments`, and other commands that build instruments
  fail with `… made a <Class>, which is not an Instrument: it lacks pitches; …`.
  **Cause:** an instrument adapter outside poser predates the `pitches` property of the
  `Instrument` port. poser's own adapters already declare it.
  **Fix:** add the property to the adapter class:

  ```python
  from poser.core.performance import PitchSupport


  class MyInstrument:
      @property
      def pitches(self) -> PitchSupport:
          return PitchSupport.SEMITONES  # CONTINUOUS if it plays any pitch, such as 60.5
  ```

  An adapter that wraps another instrument can return the wrapped instrument's `pitches`.
  Declaring the property changes no audio, so `adapter_version` stays the same.

- **Symptom:** code outside poser that reads `Note.pitch` fails on a value such as `60.5`.
  **Cause:** a pitch is any number from 0 to 127; a whole pitch is still an `int`.
  **Fix:** accept floats in that code. Songs whose pitches are all whole are unaffected.

- **New:** `uv run read-song <song>` reads a song's notes through frameworks, by default the
  `western` preset's tonal key reading. To read nothing, set `frameworks = "neutral"` in an
  `[adapters]` table in `poser.toml`, then run `uv run update-guide`. A track can set
  `pitched: false` to stay out of readings; its pitches must then be whole.

Songs whose pitches are all whole render the same audio.
