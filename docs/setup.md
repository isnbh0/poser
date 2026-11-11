# Create a studio

A studio is your own project for songs, configuration, sounds, and renders. A reference setup
is a curated starting configuration and demo copied into that project; an adapter connects poser
to a backend that plays instruments. This guide uses the free Surge XT reference setup on macOS.

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
these steps to an existing project, inspect its files and dependencies first. The studio's `uv.lock`
records the resolved Git commit; retain it with the project for reproducibility. A newly resolved
unqualified Git URL can select a later commit. The initializer
refuses to overwrite its owned output files; it merges required entries into an existing
`.gitignore`.

The setup copies `poser.toml` and `songs/demo.yaml` into your studio. Edit `poser.toml` for your
instruments and create new files for your songs; preserve `songs/demo.yaml` as the smoke-test demo.
Put studio-specific agent instructions in `AGENTS.md`; `CLAUDE.md` points to those instructions.
`POSER.md` is generated from poser, the current configuration, and its adapters. After changing
`poser.toml` or other guide inputs, run `uv run update-guide` from the studio; use `uv run update-guide --check` to check whether the guide is current. Your `AGENTS.md`
edits remain yours.

Advice is optional and independent of instrument settings. For first-run advice-free generation,
replace the initializer invocation above with `uv run init-workspace --setup surge --advice off`.
The default is on. In an existing studio set `[guidance]` with `advice = false` in `poser.toml`
(or `true` to restore advice), then run `uv run update-guide` and `uv run update-guide --check`.
This changes generated documentation, not rendering or analysis thresholds. Owner instructions
and custom instrument notes are not rewritten. Evaluation operators should use the separate
[ablation procedure](ablation.md) before starting composing sessions.

The check exercises instrument loading and note output. Status and exit code determine failure:
exit 0 means all instruments pass, 1 means an instrument failed, and 2 means a configuration or
workspace error. `repeatable` reports exact sample-value equality between two renders, with no
tolerance, not perceptual quality. A `pass` with `REPEATABLE no` (JSON `repeatable: false`) is
valid. Clipping is informational in instrument checking, unlike clipping defects in song analysis.

The demo mix is `renders/songs/demo.yaml/latest/mix.wav`; its report and spectrograms are in
the same `latest/` directory. Listen with permission if useful. Listening is optional; report
whether it occurred. Analysis checks technical defects, not musical quality.

## Compose your first song (optional)

If a song was requested, continue after setup; otherwise offer to compose one. Reuse the owner's
preferences and ask only for missing creative direction, such as mood or approximate length.

Read the studio's `AGENTS.md` and generated `POSER.md`, and run `uv run list-sounds`.
For large listings use `uv run list-sounds --instrument surge --match pad --json`; instrument
selection is exact and repeatable, and matching searches IDs/descriptions case-insensitively.
A listing identifies available presets; it does not audition them. Create a new song without
overwriting the demo or the owner's work. Replace `<song-path>` below with its YAML or Python path.
Run from the studio:

```bash
uv run render-song <song-path> &&
uv run analyze-song <song-path> --json
```

Inspect the JSON report's node and mix defects and spectrograms. Revise against the brief and
results, then render and analyze again. Listening feedback can guide further revisions, but is
not required to deliver a draft.

### Final handoff

For a poser-verified matching delivery, run:

```bash
uv run export-song <song-path> --destination deliveries/first-song
```

Use a new destination each time. Add repeatable `--include <source-relative-file>` for declared
local Python source dependencies. Share the successful bundle, or accessible paths to it, with
analysis interpretation and listening status. For Surge the mix is WAV; other adapters may use
another format. If export fails, resolve the diagnostic rather than substituting an earlier mix.

See the [export contract](#export-contract) for exit codes, manifest fields, and portability limits.

## Recover from a failure

If initialization reports a missing plugin or invalid path, locate or install Surge, or specify
its existing location as described in the [Surge guide](../src/poser/reference_setups/surge/README.md),
and retry `uv run init-workspace --setup surge` with any needed overrides. Failed prerequisite
checks create no studio template files, but the Python project and dependency created earlier
remain. After an interrupted initialization, retry the same command. Preserve `.poser-init`,
which the initializer uses for recovery. If recovery refuses, follow the named-path diagnostic;
resolve existing-file conflicts rather than deleting the studio or overwriting owner edits.

If initialization succeeds but `check-instruments` fails, keep the studio and read the diagnostic:
real failures include load errors, crashes, timeouts, non-finite output, silence, or missing notes,
not just a missing plugin. Do not repair or reinitialize solely because repeatability is false. Correct
the installation or current `poser.toml`, run `uv run update-guide` if its inputs changed, and
rerun `uv run check-instruments` before rendering and analyzing. Do not delete studio files or
rerun initialization to repair an initialized studio.

## Export contract

Export owns one loaded song and one lock across render, analysis, and publication; failed
execution or analysis publishes no bundle. The unused destination receives `source/`, `audio/`,
`images/`, `report.json`, and schema-version-2 `manifest.json`. It is not mutable `latest`.
A collision is refused, never replaced. Exit codes: 0 published; 1 execution, analysis, changed
input, or I/O failure; 2 invalid arguments/configuration; 3 song contention. `--json` reports the
outcome and diagnostics, with bundle paths only on success. `--force` bypasses render caching,
not safety checks. Configuration, sounds, and output path options work as for render/analyze.

The manifest binds source and artifact SHA-256 hashes to the analysis run and serialized-song
`song_sha256`; that last digest is not a raw source-file hash. Configuration and role-labeled
source/configuration-root `uv.lock` digests (or absence) are evidence, not proof of the executing
Python environment. Configuration itself and plugin/preset binaries are not bundled. Includes
resolve from the source root, not implicitly from the configuration directory. With all path
options explicit the source root is the invocation directory; otherwise it is the discovered or
configured workspace. Destination and explicit path options resolve from the invocation directory.
Unsafe includes, source escapes, and overlap with inputs or store-mutated roots are refused.
Legacy stores without declared protected roots cannot perform verified export.

Source/configuration/lockfile changes prevent publication. Export does not read or compose
`POSER.md`.

Schema-owned report/manifest artifact paths remain valid when the bundle moves. Section payloads
are opaque JSON, preserved without copying or interpreting embedded paths; when present the
manifest declares `section_payload_portability: "opaque_not_verified"`. Hashes cover bundled
artifacts other than the manifest itself. Export always records `listening: "not_auditioned"`;
actual audition belongs in separate evidence. It cannot sandbox Python, detect every external
input or undeclared import, prevent external change-and-revert races, or make plugins deterministic.
Prior historical reports require a new export run; they do not retain matching historical audio.
