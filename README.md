# poser

You write a song as code, in Python or YAML. poser renders each track through a real VST3 or
Audio Unit instrument via [pedalboard](https://github.com/spotify/pedalboard), mixes the stems,
and writes a JSON report and spectrograms, so that an agent, or a person, can revise the song.

## Requirements

- macOS
- Python 3.13 or later
- [uv](https://docs.astral.sh/uv/)
- At least one VST3 or Audio Unit instrument

The scaffolded workspace is written for Xfer Serum, a commercial synth. Any other instrument
needs its own table in `poser.toml`.

## Start a workspace

```bash
uv init --bare --python 3.13 my-studio && cd my-studio
uv add git+https://github.com/isnbh0/poser
uv run init-workspace
# edit poser.toml, then:
uv run check-instruments
uv run render-song songs/demo.yaml
uv run analyze-song songs/demo.yaml
```

`init-workspace` writes `poser.toml`, a demo song, the composing guide as `AGENTS.md`, and a
`CLAUDE.md` that points to it, so coding agents working in the workspace read the guide. The other
commands find the workspace by walking up from the working directory to the nearest
`poser.toml`. Songs are named by their path in the workspace, and their results are kept under
`renders/`.

## Configuration

`poser.toml` names the workspace's instruments. Each table under `instruments` is one
instrument, and its name is what songs write before the colon in a track's `sound`:

```toml
[instruments.serum]
adapter = "pedalboard"
path = "/Library/Audio/Plug-Ins/VST3/Serum.vst3"
guide = "A note about this instrument for the workspace guide."

[instruments.serum.sources]
presets = "/Library/Audio/Presets/Xfer Records/Serum Presets/Presets"
```

A song names `serum` for the default patch, `serum:presets/<path without suffix>` for a preset
in a source, and `serum:saved/<name>` for a sound saved with `save-sound`. The `pedalboard`
adapter takes `path`, `plugin_name` for a bundle holding several plugins, `sources`, and
`formats`, the preset formats it reads (`fxp`, and `saved` for saved sounds, by default).
Relative paths resolve against the directory holding `poser.toml`.

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

`AGENTS.md` is written from poser's core guide, each instrument's `guide`, and the guide text of
each adapter in use, so do not edit it; run `uv run update-guide` after changing `poser.toml`, and
`uv run update-guide --check` to see whether it is current. Notes about one instrument belong in
its `guide`. Other notes for agents belong in `CLAUDE.md`, after its `@AGENTS.md` line, or in files
it links to; `update-guide` writes only `AGENTS.md`, and refuses to replace an `AGENTS.md` it did
not write.

## Commands

- `uv run init-workspace`: write the workspace scaffold and its guide into a directory.
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
- `uv run update-guide`: write `AGENTS.md` from poser and `poser.toml`; `--check` only compares.

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

## Upgrading from `plugins.toml`

Workspaces now use `poser.toml`, and sounds are named `<instrument>:<name>`: `Serum` becomes
`serum`, and `Serum:<preset path>` becomes `serum:presets/<preset path>`. Saved sounds live in
`sounds/<instrument>/<name>.json` without a `"plugin"` key; `list-sounds` names any file left in
`sounds/` itself. Every stem renders again once, because sound keys now come from the instrument
adapter. Old-layout files in a song's render directory (`<track>.wav`, `<track>.json`,
`analysis/`, and `mix/`) are ignored and can be deleted; `history/` keeps its numbering.

## Developing poser

`make check` runs format, lint, types, and the tests that need no plugins. `make test-real` loads
real plugins; it reads a `poser.toml` at the repository root, so copy
`src/poser/scaffold/poser.toml` there and edit its paths first. [AGENTS.md](AGENTS.md) describes
the layout and rules.

## License

poser is licensed under GPL-3.0-or-later; see [LICENSE](LICENSE). poser depends on pedalboard,
which is GPL-3.0. The instruments you load keep their own licenses.
