# Create a studio

A studio is your own project for songs, configuration, sounds, and renders. A reference setup
is a curated starting configuration and demo copied into that project; an adapter connects poser
to a backend that plays instruments. This guide uses the Surge XT reference setup.

To have an agent help, copy the [bootstrap prompt](bootstrap.md). Or follow the same procedure
manually below.

## Prerequisites

You need macOS, [uv](https://docs.astral.sh/uv/), and Python 3.13 or later available to uv.
Install or locate Surge XT before initializing; the [Surge guide](../src/poser/reference_setups/surge/README.md)
covers installation, searched locations, and custom paths. Installing software requires appropriate
permission. Poser does not install the synthesizer.

## Create and verify

Choose a parent directory for a new studio. From that directory, run:

```bash
uv init --bare --python 3.13 my-studio &&
cd my-studio &&
uv add git+https://github.com/isnbh0/poser &&
uv run init-workspace --setup surge &&
uv run check-instruments &&
uv run render-song songs/demo.yaml &&
uv run analyze-song songs/demo.yaml
```

The chain stops at the first failure. After `cd` succeeds, your shell stays in the studio; if
`uv init` or `cd` fails, remain in the parent directory and resolve that error before continuing.

Use the Git dependency shown here, not the unrelated PyPI package named `poser`. If adapting
these steps to an existing project, inspect its files and dependencies first. The initializer
refuses to overwrite its owned output files; it merges required entries into an existing
`.gitignore`.

The setup copies `poser.toml` and `songs/demo.yaml` into your studio. Edit these for your own
instruments and songs. Put studio-specific agent instructions in `AGENTS.md`; `CLAUDE.md` points
to those instructions. `POSER.md` is generated from poser, the current configuration, and its
adapters. After changing `poser.toml` or other guide inputs, run `uv run update-guide` from the
studio; use `uv run update-guide --check` to check whether the guide is current. Your `AGENTS.md`
edits remain yours.

The check exercises instrument loading, sound output, and repeatability. After the demo renders
and analyzes, listen to `renders/songs/demo.yaml/latest/mix.wav`; its report and spectrograms are
also under `renders/songs/demo.yaml/latest/`.

## Recover from a failure

If initialization reports a missing plugin or invalid path, locate or install Surge, or specify
its existing location as described in the [Surge guide](../src/poser/reference_setups/surge/README.md),
and retry `uv run init-workspace --setup surge` with any needed overrides. Failed prerequisite
checks create no studio template files, but the Python project and dependency created earlier
remain. Resolve any reported existing-file conflicts rather than overwriting them.

If initialization succeeds but `check-instruments` fails, keep the studio and read the diagnostic:
the failure may concern loading, sound output, or repeatability, not just a missing plugin. Correct
the installation or current `poser.toml`, run `uv run update-guide` if its inputs changed, and
rerun `uv run check-instruments` before rendering and analyzing. Do not delete studio files or
rerun initialization to repair an initialized studio.
