# poser

You write a song as code, in Python or YAML. poser renders each track through a real VST3
instrument via [pedalboard](https://github.com/spotify/pedalboard), mixes the stems, and writes a
JSON report and spectrograms, so that an agent, or a person, can revise the song.

## Requirements

- macOS
- Python 3.13 or later
- [uv](https://docs.astral.sh/uv/)
- At least one VST3 instrument

The scaffolded workspace and its guide are written for Xfer Serum, a commercial synth. Other
instruments need a `plugins.toml` entry and work through their default patch or `save-sound`.

## Start a workspace

```bash
uv init --bare --python 3.13 my-studio && cd my-studio
uv add git+https://github.com/isnbh0/poser
uv run init-workspace
# edit plugins.toml, then:
uv run check-plugins
uv run render-song songs/demo.yaml
uv run analyze-song songs/demo.yaml
```

`init-workspace` writes `plugins.toml`, a demo song, and the composing guide as `AGENTS.md`, with
a `CLAUDE.md` that points to it, so coding agents working in the workspace read the guide. The other
commands find the workspace by walking up from the working directory to the nearest
`plugins.toml`. `render-song` and `analyze-song` write under its `renders/` directory.

## Commands

- `uv run init-workspace`: write the workspace scaffold into a directory.
- `uv run check-plugins`: check that each plugin in `plugins.toml` loads and makes sound.
- `uv run list-sounds`: list the plugin defaults, saved sounds, and installed presets a song can
  use.
- `uv run render-song <song>`: render one aligned WAV stem per track.
- `uv run analyze-song <song>`: mix the stems and write the report and spectrograms.
- `uv run save-sound <name>`: open a plugin's window and save its state as a sound when the window
  closes. It is for a person at the screen, not an agent.

## Developing poser

`make check` runs format, lint, types, and the tests that need no plugins. `make test-real` loads
real plugins; it reads a `plugins.toml` at the repository root, so copy
`src/poser/scaffold/plugins.toml` there and edit its paths first. [AGENTS.md](AGENTS.md) describes
the layout and rules.

## License

poser is licensed under GPL-3.0-or-later; see [LICENSE](LICENSE). poser depends on pedalboard,
which is GPL-3.0. The instruments you load keep their own licenses.
