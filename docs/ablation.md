# Advice ablation

This is an evaluation procedure for trial operators, not required composing material. The persistent
`[guidance] advice` boolean selects optional poser advice in generated `POSER.md`; it does not
remove API contracts, owner intent, permissions, measured defects, or truthful-delivery requirements.
It does not change rendering, cache identity, or analysis thresholds.

## Prepare a paired trial

1. Prepare two fresh studios/environments with identical poser revision, plugin/preset versions,
   brief, permissions, and technical reference. Before either composing model starts, initialize
   one with `uv run init-workspace --setup surge --advice on` and the other with `--advice off`.
   Existing studios use `[guidance]` / `advice = true` or `false`, followed by
   `uv run update-guide` and `uv run update-guide --check`. There is no temporary guide override.
2. Inventory the complete normal reading path: `AGENTS.md`, `CLAUDE.md`, `POSER.md`, `poser.toml`,
   the syntax demo, and everything those files require reading. Inspect configuration comments,
   instrument notes, owner instructions, and adapter guides for optional coaching. Advice-off
   generated context must contain none. Setup can be completed by an operator separately so the
   off model need not read guided onboarding, this procedure, or the optional showcase.
3. For an existing advice-bearing studio, make clean trial copies before starting either model.
   Never edit the original owner files. Preserve operational settings and owner intent, removing
   only identified optional coaching identically from both copies. Record the changes and input
   hashes. If owner intent and coaching cannot be distinguished, record that limitation rather
   than asserting clean isolation. Poser does not automatically rewrite owner instructions or
   custom instrument notes.
4. Generate/check both guides and inspect auxiliary context files for stale advice. As an operator
   check, on → off → on regeneration should restore the selected content; fresh off initialization
   should never copy stale advice. Give each model only its designated context and the same brief.
5. Start independent model sessions with no shared conversation memory or previous trial outputs.
   Toggling a guide after exposure is not a clean ablation. Match tools and budget/stop condition.

## Retain evidence

For each trial record:

- Model/provider identifier, available settings, explicit seeds where relevant, brief and owner intent.
- Poser revision, mode, configuration and generated-guide raw SHA-256 digests.
- Actual context file inventory and digests, including required transitive reading and any browsing.
- Audited adapter/package/guide identities and versions; legacy third-party `guide` may contain
  coaching and is not automatically sanitized. Strict isolation covers built-ins and explicitly
  audited external content only.
- Tool availability, permissions, budget/stop condition, plugin and preset provenance/versions.
- Editable outputs, declared dependencies, commands, diagnostics, successful exported bundles, and
  failed trial artifacts. Record listening status separately; do not assert bitwise plugin determinism.

Export manifest guidance evidence is useful but insufficient: clean acceptance requires a current
observed guide and the separately recorded actual context inventory. Configured off with a stale
on-mode guide is not clean exposure. The package can still contain optional resources: generation
is not filesystem access control. If unrestricted repository browsing or owner-provided coaching
was allowed, label that exposure explicitly.

## Compare outcomes

Compare technical failures separately from listener-rated musical quality. Retain failed outputs
instead of removing inconvenient trials. Analysis without defects is not evidence that a listener
prefers the music, nor a mastering guarantee. Use the same listening procedure across paired
outputs and record who actually listened; opening a player is not audition evidence.
