# Publication checks

This maintainer procedure complements the single [studio setup guide](setup.md); it is not a
second installation guide or a record of completed verification. Local automated acceptance and
external release acceptance are separate. Run the plugin-free targeted tests and `make check`
for the implementation; neither proves public acquisition, a fresh installation, or listening.
Stage new documentation before the tracked-file onboarding link tests.

## Authority and environment

Use an authorized fresh macOS machine, VM with usable audio, or disposable environment whose
relevant prerequisites are documented. A **fresh Surge install** has no pre-existing Surge
plugin or factory content. A new studio or user account on a machine with a system-wide install
is not fresh. Do not uninstall a working synth or delete someone's studio to obtain freshness.

Obtain permission for network downloads, system-wide installation, GUI actions, and playback as
needed. Never bypass OS security or license prompts. Installing prerequisites and creating
disposable test directories are explicit acceptance actions, not automated-test side effects.
If the environment or permission is unavailable, mark affected checks `blocked` and hand them
to an authorized maintainer.

## A. Anonymous public acquisition

In an environment without GitHub login, tokens, credential helpers, private mirrors, or a
pre-existing poser cache, verify that these pages are publicly readable:

- https://github.com/isnbh0/poser
- https://github.com/isnbh0/poser/blob/main/docs/bootstrap.md
- https://github.com/isnbh0/poser/blob/main/docs/setup.md
- https://github.com/isnbh0/poser/blob/main/src/poser/reference_setups/surge/README.md

Run this probe in that clean environment:

```bash
GIT_TERMINAL_PROMPT=0 git -c credential.helper= ls-remote https://github.com/isnbh0/poser HEAD
```

Expect exit 0 and a commit ID without authentication. This probe alone is insufficient. Follow
the shared setup guide's `uv init`, `cd`, and public `uv add` steps with a new uv cache or
`UV_NO_CACHE=1` for this verification. Do not substitute `--project` pointing at a local checkout,
an editable path dependency, a private URL, or the unrelated PyPI namesake. Inspect the generated
studio's `uv.lock` for the resolved public Git URL and commit; verify the installed command entry
points using their help. Record commands, exit codes, and the public documentation revision followed.

Require the installed commit to equal the candidate commit being accepted, and verify the followed
documentation is that candidate's documentation. The unqualified Git URL and `main` pages can
advance independently; recording their values or a successful probe does not establish equality.
If code or documentation differs from the candidate, mark candidate-specific acceptance `blocked`
or repeat the public workflow for that candidate. Do not silently replace the advertised workflow
with a pinned install command.

Authentication failures, inaccessible URLs, and dependency-resolution failures block this claim.
Retain the actual diagnostics rather than assuming a code defect or private repository. If the
implementation is not yet published, an older public revision cannot verify this candidate:
record the difference and rerun after authorized publication. Publishing or changing repository
visibility requires separate authority and is not required by this documentation change.

## B. Fresh free-instrument installation

Follow the official download and installer links in the
[Surge reference guide](../src/poser/reference_setups/surge/README.md) in the fresh environment.
Record the downloaded Surge version, provenance, and installer choices. Install VST3 and factory
patches for this full acceptance path and verify their actual locations. Run the remaining shared
setup chain. Finding a bundle directory does not prove it loads.

Expect initialization, instrument checking, demo rendering, and analysis each to exit 0.
`repeatable: false` does not invalidate success. Retain diagnostics for genuine failures and use
the shared guide's non-destructive recovery. An existing installation can provide an additional
compatibility result but cannot replace this fresh-install check.

## C. First-song composition and named preset

Exercise the optional continuation with a short brief and a new source in the generated studio's
`songs/` directory. Its filename and musical choices are test inputs, not repository fixtures.
Run `uv run list-sounds`, record an actual `surge:presets/...` name from the fresh factory
installation, and use it in at least one track. Do not invent a name or require an undocumented
preset observed elsewhere. Render and analyze with the shared guide's commands.

Require nonempty audio, analyzed nodes, no node or mix defects, report key `nodes`, and an existing
mix at the reported path. Record which preset worked without extrapolating to all factory presets.
If patches are absent, the default-sound demo may pass while named-preset acceptance is `blocked`.

## D. Audition and honest delivery

Follow the shared guide's final-handoff procedure: preserve the editable source and verify it
remains unchanged across a successful render followed by successful analysis. Inspect that
analysis's report and reported mix path. Copy or deliver the matching source, report, and WAV
together before another run replaces outputs; include Python source dependencies where needed.
Unchanged source bytes do not establish reproducibility for external-state-dependent scripts.
`song_sha256` hashes the record writer's serialized loaded song, not necessarily the source file;
do not compare it to a raw source-file hash. A historical report can still point to mutable
`latest` audio, so it alone cannot establish the matching audio artifact.

Deliver the source and WAV with report/spectrogram locations, analysis interpretation, and caveats.
When attachments are unavailable, give accessible paths and disclose mutable `latest` locations.
If analysis fails, report that failure; do not present a stale earlier WAV as the current successful
song. Analysis success is not artistic approval or a mastering guarantee.

Have a listener audition the delivered version when possible; record who or what actually listened
and the outcome. Neither launching playback nor inspecting a spectrogram counts as listening.
Without a listening route, technical rendering may pass but audition stays `not run` or `blocked`;
label the delivery **not auditioned**. Do not block a technically verified draft solely for that
reason. Also exercise a setup-only scenario: declining composition must end with setup results,
not unwanted creative questions or a forced song.

## Verification record

Create a release verification record with date, candidate and tested revisions, public documentation
revision, macOS version/architecture, uv/Python/Surge versions, installation provenance, plugin
location, and selected preset when relevant. Include commands and exit codes, retained artifact
locations, listening status, and listener/outcome if auditioned. Redact personal information before
publishing evidence. Use only `pass`, `fail`, `blocked`, or `not run`; each row needs evidence or a
reason and a next action. The template below makes no successful claims.

| Check | Status | Evidence / reason | Next action |
|---|---|---|---|
| Local plugin-free tests and `make check` | not run | No result recorded | Run and retain output |
| Authority and fresh environment | not run | No environment recorded | Confirm authority and prerequisites |
| A. Anonymous acquisition of candidate | not run | No public result recorded | Verify public workflow and revision equality |
| B. Fresh Surge installation and demo | not run | No fresh-install result recorded | Install with permission and follow setup |
| C. First song and named preset | not run | No preset/song result recorded | Compose, render, analyze, retain artifacts |
| D. Matching source/report/WAV delivery | not run | No handoff recorded | Preserve and identify matching artifacts |
| D. Actual audition | not run | No listener recorded | Audition with permission or disclose limitation |
| D. Setup-only scenario | not run | No scenario recorded | Verify declining composition is complete |

## Release decision

The documentation/test change can be implemented and locally accepted while external checks are
blocked. Do not declare the public/fresh-install journey verified until A, B, and C pass for the
published candidate. Audition has its own status: a technical-only claim must explicitly disclose
that listening was not performed; a listened-through claim requires D's actual audition. Do not
automate installer approval or fabricate evidence to satisfy this checklist.
