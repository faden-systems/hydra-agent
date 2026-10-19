# The dry-run of a loop's checks (founder standing rule 2026-10-19, 1792422361)

Before a coder launches or the manager starts a build, the spec's checks run once for real on main with no product.

    tools/factory/loop_dry_run.sh <id> --repo <faden or hydra-agent checkout> [--pr <spec PR>] [--replay <evidence dir>]...

1. A temporary worktree of origin/main carries a no-op candidate: one commit adding the notes stub `docs/notes/<id>.md`
   (every `## heading` the exit names). `CI=true BASE_REF=<main> bash loops/<id>.exit.sh` then runs the fence, the
   function fence, the frozen files, the amended-ids rule, the notes and the base checks for real. Verdicts:
   - `STOPPED_AT_PRODUCT`: the exit failed at its first product-dependent check (a FAIL line matching `--expect`,
     default: the "unchanged / missing / own tests are missing / does not call" wordings). The dry-run passes.
   - `HARNESS_DEFECT`: any other FAIL, a crash, or the 1800 s cap. Fix the spec PR and rerun.
   - `NO_PRODUCT_GATE`: the exit passed with no product. The spec checks nothing of its own; fix it.
2. Every `--replay <dir>` runs `python loops/<id>.acceptance.py --replay <dir>`: the driver's parsers and counters over
   recorded evidence (chains, journey files, events.jsonl, records) from earlier runs under `artifacts/`; checks only,
   no server, no model. A driver whose source lacks the `--replay` flag is reported as `no --replay mode` and never
   started (its live sections would run). Verdict `REPLAY_FAILED` unless `--replay-optional` (loops written before the
   rule).
3. Output: `<out>/dry-run.log`, `<out>/exit.log`, `<out>/replay-*.log`, `<out>/dry-run.json` (verdict, the stopping
   line, the replays, the sha256 of the three spec files). A copy lands at `$HYDRA_HOME/coder/<id>-dry-run.json`.
   With `--pr`, the verdict and the exit log's tail are posted on the spec PR under `## Dry-run` before its last
   review round.
4. The launch gate: `tools/factory/dry_run_gate.sh <id> <checkout at the spec commit>` exits non-zero unless a recorded
   dry-run exists with verdict `STOPPED_AT_PRODUCT` and the three spec files' sha256 still match (an amendment after
   the dry-run needs a new one). Every `coder/<id>-launch.sh` and every manager build calls it first.

## The `--replay` convention for acceptance scripts

An acceptance script written from now on handles `--replay <dir>` at the top of `main()`: it loads the recorded files
of an earlier run from `<dir>` (its own layout: `real-<j>.json`, `real-<j>-chain.jsonl`, `summary.json`, `events.jsonl`,
the records) and runs every check function it would run on a live run against them, printing one line per check and
exiting 0 when all hold. It starts no server and calls no model. The checks are the same functions the live path uses,
so a parse error, a field-shape or nesting mistake, a word match or a count defect shows up on last night's evidence
before the first build.
