# Developing poser

poser renders songs written as code through VST instruments and measures the result. This guide is
for changing poser itself. The composer guide that every workspace receives as its `AGENTS.md` is
`src/poser/scaffold/guide.md`.

## Layout

- `src/poser/__init__.py`: the composer API that song scripts import, re-exported from the core.
- `src/poser/core/`: the application. Model modules hold values and pure functions (`names`,
  `jsonvalue`, `song`, `music`, `sounds`, `audio`, `performance`, `statuses`, `cache`, `report`,
  `checks`); `ports` declares the protocols adapters implement; use-case modules (`loading`) run
  the model through ports.
- `src/poser/adapters/`: implementations of the ports, such as `song_formats` (YAML and script
  songs) and `wav` (stem files).
- `src/poser/cli/`: what the commands share, such as exit codes and `align_rows`.
- `src/poser/`: the command modules (`init_workspace`, `plugin_check`, `render`, `analyze`, and
  `sounds`, which holds `save-sound` and `list-sounds`) and the modules they share (`presets`,
  `workspace`, and `worker`).
- `src/poser/scaffold/`: package data that `uv run init-workspace` writes into a new workspace.
- `tests/`: the tests. Tests marked `real_plugins` load real plugins. `tests/contracts.py` holds
  the checks every adapter of a port must pass.

## Rules

- `poser.core` imports only the standard library, numpy, and `poser.core`. Within it, model
  modules import only model modules, `ports` imports only model modules, and a use case imports
  model modules, `ports`, and use cases earlier in `USE_CASES`. No module uses a relative import,
  the import graph has no cycle, and package `__init__.py` files hold no imports except the
  composer API. `tests/test_boundaries.py` enforces these rules.
- Core value types check their rules in their constructor and raise `ValueError`, so code that
  receives one never checks again.
- `tests/test_guide.py` checks `src/poser/scaffold/guide.md` against the code. A change to song
  keys, report fields, statuses, bands, music helpers, or scripts updates the guide in the same
  commit.
- Library functions take explicit paths. Only command entry points search for a workspace, through
  `poser.workspace`.
- Plugin code runs only in worker subprocesses (`poser.worker.run_isolated`).
- `typing.Any` is banned (ruff `TID251`), and the line length is 100.

## Checking

- `make check` runs format, lint, types, and the tests that need no plugins.
- `make test-real` loads the plugins listed in a root `plugins.toml`. To create one, copy
  `src/poser/scaffold/plugins.toml` and edit its paths.
- To try the commands, run `uv --project <this repo> run init-workspace` in a scratch directory
  outside the repo, then run the other commands there the same way.
