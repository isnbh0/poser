# poser-scsynth

A poser instrument adapter that renders sounds through SuperCollider's `scsynth` server in
non-realtime mode. A workspace selects it by import path in `poser.toml`:

```toml
[instruments.sc]
adapter = "poser_scsynth.instrument:create"
```

The adapter ships no plugins: every sound is a synth definition plus a JSON sidecar, and one
`scsynth -N` subprocess per render is the isolation. The repo's tests cover it; this member is
built as part of the root workspace.
