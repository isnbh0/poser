# Surge XT reference setup

This reference setup uses the free Surge XT synthesizer on macOS. To create a studio and verify
it, follow the [shared setup guide](../../../../docs/setup.md); this page covers Surge-specific
prerequisites and choices.

## Install or locate Surge XT

An existing installation works. To install, download the macOS Intel/ARM Universal release from
the official [Surge XT downloads page](https://surge-synthesizer.github.io/downloads/) and run
the packaged installer, selecting VST3 and factory patches as needed. The installer installs for
all users; obtain appropriate permission before downloading or installing software. See the
[Surge XT manual](https://surge-synthesizer.github.io/manual-xt/) for installation details.

The initializer searches for the VST3 bundle in this order:

1. `/Library/Audio/Plug-Ins/VST3/Surge XT.vst3`
2. `~/Library/Audio/Plug-Ins/VST3/Surge XT.vst3`

Factory patches are optional and searched independently, first at
`/Library/Application Support/Surge XT/patches_factory`, then at
`~/Library/Application Support/Surge XT/patches_factory`.

For a bundle in another location, run this from your studio directory, replacing the path:

```bash
uv run init-workspace --setup surge --plugin-path '/path/to/Surge XT.vst3'
```

For factory patches in another location, add `--presets-path '/path/to/patches_factory'` to the
initialization command. Explicit paths must be existing directories. If no patches are found and
none are specified, initialization continues without a preset source.

## Demo sounds

The demo's lead and bass both use `surge`, the instrument's default patch. With a configured
preset source, songs can also name a patch as `surge:presets/<path without suffix>`. Use
`uv run list-sounds` from the studio to discover available sounds.

## Verify

Initialization only checks that the bundle exists. To verify that Surge loads and sounds, run
`uv run check-instruments`, then render and analyze the demo as described in the shared guide.
