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

The workspace that `init-workspace` creates describes the instruments it detects on this
machine: with Surge XT installed you get a Surge XT workspace whose demo song is ready to
render; with only Serum installed you get a Serum workspace; with neither you get the
scaffold, which names Surge XT at its documented paths, and a note.

No synthesizer installed? Surge XT (https://surge-synthesizer.github.io) is a capable, free
one, and poser's default instrument. Its packaged installer installs for all users, putting
the plugin at `/Library/Audio/Plug-Ins/VST3/Surge XT.vst3` and the factory patches at
`/Library/Application Support/Surge XT/patches_factory`; a per-user install puts them under
`~/Library` instead. Install it and run `uv run check-instruments`; if it lives elsewhere,
set the paths in `poser.toml` to match and run `uv run update-guide` afterwards. For another
instrument, name it in `poser.toml`, point `songs/demo.yaml`'s sound names at it, and run
`uv run update-guide`.

## Start a workspace

```bash
uv init --bare --python 3.13 my-studio && cd my-studio
uv add git+https://github.com/isnbh0/poser
uv run init-workspace
# the workspace's poser.toml and songs/demo.yaml then describe the instruments found on this
# machine; edit them only when something lives in an unusual place, then:
uv run check-instruments
uv run render-song songs/demo.yaml
uv run analyze-song songs/demo.yaml
```

`init-workspace` writes `poser.toml`, a demo song, the composing guide as `AGENTS.md`, and a
`CLAUDE.md` that points to it, so coding agents working in the workspace read the guide. The other
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

## Developing poser

`make check` runs format, lint, types, and the tests that need no plugins. `make test-real` loads
real plugins; it reads a `poser.toml` at the repository root, so copy
`src/poser/scaffold/poser.toml` there and edit its paths first. [AGENTS.md](AGENTS.md) describes
the layout and rules.

## License

poser is licensed under GPL-3.0-or-later; see [LICENSE](LICENSE). poser depends on pedalboard,
which is GPL-3.0. The instruments you load keep their own licenses.
