# Create a studio

A studio is your own project for songs, configuration, sounds, and renders. A reference setup
is a curated starting configuration and demo copied into that project; an adapter connects poser
to a backend that plays instruments. This guide uses the free Surge XT reference setup on macOS.

To have an agent help, copy the [bootstrap prompt](bootstrap.md), or follow the same procedure
below yourself.

## Prerequisites

You need macOS, [uv](https://docs.astral.sh/uv/), and Python 3.13 or later available to uv.
Install or locate Surge XT before initializing; the
[Surge guide](../src/poser/reference_setups/surge/README.md) covers installation, searched
locations, and custom paths.

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
`uv init` or `cd` fails, you remain in the parent directory, so resolve that error before
continuing.

Use the Git dependency shown here, not the unrelated PyPI package named `poser`. If adapting
these steps to an existing project, inspect its files and dependencies first. The studio's
`uv.lock` pins the resolved poser commit; keep it with the project, because re-resolving the bare
Git URL can select a later commit.

The setup copies `poser.toml` and `songs/demo.yaml` into your studio. Edit `poser.toml` for your
instruments and create new files for your songs; keep `songs/demo.yaml` as the smoke-test demo.
Put studio-specific agent instructions in `AGENTS.md`; `CLAUDE.md` points to them. `POSER.md` is
generated from poser, the current configuration, and its adapters. After changing `poser.toml` or
other guide inputs, run `uv run update-guide` from the studio, and `uv run update-guide --check`
to check whether the guide is current. `update-guide` rewrites only `POSER.md`.

`check-instruments` checks that each instrument loads and plays notes. Exit 0 means all
instruments pass, 1 means an instrument failed, and 2 means a configuration or workspace error.
`repeatable` says whether two renders were sample-identical. A `pass` with `REPEATABLE no` (JSON
`repeatable: false`) is still a pass and needs no repair. Clipping is reported but does not fail
the check.

The demo mix is `renders/songs/demo.yaml/latest/mix.wav`; its report and spectrograms are in
the same `latest/` directory. Analysis reports technical defects; listening to the mix is the
only check of how it sounds.

## Composition advice

`POSER.md` includes composition advice by default. To start a studio without it, replace the
initializer invocation above with `uv run init-workspace --setup surge --advice off`. In an
existing studio, add this to `poser.toml`:

```toml
[guidance]
advice = false
```

Then run `uv run update-guide` and `uv run update-guide --check`. Set `advice = true` to restore
it.

## Compose your first song (optional)

Read the studio's `AGENTS.md` and generated `POSER.md`, and run `uv run list-sounds` to see every
sound a song can name. To narrow a large listing, repeat `--instrument <exact-name>` to scan only
those instruments, and add `--match <substring>` to filter IDs and descriptions
case-insensitively in listing order, for example
`uv run list-sounds --instrument surge --match pad --json`. `list-sounds` exits 2 for an unknown
instrument and 1 when a selected source has a problem, even if no entries match; an empty match
exits 0.

Create a new song file in YAML or Python, and replace `<song-path>` below with its path. Run from
the studio:

```bash
uv run render-song <song-path> &&
uv run analyze-song <song-path> --json
```

Inspect the JSON report's node and mix defects and the spectrograms, revise against your brief and
the results, then render and analyze again.

### Final handoff

For a delivery that poser verifies against its render and analysis, run:

```bash
uv run export-song <song-path> --destination deliveries/first-song
```

Use a new destination each time, and add `--include <source-relative-file>`, once per file, for
each local Python file the song depends on. For Surge the mix is WAV; other adapters may use
another format. If export fails, fix the reported problem and rerun export; don't hand over an
earlier mix.

See the [export contract](#export-contract) for exit codes, manifest fields, and portability limits.

## Recover from a failure

If initialization reports a missing plugin or invalid path, locate or install Surge, or specify
its existing location as described in the
[Surge guide](../src/poser/reference_setups/surge/README.md), and retry
`uv run init-workspace --setup surge` with any needed overrides. Failed prerequisite checks create
no studio template files, but the Python project and dependency created earlier remain. After an
interrupted initialization, retry the same command, and keep `.poser-init`, which the initializer
uses for recovery. The initializer refuses to overwrite files it owns and merges its required
entries into an existing `.gitignore`; if it refuses, follow the diagnostic, which names the
conflicting path.

If initialization succeeds but `check-instruments` fails, keep the studio and read the diagnostic.
Failures include load errors, crashes, timeouts, non-finite output, silence, and missing notes.
Correct the installation or `poser.toml`, run `uv run update-guide` if its inputs changed, and
rerun `uv run check-instruments` before rendering and analyzing. Never reinitialize or delete files
to repair a studio that initialized successfully.

## Export contract

`export-song` loads the song once and holds one lock across render, analysis, and publication. It
takes the same configuration, sounds, and output path options as `render-song` and
`analyze-song`.

### Guarantees

- Export publishes only on success. Failed execution or analysis, or a change to the source,
  configuration, or lockfiles during export, publishes no bundle.
- The destination must be new; an existing one is refused. It receives `source/`, `audio/`,
  `images/`, `report.json`, and a schema-version-2 `manifest.json`.
- The manifest binds the SHA-256 hashes of the source and bundled artifacts to the analysis run
  and to `song_sha256`. `song_sha256` digests the serialized song, so it differs from a hash of
  the source file.
- Report and manifest artifact paths stay valid when the bundle moves.
- The manifest always records `listening: "not_auditioned"`.
- `--force` bypasses render caching; every safety check still runs.

### Exit codes

- 0: published.
- 1: execution, analysis, changed input, or I/O failure.
- 2: invalid arguments or configuration.
- 3: another command holds the song.

`--json` reports the outcome and diagnostics, and the bundle paths when export succeeds.

### Paths

- Includes resolve from the source root. With all path options explicit, the source root is the
  invocation directory, even when the configuration lives elsewhere; otherwise it is the
  discovered or configured workspace.
- The destination and explicit path options resolve from the invocation directory.
- Unsafe includes, source escapes, and overlap with inputs or with roots the render store mutates
  are refused. A render store can export only if its `protected_roots()` declares its roots.

### Not covered

- The configuration and plugin and preset binaries are not bundled; the recipient supplies them.
- The manifest records configuration and `uv.lock` digests (or their absence), labeled by role
  for the source and configuration roots. They record what was present and do not verify the
  Python environment that ran export.
- Section payloads are copied as opaque JSON with their embedded paths unchanged; when a bundle
  has them, the manifest declares `section_payload_portability: "opaque_not_verified"`.
- Export does not sandbox Python or detect undeclared imports or other external inputs; declare
  local source dependencies with `--include`.
- To deliver an earlier result, run export again.
