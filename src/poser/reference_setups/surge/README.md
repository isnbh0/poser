# Surge XT reference setup

This reference setup uses the free Surge XT synthesizer and finds it where its installers put it
on macOS and Windows; native Windows itself is not supported yet (see the setup guide's
[Prerequisites](../../../../docs/setup.md#prerequisites)). To create a studio and verify it,
follow the [shared setup guide](../../../../docs/setup.md); this page covers Surge-specific
prerequisites and choices.

## Install or locate Surge XT

An existing installation works. To install, download a release from the official
[Surge XT downloads page](https://surge-synthesizer.github.io/downloads/). See the
[Surge XT manual](https://surge-synthesizer.github.io/manual-xt/) for installation details.

### macOS

Download the macOS Intel/ARM Universal release and run the packaged installer, selecting VST3
and factory patches as needed. The installer installs for all users, which needs administrator
approval.

The initializer searches for the VST3 bundle in this order:

1. `/Library/Audio/Plug-Ins/VST3/Surge XT.vst3`
2. `~/Library/Audio/Plug-Ins/VST3/Surge XT.vst3`

Factory patches are searched independently, first at
`/Library/Application Support/Surge XT/patches_factory`, then at
`~/Library/Application Support/Surge XT/patches_factory`.

### Windows

Download the Windows 64-bit installer and run it. It installs for all users, which needs
administrator approval, with the plugin under `%CommonProgramFiles%` and the factory patches in
`%ProgramData%\Surge XT`.

The initializer searches for the VST3 bundle in this order:

1. `%CommonProgramFiles%\VST3\Surge Synth Team\Surge XT.vst3`
2. `%LOCALAPPDATA%\Programs\Common\VST3\Surge Synth Team\Surge XT.vst3`

Factory patches come from the `patches_factory` of the data folder Surge XT itself uses for that
bundle, which is the first of these that applies:

1. The folder that `SURGE_DATA_HOME` names.
2. A `SurgeXTData` folder in the folder holding the plugin's binary,
   `Surge XT.vst3\Contents\x86_64-win`, or in a folder above it.
3. `%ProgramData%\Surge XT`, when it exists.
4. `%LOCALAPPDATA%\Surge XT`.

The portable zip keeps `Surge XT.vst3` beside `SurgeXTData`. Extract it anywhere and pass the
bundle with `--plugin-path`; the initializer finds the patches beside it.

### Other locations

For a bundle in another location, run this from your studio directory, replacing the path:

```bash
uv run init-workspace --setup surge --plugin-path '/path/to/Surge XT.vst3'
```

In PowerShell, the same single quotes keep the path literal:

```powershell
uv run init-workspace --setup surge --plugin-path 'D:\Plugins\Surge XT.vst3'
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
