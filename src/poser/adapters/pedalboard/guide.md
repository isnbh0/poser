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
