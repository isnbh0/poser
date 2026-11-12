## scsynth instruments

A scsynth instrument plays sounds through SuperCollider's server (`scsynth`), rendered
non-realtime. A track names one of its sounds as `<instrument>:<source>/<path>`, exactly as
`uv run list-sounds` prints it, such as `sc:demo/voice`.

- A bare instrument name, such as `sc`, is the adapter's built-in sound: an additive bank of six
  sine partials with a short attack and a gate release. `sc:builtin/additive-voice` names it
  explicitly.
- A sound in a source folder is a pair of files: `<name>.scsyndef`, containing one SCgf v2
  definition, and `<name>.json`, its sidecar, whose `cc` table maps controller numbers 0–119
  to the synthdef's parameter names (`{"cc": {"1": "brightness"}}`). Unmapped controllers do
  nothing. Mapped controls affect all sounding voices, including release tails, and their
  latest values initialize future notes, including controls at the note's start.
- `sources` in `poser.toml` names sound folders; `saved` reads artifact pairs directly under
  `<sounds_dir>/<instrument>/`. This adapter provides no effect recipes and no `save-sound`
  capture. Case matters in `<source>/<path>`; `builtin` and `saved` are reserved source names.
- The `scsynth` option names the engine binary, whose path varies by install. Without it, sounds
  still list and resolve, and renders fail with `load_failed`.

```toml
[instruments.sc]
adapter = "poser_scsynth.instrument:create"
scsynth = "/Applications/SuperCollider.app/Contents/Resources/scsynth"
```

scsynth and Pedalboard tracks can share a song; each renders its own stem.
