# poser

[![check](https://github.com/isnbh0/poser/actions/workflows/check.yml/badge.svg)](https://github.com/isnbh0/poser/actions/workflows/check.yml)

You write a song as code, in Python or YAML. poser renders each track through a real VST3 or
Audio Unit instrument via [pedalboard](https://github.com/spotify/pedalboard), mixes the stems,
and writes a JSON report and spectrograms, so that an agent, or a person, can revise the song.

## Requirements

- macOS
- Python 3.13 or later
- [uv](https://docs.astral.sh/uv/)
- At least one VST3 or Audio Unit instrument

Initialization requires an explicit reference setup. The `surge` setup uses the free Surge XT
VST3; see the [Surge setup guide](src/poser/reference_setups/surge/README.md) for installation
and default locations. If the plugin is not found in the checked locations, initialization fails
without creating a workspace or falling back to another instrument. Install Surge with appropriate
permission, or retry with `--plugin-path PATH` for an existing installation. Use `--presets-path
PATH` to select a custom factory-preset directory; presets are optional. After initialization,
run `uv run check-instruments` to verify the instrument can render.

## Start a workspace

```bash
uv init --bare --python 3.13 my-studio && cd my-studio
uv add git+https://github.com/isnbh0/poser
uv run init-workspace --setup surge
uv run check-instruments
uv run render-song songs/demo.yaml
uv run analyze-song songs/demo.yaml
```

`init-workspace` writes `poser.toml`, a demo song, editable studio instructions in `AGENTS.md`,
a generated composing reference in `POSER.md`, and a `CLAUDE.md` pointing to `AGENTS.md`. The other
commands find the workspace by walking up from the working directory to the nearest
`poser.toml`. Songs are named by their path in the workspace, and their results are kept under
`renders/`.

The demo is two Surge XT tracks; its mix and a spectrogram per track land under
`renders/songs/demo.yaml/latest/`. Play `mix.wav` to hear the song, and see its shape at a
glance:

![the demo mix's spectrogram](docs/demo-spectrogram.png)

The report that drives the next revision (here abridged):

```json
{
  "song": "demo",
  "run": 1,
  "tracks": [
    {
      "name": "lead",
      "status": "analyzed",
      "gain_db": -3.0,
      "peak_dbfs": -10.383672189873698,
      "integrated_lufs": -18.651026814438925,
      "clipped_samples": 0,
      "defects": []
    }
  ],
  "mix": {
    "peak_dbfs": -10.05617342416529,
    "integrated_lufs": -19.345337407632602,
    "spectrogram": "renders/songs/demo.yaml/latest/images/[mix].png",
    "defects": []
  }
}
```

## Configuration

`poser.toml` names the workspace's instruments. Each table under `instruments` is one
instrument, and its name is what songs write before the colon in a track's `sound`:

```toml
[instruments.surge]
adapter = "pedalboard"
path = "/Library/Audio/Plug-Ins/VST3/Surge XT.vst3"
guide = "A note about this instrument for the workspace guide."

[instruments.surge.sources]
presets = "/Library/Application Support/Surge XT/patches_factory"
```

A song names `surge` for the default patch, `surge:presets/<path without suffix>` for a preset
in a source, and `surge:saved/<name>` for a sound saved with `save-sound`. The `pedalboard`
adapter takes `path`, `plugin_name` for a bundle holding several plugins, `sources`, and
`formats`, the preset formats it reads (`fxp`, and `saved` for saved sounds, by default).
Relative paths resolve against the directory holding `poser.toml`. By default a source's presets
are `.fxp` files; an instrument's `formats` option names the preset formats it reads, and
`list-sounds` notes the files in a source it cannot use.

An optional `[adapters]` table chooses the adapter for every other part of poser. Each key takes
a built-in name, an import path, or a table with `use` and options:

| Key | Default |
|---|---|
| `song_formats` | `["yaml", "script"]` |
| `record_writer` | `"yaml"` |
| `audio_files` | `"wav"` |
| `render_store` | `"files"` |
| `mixer` | `"gain"` |
| `measurer` | `"standard"` |
| `visualizer` | `"spectrogram"` |
| `sections` | `[]` |

Edit `AGENTS.md` for studio preferences and each instrument's `guide` in `poser.toml` for
instrument notes. `POSER.md` is generated from poser's core guide, instrument notes, and adapter
guides; edit adapter guides for backend-specific guidance. Run `uv run update-guide` after changing
`poser.toml`, and `uv run update-guide --check` to see whether the reference is current. The command
writes only `POSER.md`, and refuses to replace one it did not generate. `CLAUDE.md` points to
`AGENTS.md`.

## Commands

- `uv run init-workspace --setup surge`: write the selected reference setup and guide into a directory.
- `uv run check-instruments`: check that each instrument loads, makes sound, and renders the same
  notes twice alike.
- `uv run list-sounds`: list every sound a song can name, with a description.
- `uv run render-song <song>`: render one aligned stem per track, rendering again only tracks
  whose inputs changed.
- `uv run analyze-song <song>`: mix the stems and write the report, spectrograms, and a numbered
  record of the analysis.
- `uv run save-sound <instrument> <name> --description <text>`: open an instrument's window and
  save its state as a sound when the window closes. It is for a person at the screen, not an
  agent.
- `uv run update-guide`: write `POSER.md` from poser and `poser.toml`; `--check` only compares.

Every command but `init-workspace` takes `--config`, and those that use saved sounds or results
take `--sounds-dir` or `--output-dir`, to work outside a workspace or on another one. With
`--config`, the workspace is the directory holding that file, and no search runs.

## Adapters of your own

Any adapter key, and an instrument's `adapter`, can name a factory by import path,
`module:attribute`:

```toml
[instruments.drums]
adapter = "studio_adapters.drums:create"
kit = "brushes"

[adapters]
sections = [{ use = "studio_adapters.sections:create", threshold = 3 }]
```

An instrument's factory takes `(name, options, context)`, the render store's takes
`(options, context, stem_suffix)`, and every other one takes `(options, context)`. Options are the
table's other keys; `context` holds the workspace root and the sounds and renders directories the
command uses. Each factory returns an object meeting its port in `poser.core.ports`, and raises
`ConfigError` for options it cannot use.

Building an adapter loads no plugin, starts no process, writes no file, and reads nothing but its
arguments and imported modules. Work with side effects waits for a method call, and a `guide`
property reads only package data, so `update-guide` works on a machine without the plugins.

The module must be importable in the workspace's environment, which poser's worker processes
share. In a uv workspace, make the workspace a package (`[tool.uv] package = true` in its
`pyproject.toml`, with its modules under `src/`), or depend on a package holding the adapter.

## Developing poser

`make check` runs format, lint, types, and the tests that need no plugins. `make test-real` loads
real plugins; it reads a `poser.toml` at the repository root, so copy
`src/poser/reference_setups/surge/poser.toml` there and edit its paths first. [AGENTS.md](AGENTS.md) describes
the layout and rules.

## License

poser is licensed under GPL-3.0-or-later; see [LICENSE](LICENSE). poser depends on pedalboard,
which is GPL-3.0. The instruments you load keep their own licenses.
