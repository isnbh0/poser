## Pedalboard instruments

A Pedalboard instrument plays a VST3 or Audio Unit plugin. A track names one of its sounds as
`<instrument>:<source>/<path>`, exactly as `uv run list-sounds` prints it, such as
`synth:presets/Bass/Growl`.

- A bare instrument name, such as `synth`, is the plugin's default patch.
- `<source>` is a preset folder named in the instrument's options, and `<path>` is a file's path
  inside it, with `/` between folders and without the file's suffix. Case matters.
- By default a source holds `.fxp` preset files. Files in other formats get no sound name, and
  `list-sounds` notes them.
- Saved sounds are under `saved/`, such as `synth:saved/warm-pad`.

Song BPM schedules authored notes and controllers only; plugins get no host tempo or transport,
so a preset's tempo-synced LFOs, sequences, or delays may not follow it.

`save-sound` opens the plugin's window so the owner can shape a sound by hand and save it under
`saved/`. It waits for a person to close the window.

## Pedalboard effects

An `[effects.<name>]` table in `poser.toml` is one effect recipe, which a track's `effects` list
names. Two shapes are allowed:

- `adapter = "pedalboard"` with `path`: a VST3 or Audio Unit **effect** plugin (multi-plugin
  bundles are not supported; an instrument plugin refuses with `is not an effect`).
- `adapter = "pedalboard"` with `builtin`: a Pedalboard-native effect class, such as `Gain`,
  `Reverb`, or `Distortion`, and optional `parameters`, a table of the class's keyword arguments
  (`gain_db = -6`). Native effects need no vendor plugin.

Changing `parameters` re-renders that node and everything downstream of it.

poser adds two builtins of its own, which need no vendor plugin:

- `builtin = "sum"` passes its audio through unchanged, so a node wired to it groups its feeds
  into a bus. When the bus is a leaf, its node's `gain_db` fades the group in the mix.
- `builtin = "duck"` is a sidechain duck: the node's output is pulled down while a `control`
  wire's signal is loud. Its `parameters` are `attack_ms`, `release_ms`, and `amount_db`
  (defaults 10, 150, and 6): the follower rises to a louder control level over `attack_ms`,
  falls from a quieter one over `release_ms`, and a full-scale control level pulls the gain down
  by exactly `amount_db`. With no control wire it passes its audio through unchanged.

Only `duck` accepts a control wire; any other recipe fails at render time with `load_failed`,
naming the effect and the missing sidechain input.
