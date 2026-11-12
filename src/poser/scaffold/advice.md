## Composing here

When the owner delegates a choice, make it and state the default you picked.
Choose sounds by track role. Plan a composition against the brief, then write a new song,
render and analyze it. Read the report, spectrograms, and history; revise against actual results.
Use `humanize` on lines with uniform velocities or grid-exact timing.
Invite the person to audition and give feedback.

## Judging a revision

Compare a revision against an earlier record, and treat a band or bar difference under 1 dB as
possible render noise. Use `run` and `song_sha256` to tell versions apart, and `node_key` and
`sound_key` to see which nodes changed between records. Compare relative band levels between
nodes and versions. To clear `headroom`, lower track `gain` for a `Song`, or the appropriate
leaf-node `gain_db` for a `Graph`.
For `upstream_failed`, fix the named upstream and render again; for `stale_stem`, render again.
