# Optional Surge showcase

[Download the audio preview (MP3)](audio/surge-showcase.mp3) ·
[Editable song source](../src/poser/reference_setups/surge/showcase.py)

This 16-bar miniature has a sparse bass opening, flute entry, layered middle, and closing return.
The preview is **not auditioned**. The packaged eight-beat `demo.yaml` is a separate,
default-patch smoke test.

## Instrument and reproduction

The preview uses an existing Surge XT **1.3.4** macOS VST3 installation and these discovered
factory IDs:

- `surge:presets/Basses/Digibass`
- `surge:presets/Polysynths/Past Tense`
- `surge:presets/Winds/Flute 1`

In your studio, confirm the names with `uv run list-sounds --instrument surge --json`.
Copy the linked source to `songs/showcase.py`, then run:

```bash
uv run export-song songs/showcase.py --destination deliveries/showcase
```

The destination must be new. The export contains the matching source, WAV, report, images, and
manifest. Plugin and preset binaries are not distributed, and different instrument versions or
renders need not produce identical audio.
