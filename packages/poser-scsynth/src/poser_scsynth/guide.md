## scsynth instruments

A scsynth instrument plays sounds through SuperCollider's server (`scsynth`), rendered
non-realtime: one `scsynth -N` subprocess per render. A track names one of its sounds as
`<instrument>:<source>/<path>`, exactly as `uv run list-sounds` prints it, such as
`sc:demo/voice`.

- A sound is a pair of files in a source folder: `<name>.scsyndef`, the synth definition, and
  `<name>.json`, its sidecar, whose `cc` table maps controller numbers to the synthdef's
  parameter names (`{"cc": {"1": "brightness"}}`). A controller whose number has no entry does
  nothing; a track's controller changes reach the most recent note still sounding.
- A bare instrument name, such as `sc`, is the default sound, when the adapter ships one.
- `sources` in `poser.toml` names sound folders; `saved` is the source of sounds kept by
  `save-sound`. Case matters in `<source>/<path>`.
- The `scsynth` option names the engine binary. Without SuperCollider installed, sounds still
  list and resolve, and renders fail with `load_failed`.

scsynth hosts no plugins: a scsynth sound is code (a synth definition), not a VST/AU preset.
VST and AU instruments stay on Pedalboard adapters; the two render as independent track stems
that mix as one song.
