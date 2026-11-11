## Pedalboard instruments

A Pedalboard instrument plays a VST3 or Audio Unit plugin. A track names one of its sounds as
`<instrument>:<source>/<path>`, exactly as `uv run list-sounds` prints it, such as
`synth:presets/Bass/Growl`.

- A bare instrument name, such as `synth`, is the plugin's default patch.
- `<source>` is a preset folder named in the instrument's options, and `<path>` is a file's path
  inside it, with `/` between folders and without the file's suffix. Case matters.
- By default a source holds `.fxp` preset files. Files no configured format claims stay
  unnamed, and `list-sounds` reports them in a note.
- Saved sounds are under `saved/`, such as `synth:saved/warm-pad`.

`uv run list-sounds` prints every name a song can use, with a description of each. Choose sounds
from that list rather than guessing names.

Song BPM schedules authored notes and controllers. Poser does not supply song BPM as plugin
host tempo or transport; a preset's internal tempo-synced LFOs, sequences, or delays are not
guaranteed to follow it. Changing note spacing is not evidence that an internal pulse synchronized.

`save-sound` opens the plugin's window so the owner can shape a sound by hand and save it under
`saved/`. It waits for a person to close the window, so it is not for agents.

## Pedalboard effects

An `[effects.<name>]` table in `poser.toml` is one effect recipe, which a track's `effects` list
names. Two shapes are allowed:

- `adapter = "pedalboard"` with `path`: a VST3 or Audio Unit **effect** plugin (multi-plugin
  bundles are not supported here; an instrument plugin refuses with `is not an effect`).
- `adapter = "pedalboard"` with `builtin`: a Pedalboard-native effect class, such as `Gain`,
  `Reverb`, or `Distortion`, and optional `parameters`, a table of the class's keyword arguments
  (`gain_db = -6`). Native effects need no vendor plugin and render headlessly.

A recipe processes whatever its node is fed, so a chain node named `<track>.<effect>` hears its
track. Changing `parameters` re-renders that node and everything downstream of it.

Two builtins belong to poser, not to Pedalboard, and render headlessly with no vendor plugin:

- `builtin = "sum"` is a passthrough summer: it changes nothing, so a node wired to it exists to
  carry its feeds. Send tracks into it with wires at `gain_db=0`. When the bus is a leaf, its
  node's `gain_db` fades the group in the mix. If it feeds another node, use the outgoing wire's
  `gain_db` or an explicit `Gain` effect upstream to scale that signal, not the bus's mix fader.
- `builtin = "duck"` is a sidechain duck: the node's output is pulled down while a `control`
  wire's signal is loud. Its `parameters` are `attack_ms`,
  `release_ms`, and `amount_db` (defaults 10, 150, and 6): the follower rises to a louder control
  level over `attack_ms`, falls from a quieter one over `release_ms`, and a full-scale control
  level pulls the gain down by exactly `amount_db`. With no control wire it passes its audio
  through unchanged.

A control wire is accepted only by `duck`. Any other recipe refuses one at render time with
`load_failed`, naming the effect and the missing sidechain input: whether a plugin exposes a
sidechain is not poser's to promise without loading it, and build time loads no plugin.
