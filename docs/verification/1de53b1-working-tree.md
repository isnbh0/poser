# Publication-readiness working-tree candidate

Candidate: uncommitted publication-readiness changes based on
`1de53b12bac09ed21be7e3a118f326a1f96bc8be`. This is not an immutable published candidate identifier.
Replace/extend this record with the final candidate revision and artifact references before release.
No public acquisition, fresh installation, listening, or publication claim is made here.
Existing-install rendering evidence is recorded separately below.

## Local evidence boundary

Inspected the repository's existing README, setup/bootstrap/publication documentation and packaged
Surge demo. The demo uses default `surge` sounds, not named factory presets. The existing
`docs/demo-spectrogram.png` does not establish accessible audio or listening. No existing verification
record or showcase source was found in the checkout at inspection.

Read-only local file searches found neither `/Library/Audio/Plug-Ins/VST3/Surge XT.vst3` nor
`/Library/Application Support/Surge XT` as a directory. Custom installations were not exhaustively
searched initially. Subsequently, with authorization to render using the existing installation,
the ignored root configuration identified user-library paths. `~/Library/Audio/Plug-Ins/VST3/Surge
XT.vst3/Contents/Info.plist` records version **1.3.4**; factory content is under
`~/Library/Application Support/Surge XT/patches_factory`. Filtered listing found
`surge:presets/Basses/Digibass`, `surge:presets/Polysynths/Past Tense`, and
`surge:presets/Winds/Flute 1` (all three listing commands exited 0). No installation, playback,
upload, visibility change, or publication was attempted. Network acquisition of this uncommitted
candidate was not attempted.

| Check | Status | Evidence / reason | Next action |
|---|---|---|---|
| Focused sound-filter/CLI tests | pass | 12 passed, 62 deselected; command below | Repeat on final candidate |
| Working-tree `make check` | pass | Format, lint, types; 1532 passed, 1 case-insensitive-volume skip, 24 real-plugin tests deselected | Repeat after final integration |
| Authority and fresh environment | blocked | No authorized fresh-install/listening environment established | Obtain authority and disposable macOS environment |
| Anonymous public candidate acquisition | blocked | Candidate is an uncommitted working tree | After authorized publication verify exact candidate and docs via public workflow |
| Fresh Surge install and smoke demo | blocked | No fresh installation performed | Follow shared setup with permission; record OS/uv/Python/Surge versions |
| Named-preset composition on existing install | pass | Three discovered factory presets on Surge 1.3.4; render/analyze exited 0 | Repeat on fresh release environment |
| Exported bundle hashes and relocation | pass | Seven hashes and relocated schema-owned paths verified; source matches packaged file | Recheck final candidate and subsequent-render immunity |
| Actual audition | blocked | No audio/listener evidence; not auditioned | Record actual authorized listener and exact artifact |
| Setup-only completion | not run | Documentation updated; no live scenario exercised | Verify setup-only ends without creative questionnaire |
| Already-requested composition | not run | Documentation specifies direct continuation | Exercise scenario without duplicate authorization |
| Advice-free generated context | pass | Automated fresh-off inventory, on/off/on regeneration, owner-file preservation, and cache/render invariance tests | Record actual model context separately for each trial |
| Packaged showcase and public-access preview | blocked | Packaged source renders locally; no public preview upload authorized | Provide authorized public preview with provenance |
| Auditioned showcase | blocked | No listener; not auditioned | Audition exact exported preview with permission |

## Existing-install showcase result

Disposable studio: `/tmp/poser-showcase-ETgIJ3`. It selected only the existing Surge instrument,
not the other instruments/effects in the owner's root config. Commands from the poser checkout:

```bash
uv run render-song /tmp/poser-showcase-ETgIJ3/showcase.py --config /tmp/poser-showcase-ETgIJ3/poser.toml
uv run analyze-song /tmp/poser-showcase-ETgIJ3/showcase.py --config /tmp/poser-showcase-ETgIJ3/poser.toml --json
uv run export-song /tmp/poser-showcase-ETgIJ3/showcase.py --config /tmp/poser-showcase-ETgIJ3/poser.toml --destination /tmp/poser-showcase-ETgIJ3/delivery --json
```

All exited 0. Export reported `published`, analysis run 2. A source-only formatting adjustment
between initial analysis and export was captured in the exported source, which was independently
compared byte-for-byte with the packaged `showcase.py` after export.

Retained bundle (outside temporary storage):
`>~/local/poser-verification/c9922f5-showcase/`.
Local preview: `audio/mix.wav`; source: `source/showcase.py`; measurements: `report.json`;
provenance: `manifest.json`. These are machine-local artifact references, not publicly accessible
URLs. No plugin/preset binaries were copied. The source imports only poser's public song classes.

- Source SHA-256: `d96eeff449dd83087f8be37b55974c4b283a8feb8097b7c6d3445f5f7c48b7bd`.
- Audio SHA-256: `8ae8039144f0fa87152d7770290ffc83b9095bb99ee206aad316a0a57a943e96`.
- Manifest SHA-256: `1e3357ffe312448900f8d8fcadc6c744befdc86ea7f7db3cc2a625bc22fc4694`.
- All seven manifest artifact hashes independently verified after copying the bundle; all
  schema-owned mix/image paths resolved in the relocated copy.
- Bass, pulse, and melody statuses: `analyzed`; node and mix defects: empty.
- Mix peak: -14.59 dBFS; integrated loudness: -26.40 LUFS; clipped samples: 0.
- Listening: **not auditioned**. No judgment of preset suitability or musical quality is implied.
- The disposable studio had no `POSER.md` or `uv.lock`; manifest guidance is unavailable, configured
  advice true, with lockfile absence recorded. This is not advice-ablation evidence.
- Factory-content provenance is the existing local installation, not a recorded fresh download;
  installer provenance, environment reproducibility, and final published revision remain unverified.

## Focused automated result

`uv run pytest tests/test_sound_filters.py tests/test_cli.py -k 'list or filter' -q`
exited 0: **12 passed, 62 deselected**. Targeted Ruff lint and format checks on
`src/poser/cli/sounds.py` and `tests/test_sound_filters.py` also exited 0.
A broader CLI run during concurrent implementation was not clean (initialization/guide integration
errors); it is not full-candidate acceptance and must be rerun after integration.

## Integrated local checks

- `make check`: exit 0; **1532 passed, 1 skipped, 24 deselected**. The skip is the existing
  case-sensitive filename fixture on this case-insensitive volume. Format, lint, and types passed.
- `make test-real`: exit 0; **24 passed** using the already-installed instruments. This does not
  establish fresh installation, anonymous acquisition, or listening.
- Independently rechecked all seven retained showcase artifact hashes, schema-owned report paths,
  and exact correspondence between bundled and packaged source.
- Independent guidance review found no functional isolation blocker. Initializer review found a
  metadata-edit recovery issue; mode/mtime ownership checks and ordinary/process-death regression
  tests now cover it, and independent re-review passed. Delivery review found that an absent
  `POSER.md` could be used as the destination; both lexical and resolved guide-path overlaps are
  now rejected before song execution. Four shared/distinct-root regressions and independent
  re-review passed; no remaining review findings.
- Trial-copy sanitization is an operator procedure, not an automated owner-file rewriter. Actual
  model exposure and subjective distinctions between owner intent and coaching remain recorded
  trial boundaries.

These results concern this working tree, before piano-roll integration; repeat the combined checks
on the landed revision. No audio playback was started.

## Release decision

External release acceptance is blocked. Plugin-free implementation checks cannot satisfy the public,
fresh-install, named-preset or audition rows. A guessed preset or invented preview URL is not evidence.
Do not describe section 6's accessible public-preview or audition acceptance as completed.
