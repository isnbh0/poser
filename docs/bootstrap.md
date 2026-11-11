# Agent bootstrap

Copy this prompt into your coding agent:

```text
Help me create and verify an independent poser studio. Follow the shared setup guide at
https://github.com/isnbh0/poser/blob/main/docs/setup.md.
Inspect the existing environment and project state first, and discuss consequential choices with
me. Resolve missing prerequisites using permissions already granted, or ask me before installing
software or taking other actions that need permission. Adapt the guide to the environment, recover
from failures without discarding my work, and report the verification results and any limitations.
After successful setup, continue directly if I already requested composition; otherwise offer
the shared guide's optional first-song composition, audition, and export-song handoff loop. Reuse my preferences, ask only for consequential missing intent, and
accept delegated choices with stated provisional defaults. Setup-only is a complete outcome;
if I decline composition, stop with setup results. Do not install software, start playback,
publish, or upload without appropriate authority. Disclose when you cannot listen; starting a
player is not auditioning. Respect the optional persistent advice setting independently of the
instrument setup. For a poser-verified song handoff, use export-song to a new destination,
declare local source dependencies with --include, disclose listening limitations, and never
present stale output after a failed attempt as the current successful song.
```
