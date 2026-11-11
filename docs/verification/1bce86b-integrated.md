# Integrated publication-readiness and piano-roll verification

Candidate: `1bce86bdf5aba7746b336ff0646588746a97f731` in the poser repository.
This merge combines publication-readiness `706f0f9` with piano-roll `63880c0`.
The command-registration conflict was resolved by retaining both `export-song` and `piano-roll`.

## Automated checks

- Combined `make check`: format, lint, and types passed; **1598 passed, 1 skipped,
  24 real-backend tests deselected**. The skip is a case-sensitive filename fixture on a
  case-insensitive volume.
- Additional explicit assertions that advice-off guides retain the piano-roll safety reference:
  `uv run pytest tests/test_guidance_modes.py -q` — **19 passed**, with Ruff format/lint passing.
- Combined `make test-real`: **24 passed, 1599 deselected**, using the existing installed
  instruments, including real scsynth tests. This is not fresh-install or listening evidence.
- Installed `uv run piano-roll --help` and `uv run export-song --help` both succeeded.
- Independent initializer review closed metadata-preservation and prerequisite-namespace findings;
  closure check: **46 passed**. Independent delivery review closed the absent-guide destination
  finding; closure check: **61 passed**.
- Piano-roll fixture was visually inspected at 1600 CSS pixels in headless Chrome before the
  merge; names and labels were unclipped, panels distinguishable, sparse notes and duration
  differences visible. Renderer code is unchanged by integration.

## Evidence limits

This is local integration evidence, not a release announcement. Fresh installation, anonymous
public acquisition, actual audition, and public preview publication remain unverified. Existing
installation/showcase evidence and those external release blockers are recorded in
[1de53b1-working-tree.md](1de53b1-working-tree.md); its earlier working-tree counts are not the
combined-candidate counts above. No listening or musical-quality claim is made.
