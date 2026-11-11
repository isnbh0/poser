# Publication verification evidence

This record supports the [publication checklist](publication-checks.md); it is not another setup
procedure. A passing technical check is not listening evidence or a claim about every preset.
States are **pass**, **fail**, **pending**, and **not tested**. An inconclusive observation stays
pending with its reason. Checks apply to their named source commit and environment, not future
versions.

## Scope and environment

Verification on 2026-11-12 used macOS 26.7.1 arm64, Python 3.13.3, uv 0.13.6, poser 0.1.0,
Pedalboard 0.9.26, and an existing user-local Surge XT 1.3.4 VST3 installation with factory patches.
No synthesizer was installed, no repository visibility changed, and no audio was played or uploaded.

The source baseline was `7670aab`; reference corrections are in `64d0c7f`. Runtime rendering
code was unchanged. The scheduling tests and tempo reference are committed with this record.
Separate installation entries below identify their own tested candidate.

## Reference and scheduling checks

- **Pass:** `make check` after reference corrections and scheduling tests: 1,592 passed,
  one case-insensitive-filesystem skip, 24 real-backend tests deselected.
- **Pass:** generated Python examples load as `Song` or `Graph` with advice on and off.
- **Pass:** the rendering bus test checks terminal gain, unchanged outgoing audio/cache after an
  intermediate mix-fader change, and changed downstream audio/cache after a wire-gain change.
- **Pass:** performance tests show proportional beat/BPM scaling preserves absolute event times,
  while fixed beats move with BPM.
- Existing `test_render_fresh_renders_the_new_instance_without_a_reset` checks the actual worker
  invocation and MIDI messages; it was reused rather than duplicated.

## Plugin tempo boundary

Code inspection establishes that poser converts song BPM into sample-timed notes/controllers and
passes no song BPM or host transport to the plugin. The operational adapter guide now states this
in both advice modes. This does not identify a plugin's fallback tempo or imply all rhythmic
presets fail.

Primary references checked during this verification:

- [Surge tempo sync](https://surge-synthesizer.github.io/manual-xt/#tempo-sync) describes parameters
  synchronized to host tempo.
- [Pedalboard processing API](https://spotify.github.io/pedalboard/reference/pedalboard.html#pedalboard.ExternalPlugin.process)
  describes timestamped MIDI and processing arguments; poser's worker does not supply host tempo.

### Real-preset characterization

**Pass (bounded observation):** nine forced renders succeeded, three per probe. The six
same-absolute-time renders showed a strongest sustained-envelope recurrence at 2.000 seconds.
This characterizes one preset and route; stronger causal attribution remains **pending**.

Discovered ID: `surge:presets/Sequences/Gate Chord`. Factory FXP SHA-256:
`7215a7e734b9f6abc0660d21de3799cd31dbeea3d8ab850774815ae52b11fb36`.
Its embedded XML has `a_lfo0_rate=3`, `temposync=1`, shape 7 (step sequencer), and a nonzero
sequence trigger mask. Source 17 routes to amplitude/filter envelope decay. Inspection against
[Surge 1.3.4 source](https://github.com/surge-synthesizer/surge/tree/release_xt_1.3.4/src/common)
(`ModulationSource.h`, `dsp/modulators/LFOModulationSource.cpp`, and `dsp/SurgeVoice.cpp`)
connects this active sequencer to envelope retriggering. Synced delays also exist in the patch;
the resulting envelope is not an isolated LFO measurement.

Create each probe using this source, substituting the constants from the table:

```python
from poser import Song, Track, notes

BPM, BEAT, LENGTH = 60, 1, 8


def build() -> Song:
    return Song(bpm=BPM, tracks=(Track(
        name="probe",
        sound="surge:presets/Sequences/Gate Chord",
        notes=notes([60], BEAT, LENGTH, 100),
    ),))
```

| Source under `songs/` | BPM | BEAT | LENGTH | Scheduled on/off (seconds) |
|---|---:|---:|---:|---|
| `absolute60.py` | 60 | 1 | 8 | 1 / 9 |
| `absolute120.py` | 120 | 2 | 16 | 1 / 9 |
| `fixed120.py` | 120 | 1 | 8 | 0.5 / 4.5 |

Run `uv run render-song songs/absolute60.py --force --json`, likewise for the other two sources,
three times each. Copy each stem immediately to a distinct review filename before the next run.
This run used the source checkout's environment from a scratch studio (`uv --project`), not a
freshly installed distribution. Core performance inspection confirmed equal event arrays for the
first pair: note on at sample 48,000, off at 432,000, total 528,000. The third ends at 312,000
samples including the fixed tail. Sample rate is 48,000 Hz.

Measurement: decode each stereo WAV as float64; compute stereo RMS in nonoverlapping 240-sample
(5 ms) windows. Mean-center the envelope in [2, 8) seconds for the equal-event probes, then
compute Pearson correlation of shifted traces at integer window lags 20 through 599. Select
the largest local correlation maximum (strictly greater than the previous lag and at least the
next lag). These interior windows exclude authored entrances and release tails.

| Probe | Strongest lag, repeats 1/2/3 | Correlation, repeats 1/2/3 |
|---|---|---|
| absolute60 | 2.000 / 2.000 / 2.000 s | .4812 / .6160 / .7767 |
| absolute120 | 2.000 / 2.000 / 2.000 s | .3832 / .7519 / .4315 |

The fixed-beat probe uses [1.5, 4) seconds and lags below half that window: too short to test a
2-second recurrence with this method. Its short-lag maxima varied, so no internal tempo-tracking
conclusion follows. Its first envelope bin above 1% of peak was at 0.5025 s, versus 1.0025 s
for every equal-event render, consistent with changed authored entrances.

The equal-event pair delivers identical relevant inputs, not two different supplied host tempos.
Unchanged recurrence does not identify fallback BPM or the responsible component. Resolution is
5 ms, not a confidence interval; the measurement window contains only three pattern cycles.
All nine audio hashes differ. No exact-waveform equality, uniquely isolated sequencer causality,
other-preset behavior, or listening is claimed. A tempo-capable-host positive control would be
needed for stronger claims, not for documenting poser's missing tempo input.

Retained measurements, per-run JSON, envelope traces, source snapshots, audio, and hashes are in
`.local/publication-verification/tempo-evidence/`. Representative audio SHA-256 values:

- absolute60 repeat 1: `07a0f7cfe8856a46c4c74d24f9081aad462ef6ad4c86037404a043bc924a2869`
- absolute120 repeat 1: `44473ad0f28c1d0437a05ff522073a6198957a0b093c151eed3599972b896ca4`
- fixed120 repeat 1: `84ea8de3ecf48cad28cf9f59a3a381d752d88ed0e69c5679fe820a5d58ea4616`

## Representative delivery and listening

**Pass (technical export); pending (listening):** a fresh export of the tracked
[Surge showcase source](../src/poser/reference_setups/surge/showcase.py) at `64d0c7f` is retained for review.
The piece has an opening, layered middle, and closing return. Its discovered presets are Digibass,
Past Tense, and Flute 1. This selection is not a test of a confirmed tempo-synchronized pulse.

Copied the source to `songs/release-listen.py` in the scratch studio, confirmed all three names
with `list-sounds`, and ran `uv run export-song songs/release-listen.py --destination deliveries/release-listen --json`
through the checkout environment. Export succeeded with no mix defects; every manifest artifact
hash was independently checked. Stereo 48 kHz float WAV duration is 37.27777 seconds, mix peak
-14.3032 dBFS, integrated loudness -26.3897 LUFS. No claim of mastering approval follows.

- Source SHA-256: `d96eeff449dd83087f8be37b55974c4b283a8feb8097b7c6d3445f5f7c48b7bd`
- Mix SHA-256: `e92dae68a286a010e5a7a5590c3ffe57a18244367787f33baf51b07a70006dee`
- Manifest SHA-256: `9872bbb361cd27e27f0c6821a5699cbaf159a86488de564299c76b1b92488e66`
- Local retained bundle: `.local/publication-verification/release-listen/`.
- Listener and timing/ending observations: **pending; not auditioned**.

Automated analysis cannot establish correct internal rhythm or an intact release. Rendering adds
only two seconds after the last authored event. Actual full-piece listening, including entrances
and ending, remains required. Later listening testimony must identify the delivered mix hash;
the export manifest's `listening: "not_auditioned"` field remains unchanged.

Exact review artifacts are retained locally outside Git under `.local/publication-verification/`
in the verification checkout, not published. This is a local handoff location, not a portable
public download. Owner confirmation of retention through review and listener observations remain
pending. If artifacts are lost, any later record must disclose that exact audio is unavailable;
source and hashes cannot reconstruct nondeterministic output.

## Installation checks

### Clean distributions

**Pending:** wheel and source archive checks must name their tested source commit, artifact hashes,
independent clean environments, and results. The distribution smoke's empty plugin fixture tests
packaging only; it is not audio acceptance.

### Anonymous public Git route

**Pending:** an unauthenticated direct request to `https://api.github.com/repos/isnbh0/poser`
returned HTTP 404 on 2026-11-12. This establishes no public access through that request, not the
reason for the response. The public Git installation and subsequent studio workflow were not run.
They remain a post-publication owner-controlled check. A local Git install or an authenticated
request cannot substitute for it.

### Fresh Surge installation

**Not tested:** verification used an existing installation. Do not describe this as a successful
fresh synth installation. Use a disposable environment and obtain permission for the official
installer if that additional claim is needed.

[uv's locking documentation](https://docs.astral.sh/uv/concepts/projects/sync/#upgrading-locked-package-versions)
confirms that the resolved Git commit is retained by the lockfile until upgrade. The existing
Git dependency instructions and lockfile policy need no mandatory revision-pinning change.
