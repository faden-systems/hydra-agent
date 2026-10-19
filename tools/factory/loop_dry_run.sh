#!/usr/bin/env bash
# The dry-run of a loop's checks before its build (founder standing rule 2026-10-19, 1792422361):
#
#   tools/factory/loop_dry_run.sh <id> [--repo <checkout>] [--spec-ref <branch>] [--pr <number>] [--replay <evidence-dir>]...
#                                      [--out <dir>] [--expect <regex>] [--replay-optional]
# --spec-ref <branch>: the spec under review is not on main yet (the usual case): its three loops files are put on top of
# origin/main as the exit's base, then the no-op candidate; without it the spec is read from origin/main.
# (--repo, --out and --replay paths may be relative to the caller's directory; they are resolved before the worktree switch)
#
# 1. A temporary worktree of the checkout's origin/main; a no-op candidate = one commit adding the notes stub
#    docs/notes/<id>.md (every "## heading" the exit script names, nothing else); then
#    CI=true BASE_REF=<main sha> bash loops/<id>.exit.sh under a 1800 s cap. The fence, function fence, frozen files,
#    amended-ids, notes and base checks run for real. The exit may stop only at its first product-dependent check:
#    a FAIL line matching --expect (default: the product missing / unchanged / own tests missing wordings). Any other
#    FAIL is a harness defect: verdict HARNESS_DEFECT, exit 1. A full PASS with no product means the exit has no
#    product gate: verdict NO_PRODUCT_GATE, exit 1.
# 2. For every --replay <dir>: python loops/<id>.acceptance.py --replay <dir> (checks only, no server, no model) in the
#    temp worktree; its rc and output are recorded. A script without a --replay mode is a defect unless
#    --replay-optional (loops written before the rule).
# Output: <out>/dry-run.log, <out>/dry-run.json (id, main, verdict, stopped_at, replay results, the sha256 of the three
# spec files); a copy at $HYDRA_HOME/coder/<id>-dry-run.json for the launch gate (tools/factory/dry_run_gate.sh);
# with --pr, the verdict and the log tail are posted on the spec PR under "## Dry-run".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ID="${1:?loop id}"; shift
REPO="$PWD"; PR=""; OUT=""; EXPECT=""; OPTIONAL=0; REPLAYS=(); SPEC_REF=""
while [ $# -gt 0 ]; do
  case "$1" in
    # paths are made absolute here: the script changes into a temporary worktree before using them
    --repo) REPO="$(realpath -m "$2")"; shift 2;; --pr) PR="$2"; shift 2;; --out) OUT="$(realpath -m "$2")"; shift 2;;
    --expect) EXPECT="$2"; shift 2;; --replay) REPLAYS+=("$(realpath -m "$2")"); shift 2;; --replay-optional) OPTIONAL=1; shift;;
    --spec-ref) SPEC_REF="$2"; shift 2;;  # the spec PR's branch: its three loops files on top of main (the usual case: a spec not yet merged)
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
# the product-check wordings the exit scripts use for "the candidate has no product" (narrow on purpose: a notes, base,
# fence, amended-ids or cut-short wording never matches; a spec whose product check says it differently passes --expect)
DEFAULT_EXPECT='(is|are) unchanged|tests [^ ]+ are missing|does not (call|pass|receive) |still calls|has no (module-level|single|[A-Za-z_]+ (class|method|function)|[A-Za-z_]+$)'
EXPECT="${EXPECT:-$DEFAULT_EXPECT}"
cd "$REPO" || exit 2
git fetch -q origin || { echo "fetch failed in $REPO" >&2; exit 2; }
MAIN="$(git rev-parse origin/main)"
SPEC_AT="$MAIN"
if [ -n "$SPEC_REF" ]; then
  SPEC_AT="$(git rev-parse "$SPEC_REF" 2>/dev/null)" || SPEC_AT="$(git rev-parse "origin/$SPEC_REF" 2>/dev/null)" || { echo "spec ref not found: $SPEC_REF" >&2; exit 2; }
fi
for f in loops/$ID.md loops/$ID.exit.sh loops/$ID.acceptance.py; do
  git cat-file -e "$SPEC_AT:$f" 2>/dev/null || { echo "spec file missing at ${SPEC_REF:-origin/main} ($SPEC_AT): $f" >&2; exit 2; }
done
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUT:-${HYDRA_HOME:-$HOME}/artifacts/dry-run/$ID-${MAIN:0:8}-$STAMP}"; mkdir -p "$OUT"
LOG="$OUT/dry-run.log"; : > "$LOG"
say() { echo "$*" | tee -a "$LOG"; }
WT="$(mktemp -d /tmp/dry-run-$ID-XXXXXX)"; rmdir "$WT"
git worktree add -q --detach "$WT" "$MAIN" || exit 2
cleanup() { git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cd "$WT"
git config user.name hydra-dry-run; git config user.email dry-run@hydra.local
if [ "$SPEC_AT" != "$MAIN" ]; then
  # the spec under review on top of main: exactly its three loops files (the rest of the PR, if any, is not the spec)
  git checkout -q "$SPEC_AT" -- "loops/$ID.md" "loops/$ID.exit.sh" "loops/$ID.acceptance.py"
  git commit -q -m "dry-run: the $ID spec from $SPEC_REF ($SPEC_AT)" -- "loops/$ID.md" "loops/$ID.exit.sh" "loops/$ID.acceptance.py" 2>/dev/null || true
  BASE_FOR_EXIT="$(git rev-parse HEAD)"
else
  BASE_FOR_EXIT="$MAIN"
fi
# the notes stub: every "## heading" the exit script names, in the order it names them, plus the standard four
HEADS="$(grep -o '"## [A-Za-z][A-Za-z -]*"' "loops/$ID.exit.sh" | tr -d '"' | awk '!seen[$0]++')"
{
  echo "# $ID notes (dry-run stub, no product)"; echo
  for h in "## What changed" "## Measured" "## Open questions" "## Pre-existing tests amended"; do
    echo "$h"; echo; echo "dry-run: no product. candidate: $MAIN"; echo
  done
  printf '%s\n' "$HEADS" | while IFS= read -r h; do
    [ -n "$h" ] || continue
    case "$h" in "## What changed"|"## Measured"|"## Open questions"|"## Pre-existing tests amended") continue;; esac
    echo "$h"; echo; echo "dry-run: no product."; echo
  done
} > "docs/notes/$ID.md"
git add "docs/notes/$ID.md"; git commit -q -m "dry-run: no-op candidate for $ID (notes stub only)"
say "[dry-run] $ID on origin/main $MAIN${SPEC_REF:+ with the spec from $SPEC_REF ($SPEC_AT)}; no-op candidate $(git rev-parse --short HEAD); out $OUT"
say "[dry-run] exit: CI=true BASE_REF=$BASE_FOR_EXIT bash loops/$ID.exit.sh"
PY="${PYTHON:-$(command -v python3)}"
( CI=true BASE_REF="$BASE_FOR_EXIT" PYTHON="$PY" timeout -k 10 1800 bash "loops/$ID.exit.sh" ) > "$OUT/exit.log" 2>&1; RC=$?
sed "s/^/  | /" "$OUT/exit.log" | tail -n 80 >> "$LOG"
FAIL_LINE="$(grep -E "^\[$ID\] FAIL:" "$OUT/exit.log" | tail -n 1)"
FAIL_COUNT="$(grep -cE "^\[$ID\] FAIL:" "$OUT/exit.log")"
LAST_TAGGED="$(grep -E "^\[$ID\] " "$OUT/exit.log" | tail -n 1)"
LAST_STEP="$(grep -E "^\[$ID\] " "$OUT/exit.log" | grep -vE "FAIL:|PASS" | tail -n 1)"
# anything after the FAIL line that looks like a crash (a traceback, a shell error) makes the stop suspect
AFTER_FAIL="$(awk -v f="$FAIL_LINE" 'found && /Traceback|command not found|syntax error|No such file|Permission denied|^\[/ {print; n++} $0==f {found=1} END {}' "$OUT/exit.log" | head -n 3)"
# the cap is judged first: an exit that prints an expected FAIL line and then hangs is a harness defect, not a stop
if [ $RC -eq 124 ] || [ $RC -eq 137 ]; then VERDICT="HARNESS_DEFECT"; STOP="the exit did not finish in 1800 s (rc $RC; last step: $LAST_STEP; last FAIL line: ${FAIL_LINE:-none})"
elif [ $RC -eq 0 ]; then VERDICT="NO_PRODUCT_GATE"; STOP="the exit passed with no product"
elif [ -n "$FAIL_LINE" ] && echo "$FAIL_LINE" | grep -Eq "$EXPECT"; then
  # a product stop is exactly one FAIL line, the last tagged line of the log, nothing crash-like after it, rc 1
  if [ "$FAIL_COUNT" -ne 1 ]; then VERDICT="HARNESS_DEFECT"; STOP="$FAIL_COUNT FAIL lines (a product stop is exactly one); last: $FAIL_LINE"
  elif [ "$LAST_TAGGED" != "$FAIL_LINE" ]; then VERDICT="HARNESS_DEFECT"; STOP="the exit went on after its FAIL line (last tagged line: $LAST_TAGGED)"
  elif [ -n "$AFTER_FAIL" ]; then VERDICT="HARNESS_DEFECT"; STOP="crash-like output after the FAIL line: $(echo "$AFTER_FAIL" | head -n 1)"
  elif [ $RC -ne 1 ]; then VERDICT="HARNESS_DEFECT"; STOP="exit rc $RC after the FAIL line (an exit script fails with rc 1): $FAIL_LINE"
  else VERDICT="STOPPED_AT_PRODUCT"; STOP="$FAIL_LINE"; fi
else VERDICT="HARNESS_DEFECT"; STOP="${FAIL_LINE:-exit rc $RC with no FAIL line (last step: $LAST_STEP)}"; fi
say "[dry-run] exit rc=$RC verdict=$VERDICT"
say "[dry-run] stopped at: $STOP"
# the replays
REPLAY_JSON="[]"; RFAIL=0
for d in "${REPLAYS[@]:-}"; do
  [ -n "$d" ] || continue
  rl="$OUT/replay-$(basename "$d").log"
  if ! "$PY" "$HERE/replay_mode_check.py" "loops/$ID.acceptance.py" > "$rl" 2>&1; then
    # never start a script that would run its live sections: a driver whose code does not handle the flag (an AST
    # check: add_argument("--replay") or a comparison against '--replay'; comments and help text do not count) is
    # reported, not run
    echo "loops/$ID.acceptance.py has no --replay mode; not run" >> "$rl"; rrc=3; mode="no --replay mode"
  else
    # the replay sandbox: no static check can prove a driver's --replay skips its live sections, so the replay runs
    # with every credential-looking variable removed and HTTP(S)_PROXY pointed at a dead local port with no_proxy
    # empty: a driver that does start live work (a model call, its own server) fails on its first connection instead
    # of running it; the 600 s cap bounds the rest
    OUT_VAR="$(echo "$ID" | tr '[:lower:]' '[:upper:]')_OUT"  # the driver's own evidence variable: its replay output lands beside the dry-run
    ( env -u NO_PROXY -u no_proxy $(env | grep -iE '^[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*=' | cut -d= -f1 | sed 's/^/-u /') \
        "$OUT_VAR=$OUT/replay-$(basename "$d")" CI=true DRY_RUN_REPLAY=1 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 \
        PYTHONPATH="$WT/fitflow-app:$WT/loops:$WT" timeout -k 10 600 "$PY" "loops/$ID.acceptance.py" --replay "$d" ) > "$rl" 2>&1; rrc=$?; mode="ran (sandboxed: no credentials, dead proxy)"
  fi
  [ $rrc -eq 0 ] || RFAIL=1
  say "[dry-run] replay $d: rc=$rrc ($mode); $(tail -n 1 "$rl")"
  sed "s/^/  | /" "$rl" | tail -n 30 >> "$LOG"
  REPLAY_JSON="$("$PY" -c 'import json,sys; a=json.loads(sys.argv[1]); a.append({"dir":sys.argv[2],"rc":int(sys.argv[3]),"mode":sys.argv[4],"log":sys.argv[5]}); print(json.dumps(a))' "$REPLAY_JSON" "$d" "$rrc" "$mode" "$rl")"
done
if [ $RFAIL -ne 0 ] && [ $OPTIONAL -eq 0 ]; then say "[dry-run] a replay failed: the acceptance driver's checks do not hold on existing evidence (or lack --replay)"; [ "$VERDICT" = STOPPED_AT_PRODUCT ] && VERDICT="REPLAY_FAILED"; fi
SPEC_SHA="$("$PY" -c 'import hashlib,sys,json; print(json.dumps({f: hashlib.sha256(open(f,"rb").read()).hexdigest() for f in sys.argv[1:]}))' "loops/$ID.md" "loops/$ID.exit.sh" "loops/$ID.acceptance.py")"
"$PY" - "$OUT/dry-run.json" "$ID" "$MAIN" "$VERDICT" "$STOP" "$RC" "$REPLAY_JSON" "$SPEC_SHA" "$STAMP" "$LOG" "$PR" "${SPEC_REF:+$SPEC_REF@$SPEC_AT}" <<'PYJ'
import json, sys
o, i, m, v, s, rc, rj, ss, st, log, pr = sys.argv[1:12]
json.dump({"id": i, "main": m, "verdict": v, "stopped_at": s, "exit_rc": int(rc), "replays": json.loads(rj), "spec_sha256": json.loads(ss), "at": st, "log": log, "pr": (int(pr) if pr else None), "spec_ref": (sys.argv[12] if len(sys.argv) > 12 and sys.argv[12] else None)}, open(o, "w"), indent=1)
PYJ
POSTED=false
if [ -n "$PR" ]; then
  REMOTE="$(git -C "$REPO" remote get-url origin | sed -E 's#.*github.com[:/]##; s#\.git$##')"
  if { echo "## Dry-run (${MAIN:0:8}, $STAMP)"; echo; echo "verdict: **$VERDICT**"; echo; echo "stopped at: \`$STOP\`"; echo
    [ "$REPLAY_JSON" != "[]" ] && { echo "replays:"; "$PY" -c 'import json,sys; [print(f"- {r[\"dir\"]}: rc={r[\"rc\"]} ({r[\"mode\"]})") for r in json.loads(sys.argv[1])]' "$REPLAY_JSON"; echo; }
    echo '```'; tail -n 40 "$OUT/exit.log"; echo '```'; } | gh pr comment "$PR" --repo "$REMOTE" --body-file - >/dev/null
  then POSTED=true; say "[dry-run] posted on PR #$PR"
  else say "[dry-run] the PR comment on #$PR FAILED: the record is not accepted (no copy for the gate); rerun with --pr once gh works"; fi
fi
"$PY" - "$OUT/dry-run.json" "$POSTED" <<'PYP'
import json, sys
o, posted = sys.argv[1:]; j = json.load(open(o)); j["posted"] = (posted == "true"); json.dump(j, open(o, "w"), indent=1)
PYP
# the gate's copy is written only when the record is complete: a --pr run whose comment failed leaves no accepted record
if [ -z "$PR" ] || [ "$POSTED" = true ]; then
  mkdir -p "${HYDRA_HOME:-$HOME}/coder"; cp "$OUT/dry-run.json" "${HYDRA_HOME:-$HOME}/coder/$ID-dry-run.json"
else
  rm -f "${HYDRA_HOME:-$HOME}/coder/$ID-dry-run.json"; exit 1
fi
case "$VERDICT" in STOPPED_AT_PRODUCT) exit 0;; *) exit 1;; esac
