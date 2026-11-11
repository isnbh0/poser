# poser

[![check](https://github.com/isnbh0/poser/actions/workflows/check.yml/badge.svg)](https://github.com/isnbh0/poser/actions/workflows/check.yml)

Write a song as code in Python or YAML. poser renders its tracks through instrument backends,
mixes the stems, and produces reports and spectrograms for a person or agent to use in revisions.
Backends include VST3 and Audio Unit instruments through [pedalboard](https://github.com/spotify/pedalboard)
and [scsynth](packages/poser-scsynth/README.md).

## Create your studio

A studio is your own project for songs, instruments, and renders. Follow the [setup guide](docs/setup.md)
to create and verify one yourself, or copy the [agent bootstrap prompt](docs/bootstrap.md) to have
an agent help. Both routes use the same procedure: the free Surge XT reference setup on macOS,
with optional continuation to your first song. This platform scope describes the reference setup,
not every poser backend.

Hear an example and get its source in the [optional showcase](docs/showcase.md).

## Surge demo

The eight-beat default-patch demo is a smoke test, not a finished-song showcase.
The demo is two Surge XT tracks; its mix and a spectrogram per track land under
`renders/songs/demo.yaml/latest/`. Play `mix.wav` to hear the song, and see its shape at a
glance:

![the demo mix's spectrogram](docs/demo-spectrogram.png)

The report that drives the next revision (here abridged). Song tracks appear as analysis `nodes`,
alongside any effect nodes; the song authoring key remains `tracks`. Metric values below are
illustrative, not a reproducible audio fingerprint:

```json
{
  "song": "demo",
  "run": 1,
  "nodes": [
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
guides; adapter maintainers supply the backend-specific guidance. Run `uv run update-guide` after changing
`poser.toml`, and `uv run update-guide --check` to see whether the reference is current. The command
writes only `POSER.md`, and refuses to replace one it did not generate. `CLAUDE.md` points to
`AGENTS.md`.

Optional composition advice defaults to on. Initialize without it using
`uv run init-workspace --setup surge --advice off`. For an existing studio, add:

```toml
[guidance]
advice = false
```

Then run `uv run update-guide` and `uv run update-guide --check`. Set `true` to restore advice.
This choice changes generated advice, not rendering or analysis. Owner instructions and custom
instrument notes remain untouched.

## Commands

- `uv run piano-roll <song> --output <path.png>`: draw authored notes without plugins or audio;
  use `--replace` to atomically replace an existing PNG. Load only trusted Python songs.

- `uv run init-workspace --setup surge`: write the selected reference setup and guide into a directory.
- `uv run check-instruments`: check instrument loading and note output; report exact sample-value
  equality with no tolerance separately as informational repeatability. `REPEATABLE no` alone
  is not failure.
- `uv run list-sounds`: list every sound a song can name, with a description. `--json` retains
  the same entries as JSON. Repeat `--instrument <exact-name>` to select instruments without
  scanning others; `--match <substring>` matches IDs/descriptions case-insensitively, preserving
  listing order. Unknown instruments exit 2; valid empty matches exit 0. Selected-source problems
  still exit 1 even if no entries match. Listing is not auditioning.
- `uv run render-song <song>`: render one aligned stem per track, rendering again only tracks
  whose inputs changed.
- `uv run analyze-song <song>`: mix the stems and write the report, spectrograms, and a numbered
  record of the analysis.
- `uv run export-song <song> --destination <new-directory>`: render, analyze, and publish a
  verified matching delivery; repeat `--include <source-relative-file>` for local dependencies.
- `uv run save-sound <instrument> <name> --description <text>`: open an instrument's window and
  save its state as a sound when the window closes. It is for a person at the screen, not an
  agent.
- `uv run update-guide`: write `POSER.md` from poser and `poser.toml`; `--check` only compares.

Every command but `init-workspace` takes `--config`, and those that use saved sounds or results
take `--sounds-dir` or `--output-dir`, to work outside a workspace or on another one. With
`--config`, the workspace is the directory holding that file, and no search runs.

## Verified delivery

Use `uv run export-song <song> --destination <new-directory>` for a matching delivery.
Export publishes editable `source/`, `audio/`, `images/`, `report.json`, and a hash manifest
only on success. Use a new destination each time and declare local source dependencies with
repeatable `--include <source-relative-file>`.

Render/analyze remain useful for iteration. Export verifies correspondence, not musical quality
or actual listening. Label unauditioned work honestly. See the
[export contract](docs/setup.md#export-contract) for guarantees, limitations, and path rules.

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
`guide` is required operational reference; optional creative/workflow coaching belongs in a
separate package-data-only `advice` property. Instrument `guide` notes are operational reference;
inline instrument `advice` is rejected. For controlled advice experiments, see
[advice ablation](docs/ablation.md).

The module must be importable in the workspace's environment, which poser's worker processes
share. In a uv workspace, make the workspace a package (`[tool.uv] package = true` in its
`pyproject.toml`, with its modules under `src/`), or depend on a package holding the adapter.

## Developing poser

`make check` runs format, lint, types, and the tests that need no plugins. `make test-real` loads
real plugins; it reads a `poser.toml` at the repository root, so copy
`src/poser/reference_setups/surge/poser.toml` there and edit its paths first. [AGENTS.md](AGENTS.md) describes
the layout and rules.
For controlled composition experiments, see [advice ablation](docs/ablation.md).

## License

poser is licensed under GPL-3.0-or-later; see [LICENSE](LICENSE). poser depends on pedalboard,
which is GPL-3.0. The instruments you load keep their own licenses.
