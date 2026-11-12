## Composing here

When the owner delegates a choice, make it and state the default you picked.
Choose sounds by track role. Plan a composition against the brief, then write a new song,
render and analyze it. Read the report, spectrograms, and history; revise against actual results.
Use `humanize` on lines that sound mechanical.
Invite the person to audition and give feedback. If no one auditions, deliver the verified draft
marked not auditioned.

## Judging a revision

Compare a revision against an earlier record, and treat a band or bar difference under 1 dB as
possible render noise. Compare relative band levels between nodes and versions. Keep the mix peak
at or below -1 dBFS by lowering track `gain` for a `Song`, or the appropriate leaf-node `gain_db`
for a `Graph`.
For `upstream_failed`, fix the named upstream and render again; for `stale_stem`, render again.
