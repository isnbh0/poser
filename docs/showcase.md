# Optional Surge showcase

[Download the audio preview (MP3)](audio/surge-showcase.mp3) ·
[Editable song source](../src/poser/reference_setups/surge/showcase.py) ·
[Preview provenance](audio/surge-showcase.json)

This 16-bar miniature has a sparse bass opening, flute entry, layered middle, and closing return.
The preview is **not auditioned**: rendering and analysis passed, but no listener has evaluated
this exact audio. It is a technical example, not an approved finished-song showcase.

This page is optional creative material, not a setup prerequisite or an advice-free composing
input. The packaged eight-beat `demo.yaml` remains the default-patch smoke test. Neither its
spectrogram nor this preview's analysis establishes musical quality.

## Instrument and reproduction

The preview uses an existing Surge XT **1.3.4** macOS VST3 installation and these discovered
factory IDs:

- `surge:presets/Basses/Digibass`
- `surge:presets/Polysynths/Past Tense`
- `surge:presets/Winds/Flute 1`

This is not fresh-install evidence or a claim that every factory preset works. On your studio,
confirm the names with `uv run list-sounds --instrument surge --json`. Copy the linked source to
`songs/showcase.py`, then run:

```bash
uv run export-song songs/showcase.py --destination deliveries/showcase
```

The destination must be new. The export contains the matching source, WAV, report, images, and
manifest. Plugin and preset binaries are not distributed, and different instrument versions or
renders need not produce identical audio.

The MP3 is a lossy preview, not the export bundle. Its provenance records the source and MP3
hashes, instrument version, presets, encoding command, and listening status.
