# Surge showcase

[Download the audio preview (MP3)](audio/surge-showcase.mp3) ·
[Editable song source](../src/poser/reference_setups/surge/showcase.py)

This 16-bar miniature has a sparse bass opening, flute entry, layered middle, and closing return.
No one has listened to the preview.

## Instrument and reproduction

The preview was rendered with Surge XT 1.3.4 (macOS VST3) using these factory patches:

- `surge:presets/Basses/Digibass`
- `surge:presets/Polysynths/Past Tense`
- `surge:presets/Winds/Flute 1`

In your studio, confirm the names with `uv run list-sounds --instrument surge --json`.
Copy the linked source to `songs/showcase.py`, then run:

```bash
uv run export-song songs/showcase.py --destination deliveries/showcase
```

The destination must be new. The export contains the matching source, WAV, report, images, and
manifest. Neither this repository nor the export includes the Surge plugin or its patches.
Renders can differ slightly from the preview between runs and between Surge versions.
