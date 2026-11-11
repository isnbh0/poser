# Surge XT reference setup

Install the free [Surge XT synthesizer](https://surge-synthesizer.github.io/downloads/) using its official macOS installer. Installation may require permission; an assisting agent should obtain permission before downloading or installing software.

The VST3 bundle is searched first at `/Library/Audio/Plug-Ins/VST3/Surge XT.vst3`, then at `~/Library/Audio/Plug-Ins/VST3/Surge XT.vst3`. Factory patches are searched independently at `/Library/Application Support/Surge XT/patches_factory`, then `~/Library/Application Support/Surge XT/patches_factory`. Presets are optional; the reference demo uses the default patch.

For a custom installation, run `uv run init-workspace --setup surge --plugin-path '/path/to/Surge XT.vst3'` and optionally add `--presets-path '/path/to/patches_factory'`. Then run `uv run check-instruments` to test that the instrument loads and sounds. Finding a bundle alone does not verify audio.
