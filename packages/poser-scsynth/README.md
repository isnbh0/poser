# poser-scsynth

A poser instrument adapter that renders sounds through SuperCollider's `scsynth` server in
non-realtime mode. Sounds are synth definitions with JSON sidecars in a source folder, and each
render runs in its own `scsynth -N` subprocess. See the [adapter guide](src/poser_scsynth/guide.md)
for sounds and configuration.

## Install SuperCollider and configure the instrument

Install [SuperCollider](https://supercollider.github.io/). On macOS the engine binary is inside the
app bundle at `/Applications/SuperCollider.app/Contents/Resources/scsynth`; Homebrew and Linux
installs put `scsynth` on `PATH`, where `command -v scsynth` prints its location. A studio selects
the adapter by import path in `poser.toml` and names the binary with the `scsynth` option:

```toml
[instruments.sc]
adapter = "poser_scsynth.instrument:create"
scsynth = "/Applications/SuperCollider.app/Contents/Resources/scsynth"
```

On Windows, the installer puts the engine in a folder named for its version, such as
`C:\Program Files\SuperCollider-3.14.1\scsynth.exe`; in PowerShell,
`Get-ChildItem "$env:ProgramFiles\SuperCollider-*\scsynth.exe"` lists the installed engines. Write
a Windows path in a TOML literal string, in single quotes, so its backslashes stay as they are:

```toml
[instruments.sc]
adapter = "poser_scsynth.instrument:create"
scsynth = 'C:\Program Files\SuperCollider-3.14.1\scsynth.exe'
```

Use an absolute path; poser does not search `PATH`. On Windows, each SuperCollider version installs
into its own folder, so after an upgrade the configured path names the old engine, or nothing once
the old version is uninstalled. Point `scsynth` at the new engine and run
`uv run check-instruments`, which reports a missing engine as `scsynth binary <path> is missing`.
Sounds the old engine rendered stay cached until `uv run render-song <song> --force`.

## Add the adapter to a studio

A studio created by [the setup guide](../../docs/setup.md) depends on poser from Git. From the
studio directory, add the adapter from the same repository:

```bash
uv add "git+https://github.com/isnbh0/poser#subdirectory=packages/poser-scsynth"
```

In PowerShell:

```powershell
uv add "git+https://github.com/isnbh0/poser#subdirectory=packages/poser-scsynth"
```

Then select it in `poser.toml` by import path, as the examples above show. The adapter comes from
the same commit as poser, so upgrade the two together, instead of the single-package upgrade in
[upgrading](../../docs/upgrading.md):

```bash
uv lock --upgrade-package poser --upgrade-package poser-scsynth &&
uv sync
```

In PowerShell, run these lines in order and stop at the first that fails:

```powershell
uv lock --upgrade-package poser --upgrade-package poser-scsynth
uv sync
```

Then continue with the rest of the upgrade steps in [upgrading](../../docs/upgrading.md).

### Write a sound

A sound is a pair of files: `<name>.scsyndef`, a compiled SynthDef, and `<name>.json`, its sidecar
(`{"cc": {}}` when no controller is mapped). Write the `.scsyndef` from the SuperCollider IDE or
`sclang`, which the SuperCollider installer ships on macOS and Windows; `sclang` accepts forward
slashes in Windows paths. This writes `pad.scsyndef` into the named folder:

```supercollider
SynthDef(\pad, { |out = 0, freq = 440, gate = 1, amp = 0.2|
    var env = EnvGen.kr(Env.adsr(0.01, 0.2, 0.7, 0.5), gate, doneAction: 2);
    Out.ar(out, (Saw.ar(freq) * env * amp).dup);
}).writeDefFile("path/to/sounds/sc");
```

## Install the adapter package for poser contributors

`uv sync` at the repository root installs both workspace packages. For wheel installation, build
with `uv build --all-packages --wheel` and install both local wheels together; the `poser` package
on PyPI is an unrelated project, and this adapter requires this repository's poser 0.1.0.

## Tests

The repository's tests cover the adapter; real-engine tests are marked `real_scsynth` and run with
`make test-real` (without `make`, as on Windows: `uv run pytest -m real_scsynth`).
