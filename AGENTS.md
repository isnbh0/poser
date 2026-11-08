# Developing poser

poser renders songs written as code through VST instruments and measures the result. This guide is
for changing poser itself. The composer guide that every workspace receives as its `AGENTS.md` is
`src/poser/scaffold/guide.md`.

## Layout

- `src/poser/`: the command modules (`init_workspace`, `plugin_check`, `render`, `analyze`, and
  `sounds`, which holds `save-sound` and `list-sounds`) and the modules they share (`song`,
  `music`, `presets`, `workspace`, and `worker`).
- `src/poser/scaffold/`: package data that `uv run init-workspace` writes into a new workspace.
- `tests/`: the tests. Tests marked `real_plugins` load real plugins.

## Rules

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
