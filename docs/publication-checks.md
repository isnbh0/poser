# Before publishing

See [verification evidence](publication-evidence.md) for tested versions, observations, and
outstanding checks. A pending check is not a pass; run independent packaging checks even when
listening or public access is unavailable.

1. Run `make check`. CI also installs the wheel and source distribution into clean environments
   and exercises commands and packaged resources. Its empty plugin fixture does not test audio.
2. Follow the [studio setup guide](setup.md) in a new studio using the public Git dependency,
   without GitHub credentials or an existing poser cache. Do this after publication if the repo
   is still private; a local checkout cannot test public access.
3. With Surge XT installed, run `uv run check-instruments`, render and analyze the demo, then
   make a short song using a preset discovered by `uv run list-sounds`. A pass with
   `REPEATABLE no` is valid. If trying a fresh Surge installation, use a fresh environment rather
   than uninstalling someone's working synth.
4. Run `uv run export-song <song> --destination <new-directory>` and open the delivered source,
   report, and audio. Listen if possible; otherwise label the result **not auditioned**.

Fix failures and say what you actually tried. An existing-plugin test is useful without being a
fresh-install test; a successful analysis is not a listening test. Obtain permission for software
installation, playback, and publication where needed.

The [showcase](showcase.md) is an optional example. Controlled advice experiments have their own
[ablation procedure](ablation.md), not a release requirement.
