# Advice ablation

This procedure is for trial operators; composing models do not read it. The persistent
`[guidance] advice` boolean controls only whether generated `POSER.md` includes packaged optional
advice. API contracts, owner intent, permissions, defect reporting, truthful-delivery
requirements, rendering, cache identity, and analysis thresholds are identical in both modes.

## Prepare a paired trial

1. Prepare two fresh studios with identical poser revision, plugin and preset versions, brief,
   permissions, and technical reference. Before either composing model starts, initialize one
   with `uv run init-workspace --setup surge --advice on` and the other with `--advice off`.
   For existing studios, set `advice = true` or `false` under `[guidance]`, then run
   `uv run update-guide` and `uv run update-guide --check`.
2. Inventory the complete normal reading path: `AGENTS.md`, `CLAUDE.md`, `POSER.md`, `poser.toml`,
   the syntax demo, and everything those files require reading. Inspect configuration comments,
   instrument notes, owner instructions, and adapter guides for optional coaching. Advice-off
   generated context must contain none. An operator can complete setup so the advice-off model
   never reads guided onboarding, this procedure, or the showcase.
3. For an existing advice-bearing studio, make clean trial copies before starting either model.
   Never edit the original owner files. Preserve operational settings and owner intent, removing
   only identified optional coaching identically from both copies. `update-guide` leaves owner
   instructions and custom instrument notes untouched, so remove coaching from them by hand.
   Record the changes and input hashes. If owner intent and coaching cannot be distinguished,
   record that limitation.
4. Generate and check both guides, and inspect auxiliary context files for stale advice. Verify
   that regenerating on → off → on restores the on-mode guide and that a fresh advice-off
   initialization contains no advice. Give each model only its designated context and the same
   brief.
5. Start independent model sessions with no shared conversation memory or previous trial outputs.
   Do not change a model's guide after it has read one. Match tools, budget, and stop condition.

## Retain evidence

For each trial record:

- Model and provider identifier, available settings, explicit seeds where relevant, brief, and
  owner intent.
- Poser revision, mode, configuration and generated-guide raw SHA-256 digests.
- Actual context file inventory and digests, including required transitive reading and any
  browsing.
- Audited adapter, package, and guide identities and versions. Third-party adapter `guide` text
  appears in both modes and may contain coaching; audit it or record it as unaudited exposure.
- Tool availability, permissions, budget, stop condition, and plugin and preset provenance and
  versions.
- Editable outputs, declared dependencies, commands, diagnostics, successful exported bundles, and
  failed trial artifacts. Record listening status separately.

Advice-off leaves `advice.md` in the installed package. If the model could browse the repository
or received owner-provided coaching, label that exposure explicitly.

## Compare outcomes

Compare technical failures separately from listener-rated musical quality, and include failed
trials. Use the same listening procedure across paired outputs, and record who listened to which
output.
