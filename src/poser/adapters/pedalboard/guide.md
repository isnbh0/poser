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
