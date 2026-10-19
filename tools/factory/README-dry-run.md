# The dry-run of a loop's checks (founder standing rule 2026-10-19, 1792422361)

Before a coder launches or the manager starts a build, the spec's checks run once for real on main with no product.

    tools/factory/loop_dry_run.sh <id> --repo <faden or hydra-agent checkout> --spec-ref <spec branch> [--pr <spec PR>] [--replay <evidence dir>]...

`--spec-ref` names the spec PR's branch: the three `loops/<id>.*` files of that branch (`origin/<branch>` after the fetch;
a local ref only when no such remote branch exists) are committed on top of origin/main (the exit's base), then the no-op
candidate. With `--pr` the ref must resolve to the PR's head (else exit 2); `--pr` alone takes the PR's head as the spec.
Without either the spec is read from origin/main (a spec already merged). The exit of a spec under review is a branch's
shell script, so it runs in the same sandbox as the replays (credentials removed, proxies dead): its pre-product checks
need only git, python and the local tree. That is a mitigation, not isolation: the script can still read files and open
direct connections. The VM has no unprivileged user namespaces, bubblewrap, firejail or sudo, so nothing stronger is
possible there today; a host that allows `unshare -n` adds it to the exit's invocation. What protects the host is that
spec PRs are the manager's own and the exit script is reviewed in the same PR before the dry-run runs.

1. A temporary worktree of origin/main carries a no-op candidate: one commit adding the notes stub `docs/notes/<id>.md`
   (every `## heading` the exit names). `CI=true BASE_REF=<main> bash loops/<id>.exit.sh` then runs the fence, the
   function fence, the frozen files, the amended-ids rule, the notes and the base checks for real. Verdicts:
   - `STOPPED_AT_PRODUCT`: the exit failed at its first product-dependent check: exactly one FAIL line, matching
     `--expect`, the last tagged line of the log, nothing crash-like after it, rc 1. The dry-run passes. The default
     `--expect` is narrow on purpose: `<file> is unchanged`, `tests <glob> are missing`, `does not call|pass|receive`,
     `still calls`, `has no <symbol>` (one word, or `<name> class|method|function`); a notes, base, fence, amended-ids
     or cut-short wording never matches. A spec whose product check says it differently passes its wording in
     `--expect`, and the spec PR's dry-run comment shows which line stopped the exit.
   - `HARNESS_DEFECT`: any other FAIL, a crash, or the 1800 s cap. Fix the spec PR and rerun.
   - `NO_PRODUCT_GATE`: the exit passed with no product. The spec checks nothing of its own; fix it.
2. Every `--replay <dir>` runs `python loops/<id>.acceptance.py --replay <dir>`: the driver's parsers and counters over
   recorded evidence (chains, journey files, events.jsonl, records) from earlier runs under `artifacts/`; checks only,
   no server, no model. A driver whose code does not handle the flag (`tools/factory/replay_mode_check.py`: an
   `add_argument("--replay")` call or a comparison against the literal `'--replay'`; comments, docstrings and help
   text do not count) is reported as `no --replay mode` and never started. No static check proves that a `--replay`
   skips the live sections, so every replay runs sandboxed: every credential-looking variable (`*KEY*`, `*TOKEN*`,
   `*SECRET*`, `*PASSWORD*`, `*CREDENTIAL*`) removed, `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` pointed at a dead local
   port with `no_proxy` empty, `DRY_RUN_REPLAY=1`, a 600 s cap: a driver that does start live work fails on its first
   connection (a model call, its own server) instead of running it, and the replay is recorded as failed. Verdict `REPLAY_FAILED` unless `--replay-optional` (loops written before the
   rule).
3. Output: `<out>/dry-run.log`, `<out>/exit.log`, `<out>/replay-*.log`, `<out>/dry-run.json` (verdict, the stopping
   line, the replays, the sha256 of the three spec files). A copy lands at `$HYDRA_HOME/coder/<id>-dry-run.json`.
   With `--pr`, the verdict and the exit log's tail are posted on the spec PR under `## Dry-run` before its last
   review round; the record says `posted: true`. A failed comment leaves no accepted record (the gate's copy is
   removed, exit 1): rerun with `--pr` once `gh` works.
4. The launch gate: `tools/factory/dry_run_gate.sh <id> <checkout at the spec commit>` exits non-zero unless a recorded
   dry-run exists with verdict `STOPPED_AT_PRODUCT`, posted on the spec PR (`DRY_RUN_GATE_NO_PR=1` is the explicit
   exception for a spec without a PR, and it prints), and the record hashes exactly the three spec files and each
   sha256 still matches (an amendment after the dry-run needs a new one). It is on the launch path: `tools/factory/coder_launch.sh <id> <main sha> <spec sha>`
   (the one launcher for every loop from rs1 on; it replaces the per-loop `coder/<id>-launch.sh` copies) clones, checks
   the frozen spec files against the spec commit, then runs the gate and removes the clone and the logs dir on refusal (exit 6, the gate's text kept as `coder/<id>-<main>-gate-refused-<stamp>.txt`, so a retry after the dry-run starts clean) before
   the account check and the coder start. The gate's output is kept at `coder/<id>-<main>-logs/dry-run-gate.txt` with
   a copy of the dry-run record. `--dry` stops after the gate (a launch rehearsal). A manager-run build has no script;
   the manager runs the gate by hand before its first build command.

## The `--replay` convention for acceptance scripts

An acceptance script written from now on handles `--replay <dir>` at the top of `main()`: it loads the recorded files
of an earlier run from `<dir>` (its own layout: `real-<j>.json`, `real-<j>-chain.jsonl`, `summary.json`, `events.jsonl`,
the records) and runs every check function it would run on a live run against them, printing one line per check and
exiting 0 when all hold. It starts no server and calls no model. The checks are the same functions the live path uses,
so a parse error, a field-shape or nesting mistake, a word match or a count defect shows up on last night's evidence
before the first build.
