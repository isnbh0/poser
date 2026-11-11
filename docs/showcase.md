# Optional Surge showcase

This page is opt-in creative material, not a setup prerequisite or an advice-free composing input.
The packaged eight-beat `demo.yaml` remains the default-patch smoke test. It is not a finished-song
showcase, and its spectrogram is not listening evidence.

## Candidate status: locally exported; public preview and audition blocked

The packaged [showcase.py](../src/poser/reference_setups/surge/showcase.py) is a 16-bar miniature:
sparse bass opening, flute entry, layered middle, and closing return. These exact factory IDs were
discovered using filtered `list-sounds --json` on an existing user-library Surge XT **1.3.4** install
(version read from the installed VST3's `Contents/Info.plist`):

- `surge:presets/Basses/Digibass`
- `surge:presets/Polysynths/Past Tense`
- `surge:presets/Winds/Flute 1`

This is existing-install evidence, not a fresh-install claim. No plugin was installed, playback
started, or audio uploaded. Render, analysis, and export succeeded in a disposable studio, with
all three nodes analyzed and no node/mix defects. A local preview is retained at
`>~/local/poser-verification/c9922f5-showcase/audio/mix.wav`, alongside editable source,
report, images, and manifest. This machine-local path is not a public-access preview. The
[candidate record](verification/1de53b1-working-tree.md) records hashes and limitations.
**Not auditioned.** Technical success is not musical approval; public preview and audition
acceptance remain blocked.

## Completion requirements

An authorized maintainer should repeat discovery with `list-sounds --instrument surge --json`
on the release environment and record its Surge version and factory-content provenance. Preserve
the editable source and explicitly declared local dependencies. Record the exact presets used without claiming
that all factory presets were tested; preset descriptions may merely be folder names.

Export it to a new bundle using `export-song`, verify manifest hashes and moved-bundle paths,
and provide an accessible audio preview with the source revision, bundle/artifact hashes, Surge
version, selected preset IDs, analysis outcome, and listening status. Upload/publication and
playback require separate permission. If no listener actually heard that exact version, label it
**not auditioned**; technical success cannot satisfy auditioned-showcase acceptance.

Do not distribute plugin or preset binaries. Keep this source separate from initialization outputs:
it must not be copied into fresh advice-free studios, embedded in required syntax examples, or
made required reading for off-mode composing models. Trial operators control exposure to this page.
