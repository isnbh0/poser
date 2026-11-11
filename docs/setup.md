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
unqualified Git URL can select a later commit. Choosing an explicit revision is optional, not a
missing locking feature; do not discard an existing lockfile. The initializer
refuses to overwrite its owned output files; it merges required entries into an existing
`.gitignore`.

The setup copies `poser.toml` and `songs/demo.yaml` into your studio. Edit these for your own
instruments and songs. Put studio-specific agent instructions in `AGENTS.md`; `CLAUDE.md` points
to those instructions. `POSER.md` is generated from poser, the current configuration, and its
adapters. After changing `poser.toml` or other guide inputs, run `uv run update-guide` from the
studio; use `uv run update-guide --check` to check whether the guide is current. Your `AGENTS.md`
edits remain yours.

The check exercises instrument loading and note output. Status and exit code determine failure:
exit 0 means all instruments pass, 1 means an instrument failed, and 2 means a configuration or
workspace error. `repeatable` reports exact sample-value equality between two renders, with no
tolerance, not perceptual quality. A `pass` with `REPEATABLE no` (JSON `repeatable: false`) is
valid. Clipping is informational in instrument checking, unlike clipping defects in song analysis.

After successful demo rendering and analysis, invite the person to listen to
`renders/songs/demo.yaml/latest/mix.wav`; its report and spectrograms are also under
`renders/songs/demo.yaml/latest/`. Audition yourself only with a usable listening route and
permission. Starting a player is not evidence you heard the result. If you cannot listen, disclose
that it is not auditioned and ask the person to audition. Lack of audition does not invalidate
technical setup verification.

## Compose your first song (optional)

After successful setup, offer to continue to a first song. Setup-only is a complete outcome;
declining needs no creative questionnaire. Reuse preferences already provided. Ask only for
consequential missing intent, such as mood, approximate length, or references; accept delegated
choices and state provisional defaults.

Read the studio's `AGENTS.md` and generated `POSER.md`, and run `uv run list-sounds`. Create a
new song without overwriting the demo or the owner's work. In the commands below, replace
`<song-path>` with that new source's path (YAML or Python); it is a placeholder, not a musical brief.
Run from the studio:

```bash
uv run render-song <song-path> &&
uv run analyze-song <song-path> --json
```

Inspect the JSON report's node and mix defects and spectrograms. Revise in response to the brief
and actual results, then render and analyze again. Invite the person to listen, or audition with
a usable route and permission, as for the demo. Disclose when you cannot listen and request the
person's audition; do not block delivery of a technically verified draft for lack of listening.
Analysis success is not artistic approval or a mastering guarantee.

### Final handoff

Preserve the editable song source and verify its bytes remain unchanged across a successful
render followed by successful analysis. Inspect that analysis's report and its reported mix path;
use that mix, not an assumed filename. For this reference setup the rendered mix is a WAV.
Copy or deliver the unchanged source, that report, and that mix together before another run can
replace the outputs. Include local source dependencies needed to reproduce a Python song;
unchanged source bytes alone do not establish reproducibility for scripts using external state.

`song_sha256` identifies the record writer's serialized loaded song, not necessarily the editable
source file; do not compare it with a raw source-file hash. Even a historical report's mix path
can refer to mutable `latest` output, so retaining that report alone does not establish matching
audio. Never hand off an earlier mix as the current successful result after a failed latest attempt.

Deliver or identify the editable source and matching WAV, report and spectrogram locations,
analysis result, a brief interpretation, and listening status (including “not auditioned” when
applicable). If attachments are unavailable, give accessible paths and disclose which point to
mutable `latest` output. Preserve the matching artifacts before any further run even when sharing
paths instead of attachments.

## Recover from a failure

If initialization reports a missing plugin or invalid path, locate or install Surge, or specify
its existing location as described in the [Surge guide](../src/poser/reference_setups/surge/README.md),
and retry `uv run init-workspace --setup surge` with any needed overrides. Failed prerequisite
checks create no studio template files, but the Python project and dependency created earlier
remain. Resolve any reported existing-file conflicts rather than overwriting them.

If initialization succeeds but `check-instruments` fails, keep the studio and read the diagnostic:
real failures include load errors, crashes, timeouts, non-finite output, silence, or missing notes,
not just a missing plugin. Do not repair or reinitialize solely because repeatability is false. Correct
the installation or current `poser.toml`, run `uv run update-guide` if its inputs changed, and
rerun `uv run check-instruments` before rendering and analyzing. Do not delete studio files or
rerun initialization to repair an initialized studio.
