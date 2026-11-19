# Create a studio

A studio is your own project for songs, configuration, sounds, and renders. A reference setup is a
curated starting configuration and demo copied into that project; an adapter connects poser to a
backend that plays instruments. This guide uses the free Surge XT reference setup on macOS or
Windows.

To have an agent help, paste the one-line prompt from the
[README](../README.md#create-your-studio), which points the agent at the
[bootstrap prompt](bootstrap.md), or follow the same procedure below yourself.

## Prerequisites

You need Git and [uv](https://docs.astral.sh/uv/). Git is required because poser installs from a
Git URL. uv finds or downloads Python itself, so Python needs no separate install. The studio pins
Python 3.13 in `.python-version`, the version poser's Windows CI tests, and uv downloads it if it is
missing. On Windows, `winget install --id Git.Git -e --source winget` and
`winget install --id=astral-sh.uv -e` install Git and uv; without winget, use the
[Git for Windows installer](https://git-scm.com/downloads/win) and
[uv's PowerShell installer](https://docs.astral.sh/uv/getting-started/installation/). Open a new
PowerShell window afterwards so it finds them. Install or locate Surge XT before initializing; the
[Surge guide](../src/poser/reference_setups/surge/README.md) covers installation, searched
locations, and custom paths.

CI runs poser's tests on macOS and on Windows x64, where CI runs Windows Server, and its real-plugin
and real-SuperCollider tests on Windows x64; on macOS those run locally. Windows on Arm is not
supported: Pedalboard, which plays VST3 instruments, publishes no build for it, and poser refuses to
run there even with x64 Python under emulation. Linux has the file-system support poser needs but is
untested, and the Surge reference setup searches only macOS and Windows locations, so on Linux pass
`--plugin-path` and `--presets-path` to `init-workspace`. Rendering is offline, so it needs no audio
interface, ASIO driver, or other audio driver.

On Windows, pedalboard needs the Microsoft Visual C++ Redistributable (x64), which most machines
already have; if it is missing, poser names it and how to install it.

On Windows, create the studio under a short parent folder such as `C:\studios`, outside
OneDrive-synced folders. Windows 11 often moves Documents and Desktop into OneDrive, whose longer
paths spend the path budget below, and sync can lock render files and interfere with the render
store's links; `init-workspace` warns when the studio is inside OneDrive. Windows Defender's
real-time scanning can slow the first plugin load, and SmartScreen may ask before running the Surge
XT installer.

The studio's filesystem must support directory links and hard links. `init-workspace` checks both
in the destination before publishing scaffold files. Windows NTFS supports these features;
exFAT and FAT32 do not. A failure names the required operation and the underlying reason.

Windows limits a program's paths to 259 characters, and a folder's to 247, unless long paths are
on. A song's results fit while the studio folder's path and the song's path together hold at most
165 characters, such as `C:\studios\my-studio` and `songs/demo.yaml`; poser refuses a song
past that and names the path. To lift the limit, run this once in a PowerShell window opened with
Run as administrator. Windows applies it to programs started afterwards; restart Windows if poser
still reports the limit. Without administrator rights, keep the studio under a short parent folder
instead.

```powershell
New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
```

## Create and verify

Choose a parent directory outside any existing project, because the studio is a project of its
own; `--no-workspace` keeps `uv init` from registering it in an enclosing uv workspace, and
`uv python pin 3.13` keeps the studio on the Python version CI tests. From that directory, run:

```bash
uv init --bare --no-workspace --python 3.13 my-studio &&
cd my-studio &&
uv python pin 3.13 &&
uv add git+https://github.com/isnbh0/poser &&
uv run init-workspace --setup surge &&
uv run check-instruments &&
uv run render-song songs/demo.yaml &&
uv run analyze-song songs/demo.yaml
```

In PowerShell, run these lines in order and stop at the first that fails, since Windows
PowerShell 5.1 has no `&&`:

```powershell
uv init --bare --no-workspace --python 3.13 my-studio
cd my-studio
uv python pin 3.13
uv add git+https://github.com/isnbh0/poser
uv run init-workspace --setup surge
uv run check-instruments
uv run render-song songs/demo.yaml
uv run analyze-song songs/demo.yaml
```

If `uv init` or `cd` fails, you remain in the parent directory; resolve that error before
continuing.

On Windows, run the other chained commands in this guide the same way, one per line. Write a
Windows path in `poser.toml` as a TOML literal string, in single quotes, because a backslash in
double quotes starts an escape: `path = 'D:\Plugins\Surge XT.vst3'`. poser reads the files you
edit, such as `poser.toml`, songs, and saved sounds, as UTF-8 with or without the byte-order mark
that Notepad's "UTF-8 with BOM" and Windows PowerShell 5.1's `Set-Content -Encoding utf8` add, and
writes UTF-8 without one. poser writes its output as UTF-8; before piping it into another
PowerShell command, run
`[Console]::OutputEncoding = [Text.UTF8Encoding]::new()` so PowerShell reads it as UTF-8.

Use the Git dependency shown here, not the unrelated PyPI package named `poser`. To add poser to
an existing project instead, inspect its files and dependencies first, then start from `uv add`.
The studio's `uv.lock` pins the resolved poser commit; keep it with the studio, and commit it if
you use version control, because re-resolving the bare Git URL can select a later commit.

The setup copies `poser.toml` and `songs/demo.yaml` into your studio. Edit `poser.toml` for your
instruments and create new files for your songs; keep `songs/demo.yaml` as the smoke-test demo.
Put studio-specific agent instructions in `AGENTS.md`; `CLAUDE.md` points to them. `POSER.md` is
generated from poser, the current configuration, and its adapters. After changing `poser.toml`,
run `uv run update-guide` from the studio, and `uv run update-guide --check` to check whether the
guide is current. To upgrade poser, follow [upgrading](upgrading.md).

`check-instruments` checks that each instrument loads and plays notes. `repeatable` says whether
two renders were sample-identical. A `pass` with `REPEATABLE no` (JSON `repeatable: false`) is
still a pass and needs no repair. Clipping is reported but does not fail the check.

The demo mix is `renders/songs/demo.yaml/latest/audio/mix.wav`; its `report.json` is in
`latest/`, and its spectrograms are in `latest/images/`. Analysis finds technical defects; listen
to the mix to judge how it sounds. On Windows,
`Start-Process renders\songs\demo.yaml\latest\audio\mix.wav` opens it in the default player
without waiting for it to finish. Playback requires permission, so an agent asks before playing it.

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
sound a song can name. To narrow a large listing, repeat `--instrument <exact-name>` and add
`--match <substring>` to filter IDs and descriptions, for example
`uv run list-sounds --instrument surge --match pad --json`.

Create a new song file in YAML or Python, and replace `<song-path>` below with its path. Run from
the studio:

```bash
uv run render-song <song-path> &&
uv run analyze-song <song-path> --json
```

In PowerShell, run these lines in order and stop at the first that fails:

```powershell
uv run render-song <song-path>
uv run analyze-song <song-path> --json
```

Inspect the JSON report's node and mix defects and the spectrograms, revise against your brief and
the results, then render and analyze again.

### Final handoff

For a delivery that poser verifies against its render and analysis, run:

```bash
uv run export-song <song-path> --destination deliveries/first-song
```

In PowerShell:

```powershell
uv run export-song <song-path> --destination deliveries/first-song
```

Use a new destination each time, and add `--include <source-relative-file>`, once per file, for
each local Python file the song depends on. The mix is WAV. If export fails, fix the reported
problem and rerun export; don't hand over an earlier mix.

See the [export contract](#export-contract) for exit codes, manifest fields, and portability limits.

## Recover from a failure

If a command reports that poser's file-system support or plugin worker isolation does not cover
your platform, it cannot run there yet; see [Prerequisites](#prerequisites).

If a command says pedalboard needs the Microsoft Visual C++ Redistributable, install it as the
message says and rerun the command.

If initialization reports a missing plugin or invalid path, locate or install Surge, or specify its
existing location as described in the [Surge guide](../src/poser/reference_setups/surge/README.md),
and retry `uv run init-workspace --setup surge` with any needed overrides. Retry only
`init-workspace`: a failed prerequisite check publishes no scaffold files and leaves the Python project
and poser dependency in place. An interrupted initialization removes the files it wrote, so retry
the same command. If the process was killed before it could clean up (for example, by SIGKILL or by
ending it in Task Manager), a retry names the files the interrupted run left and suggests
`--remove-leftovers`, which deletes exactly those files and initializes. The initializer refuses to
run if `AGENTS.md`, `POSER.md`, `CLAUDE.md`, `poser.toml`, or `songs/demo.yaml` already exists with
other content, and merges its required entries into an existing `.gitignore`; if it refuses, follow
the diagnostic, which names the conflicting path.

A link-capability probe that cannot clean up reports its remaining scratch directory and refuses
to publish scaffold files. A forcibly killed probe can leave a `.poser-link-probe-*` directory;
ordinary retries and `--remove-leftovers` do not sweep these directories. Inspect a leftover and
confirm it belongs to the interrupted probe before removing it manually. Remove directory links
themselves, never the external directories they lead to, and do not delete entries by prefix alone.

On Windows, a program holding a studio file open, such as a media player playing a render,
Explorer's preview pane, or a virus scanner, stops poser from replacing or removing that file.
poser waits briefly for the program to let go. If it keeps the file open, the command names the
file and says another program may hold it; close that program and run the command again.

If initialization succeeds but `check-instruments` fails, read the diagnostic.
Failures include load errors, crashes, timeouts, non-finite output, silence, and missing notes.
Correct the installation or `poser.toml`, run `uv run update-guide` if its inputs changed, and
rerun `uv run check-instruments` before rendering and analyzing. Never reinitialize or delete files
to repair a studio that initialized successfully.

## Export contract

`export-song` holds one lock across render, analysis, and publication. It takes the same
configuration, sounds, and output path options as `render-song` and `analyze-song`.

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
- Unsafe includes and source escapes are refused.
- The destination is refused if it already exists, has a parent that is not a directory,
  overlaps a root the render store protects, contains a captured input, or overlaps `POSER.md`.
  A render store can export only if its `protected_roots()` declares its roots.

### Not covered

- The configuration and plugin and preset binaries are not bundled; the recipient supplies them.
- The manifest records configuration and `uv.lock` digests (or their absence), labeled by role
  for the source and configuration roots. They are a record, not a verification of the Python
  environment that ran export.
- Export does not sandbox Python or detect undeclared imports or other external inputs; declare
  local source dependencies with `--include`.
- Export renders the current source and never republishes an earlier render. To deliver an
  earlier version, restore its source and export again.
