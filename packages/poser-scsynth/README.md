# poser-scsynth

A poser instrument adapter that renders sounds through SuperCollider's `scsynth` server in
non-realtime mode. A workspace selects it by import path in `poser.toml`:

```toml
[instruments.sc]
adapter = "poser_scsynth.instrument:create"
```

The adapter ships no plugins: every sound is a synth definition plus a JSON sidecar, and one
`scsynth -N` subprocess per render is the isolation. `uv sync` at the repository root installs
both workspace packages. For wheel installation, build with `uv build --all-packages --wheel`
and install both local wheels together: the `poser` name on PyPI belongs to a different
project. This adapter requires this repository's poser 0.1.0, not that unrelated package.
The repo's tests cover the adapter; real-engine tests run with `make test-real`.
