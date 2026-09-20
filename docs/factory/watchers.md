# Watchers - standing rules for Hermes (2026-09-13)

This file is the authoritative standing rule for the background watchers Hermes runs on the machine that has the
Codex login (currently the iMac). It is read at boot and whenever a watcher's behaviour is in question.

## Spec review (M3)

Start `python3 tools/factory/scripts/spec-watch.sh (which runs spec_watch.py --loop 300 with the environment set inside the script; never run spec_watch.py with an inline PYTHONPATH=, it trips the command scanner) --repo faden-systems/faden` detached at boot, next to the
buildlog watcher (`scripts/launch-loop.sh`'s `notify()`), same as any other long-running factory process:

```
python3 -c "import subprocess; subprocess.Popen(['python3','tools/factory/spec_watch.py','--loop','300'],
  start_new_session=True)"
```

It polls every 300 seconds, finds open PRs whose changed files include one or more `loops/<id>.md`, and reviews
every matching spec through `tools/factory/spec_review.py` (docs/factory/spec-review.md is the rule it enforces:
at most four rounds per PR, `review: ready` after a clean round, `review: capped` after the fourth). It posts one
aggregated comment per PR and one line to #faden-relay. Nothing else.

Two prohibitions, load-bearing, never relaxed:

- The watcher never launches a loop from a review. A review is advisory input for the human (or the steering
  conversation) deciding whether a spec is ready; the watcher only posts findings.
- The watcher never merges a PR, and never edits a spec. It reads a spec's exact head revision and comments; it does
  not touch the spec's content, its exit script, or the PR's merge state.

If the watcher's process dies, restart it the same way; it is stateless between runs (the round count and the
`review: <sha7>` dedup live entirely in the PR's own comments and labels on GitHub).
