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

## Framework-neutral pitch and readings

- **Symptom:** a command that builds instruments fails with
  `… made a <Class>, which is not an Instrument: it lacks pitches`.
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

- **Symptom:** code outside poser that reads `Note.pitch` fails on a value such as `60.5`.
  **Cause:** a pitch is any number from 0 to 127; a whole pitch is still an `int`.
  **Fix:** accept floats in that code. Songs whose pitches are all whole are unaffected.

- **New:** `uv run read-song <song>` reads a song's notes through frameworks, by default the
  `western` preset's tonal key reading. To read nothing, set `frameworks = "neutral"` in an
  `[adapters]` table in `poser.toml`, then run `uv run update-guide`. A track can set
  `pitched: false` to stay out of readings; its pitches must then be whole.

Existing songs render the same audio, and stems rendered before the upgrade stay current.
