# Developing poser

poser renders songs written as code through instrument backends and measures the result. This guide is
for changing poser itself. A studio's `AGENTS.md` is copied once from
`src/poser/scaffold/agents.md` and belongs to its owner. `update-guide` generates `POSER.md` from
the core guide, `src/poser/scaffold/guide.md`, instrument notes in `poser.toml`, and adapter guides.
Studio preferences belong in its `AGENTS.md`; instrument notes belong in its `poser.toml`.

## Layout

- `src/poser/core/`: the models, the ports (`core/ports.py`), and the use cases (`loading`,
  `rendering`, `checking`, `capturing`, and `analysis`). The core imports only the standard
  library, numpy, and itself.
- `src/poser/adapters/`: the adapters behind the ports, such as the Pedalboard instrument in
  `adapters/pedalboard/`, the file render store, and the analysis adapters.
- `src/poser/compose.py`: the composition root. It reads `poser.toml` and builds the adapters it
  selects, by built-in name or import path.
- `src/poser/cli/`: the commands, which load the composition and run a use case on it, and
  `cli/workspace.py`, which finds the workspace.
- `src/poser/scaffold/`: instrument-neutral workspace resources and the core guide.
- `src/poser/reference_setups/surge/`: the Surge reference config, demo, and setup instructions.
- `tests/`: the tests. Tests marked `real_plugins` load real plugins.

## Rules

- `tests/test_guide.py` checks `src/poser/scaffold/guide.md` against the code. A change to song
  keys, report fields, statuses, bands, music helpers, or scripts updates the guide in the same
  commit. The core guide holds only text true in every workspace; text about one adapter is that
  adapter's `guide`, and text about one instrument is its `guide` in `poser.toml`.
- Library functions take explicit paths. Only command entry points search for a workspace,
  through `poser.cli.workspace`.
- Commands import no adapter; `poser.compose` imports only the core and
  `poser.adapters.loading`, and names poser's own adapters by import path.
- Plugin code runs only in worker subprocesses, which the Pedalboard adapter starts through
  `poser.adapters.isolation`.
- Building an adapter loads no plugin, starts no process, writes no file, and reads nothing but
  its arguments and imported modules. Work with side effects waits for a method call, and a
  `guide` property reads only package data, so a guide composes on a machine without the plugins.
- `typing.Any` is banned (ruff `TID251`), and the line length is 100.

## Checking

- `make check` runs format, lint, types, and the tests that need no plugins.
- `make test-real` loads the instruments in a root `poser.toml`. To create one, copy
  `src/poser/reference_setups/surge/poser.toml` and edit its paths.
- `make check-instruments` checks that each instrument in the root `poser.toml` loads and sounds.
- To try the commands, run `uv --project <this repo> run init-workspace --setup surge` in a scratch directory
  outside the repo, then run the other commands there the same way.
