# poser-scsynth

A poser instrument adapter that renders sounds through SuperCollider's `scsynth` server in
non-realtime mode. Sounds are synth definitions with JSON sidecars in a source folder, and each
render runs in its own `scsynth -N` subprocess. See the [adapter guide](src/poser_scsynth/guide.md)
for sounds and configuration.

## Install and configure

Install [SuperCollider](https://supercollider.github.io/). On macOS the engine binary is inside the
app bundle at `/Applications/SuperCollider.app/Contents/Resources/scsynth`; Homebrew and Linux
installs put `scsynth` on `PATH`, where `command -v scsynth` prints its location. A studio selects
the adapter by import path in `poser.toml` and names the binary with the `scsynth` option:

```toml
[instruments.sc]
adapter = "poser_scsynth.instrument:create"
scsynth = "/Applications/SuperCollider.app/Contents/Resources/scsynth"
```

Use an absolute path; poser does not search `PATH`. Without a configured binary, sounds can be
listed but renders fail with `load_failed`.

## Install the package

`uv sync` at the repository root installs both workspace packages. For wheel installation, build
with `uv build --all-packages --wheel` and install both local wheels together; the `poser` package
on PyPI is an unrelated project, and this adapter requires this repository's poser 0.1.0.

## Tests

The repository's tests cover the adapter; real-engine tests are marked `real_scsynth` and run with
`make test-real`.
