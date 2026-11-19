# Developing poser

poser renders songs written as code through instrument backends and measures the result. This
guide is for changing poser itself; to set up a studio, people follow `docs/setup.md` and agents
follow `docs/bootstrap.md`. A studio's `AGENTS.md`, copied once from
`src/poser/scaffold/agents.md`, holds its owner's preferences, and its `poser.toml` holds
instrument notes. `update-guide` generates the studio's `POSER.md` from the core reference
(`src/poser/scaffold/guide.md`), those instrument notes, and adapter reference, plus packaged
advice when `[guidance] advice` is enabled.

## Layout

- `src/poser/core/`: the models, the ports (`core/ports.py`), and the use cases, such as
  `loading`, `rendering`, `checking`, `capturing`, `analysis`, `delivery`, and `piano_roll`. The
  core imports only the standard library, numpy, and itself.
- `src/poser/adapters/`: the adapters behind the ports, such as the Pedalboard instrument in
  `adapters/pedalboard/`, the file render store, and the analysis adapters.
- `src/poser/compose.py`: the composition root. It reads `poser.toml` and builds the adapters it
  selects, by built-in name or import path.
- `src/poser/cli/`: the commands, which load the composition and run a use case on it, and
  `cli/workspace.py`, which finds the workspace.
- `src/poser/scaffold/`: instrument-neutral workspace resources and the core guide.
- `src/poser/reference_setups/surge/`: the Surge reference config, demo, and setup instructions.
- `packages/poser-scsynth/`: the SuperCollider `scsynth` instrument adapter, a separate workspace
  package that a studio selects by import path.
- `tests/`: the tests. Tests marked `real_plugins` load real plugins, and tests marked
  `real_scsynth` run a real `scsynth`.

## Rules

- Include `Co-Authored-By` trailers in every AI-assisted commit, naming the actual contributing
  models (including delegated writers) with their exact model IDs and provider noreply addresses.
- `tests/test_guide.py` checks `src/poser/scaffold/guide.md` against the code. A change to song
  keys, report fields, statuses, bands, music helpers, or scripts updates the guide in the same
  commit. The core guide holds only text true in every workspace; text about one adapter is that
  adapter's `guide`, and text about one instrument is its notes (`guide`) in `poser.toml`.
- Library functions take explicit paths. Only command entry points search for a workspace,
  through `poser.cli.workspace`.
- Commands import no adapter; `poser.compose` imports only the core and
  `poser.adapters.loading`, and names poser's own adapters by import path.
- Plugin code runs only in worker subprocesses, which the Pedalboard adapter starts through
  `poser.adapters.isolation`.
- Building an adapter loads no plugin, starts no process, writes no file, and reads nothing but
  its arguments and imported modules. Work with side effects waits for a method call, and a
  `guide` property reads only package data.
- `typing.Any` is banned (ruff `TID251`), and the line length is 100.

## Checking

- `make check` runs format, lint, types, and the tests that need no plugins.
- CI (`.github/workflows/check.yml`) also runs every documented PowerShell example in Windows
  PowerShell 5.1, and the real-plugin and real-scsynth tests on Windows. `tests/doc_blocks.py`
  inventories the shell fences in the onboarding files and `docs/*.md`. A new or changed bash
  example needs an adjacent PowerShell mirror, or a reasoned `EXCEPTIONS` entry, and a
  `SCENARIOS` entry that says how CI runs it. Keep documented examples runnable.
- `make test-real` runs the `real_plugins` tests, which load the instruments in a root
  `poser.toml`, and the `real_scsynth` tests. The test harness finds `scsynth` through
  `SCSYNTH_PATH`, the SuperCollider app bundle, the Windows installer's
  `%ProgramFiles%\SuperCollider-*` folders, or `PATH`; the adapter itself runs only its
  configured absolute path. To create the root `poser.toml`, run
  `uv --project <this repo> run init-workspace --setup surge` in a scratch directory and copy the
  generated `poser.toml`, which holds this machine's resolved paths, to the repository root, as CI
  does. The packaged `src/poser/reference_setups/surge/poser.toml` is the template that setup
  rewrites; its macOS paths are not a working configuration elsewhere.
- `make check-instruments` checks that each instrument in the root `poser.toml` loads and sounds.
- Without `make`, as on Windows, run the `Makefile`'s commands directly: `make check` is
  `uv run ruff format --check .`, `uv run ruff check .`, `uv run ty check`, and `uv run pytest`;
  `make test-real` is `uv run pytest -m "real_plugins or real_scsynth"`; and
  `make check-instruments` is `uv run check-instruments`.
- To try the commands, run `uv --project <this repo> run init-workspace --setup surge` in a
  scratch directory outside the repo, then run the other commands there the same way.
- To run an existing studio's commands against this checkout instead of the poser its
  `pyproject.toml` names, run `uv run --with-editable <this repo> <command>` from the studio.
  There, `uv run --with-editable <this repo> python -c "import poser; print(poser.__file__)"`
  prints which poser is in use.
