# Upgrade a studio

A studio's `uv.lock` pins the poser commit it runs. To move a studio to the latest poser, run
these from the studio:

```bash
uv lock --upgrade-package poser &&
uv run update-guide &&
uv run update-guide --check &&
uv run check-instruments
```

`update-guide` rewrites `POSER.md` for the new poser; until then, `update-guide --check` reports
`POSER.md is out of date`. `AGENTS.md`, `poser.toml`, and songs are yours and are never rewritten.
If a command fails after the upgrade, find its message below, newest change first, and apply the
fix. A change listed here applies to every studio that pinned a poser commit from before it.

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
