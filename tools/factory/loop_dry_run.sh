#!/usr/bin/env bash
# The dry-run of a loop's checks before its build (founder standing rule 2026-10-19, 1792422361):
#
#   tools/factory/loop_dry_run.sh <id> [--repo <checkout>] [--pr <number>] [--replay <evidence-dir>]... [--out <dir>]
#                                      [--expect <regex>] [--replay-optional]
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
ID="${1:?loop id}"; shift
REPO="$PWD"; PR=""; OUT=""; EXPECT=""; OPTIONAL=0; REPLAYS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;; --pr) PR="$2"; shift 2;; --out) OUT="$2"; shift 2;; --expect) EXPECT="$2"; shift 2;;
    --replay) REPLAYS+=("$2"); shift 2;; --replay-optional) OPTIONAL=1; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
DEFAULT_EXPECT='missing|unchanged|does not (call|report|show|name|pass|receive|take|descend)|still calls|no (own|single|direct)|not recorded|are missing|must be merged|has no|is not a|does not exist'
EXPECT="${EXPECT:-$DEFAULT_EXPECT}"
cd "$REPO" || exit 2
git fetch -q origin || { echo "fetch failed in $REPO" >&2; exit 2; }
MAIN="$(git rev-parse origin/main)"
for f in loops/$ID.md loops/$ID.exit.sh loops/$ID.acceptance.py; do
  git cat-file -e "$MAIN:$f" 2>/dev/null || { echo "spec file missing on origin/main: $f" >&2; exit 2; }
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
say "[dry-run] $ID on origin/main $MAIN; no-op candidate $(git rev-parse --short HEAD); out $OUT"
say "[dry-run] exit: CI=true BASE_REF=$MAIN bash loops/$ID.exit.sh"
PY="${PYTHON:-$(command -v python3)}"
( CI=true BASE_REF="$MAIN" PYTHON="$PY" timeout -k 10 1800 bash "loops/$ID.exit.sh" ) > "$OUT/exit.log" 2>&1; RC=$?
sed "s/^/  | /" "$OUT/exit.log" | tail -n 80 >> "$LOG"
FAIL_LINE="$(grep -E "^\[$ID\] FAIL:" "$OUT/exit.log" | tail -n 1)"
LAST_STEP="$(grep -E "^\[$ID\] " "$OUT/exit.log" | grep -vE "FAIL:|PASS" | tail -n 1)"
if [ $RC -eq 0 ]; then VERDICT="NO_PRODUCT_GATE"; STOP="the exit passed with no product"
elif [ -n "$FAIL_LINE" ] && echo "$FAIL_LINE" | grep -Eq "$EXPECT"; then VERDICT="STOPPED_AT_PRODUCT"; STOP="$FAIL_LINE"
elif [ $RC -eq 124 ]; then VERDICT="HARNESS_DEFECT"; STOP="the exit did not finish in 1800 s (last step: $LAST_STEP)"
else VERDICT="HARNESS_DEFECT"; STOP="${FAIL_LINE:-exit rc $RC with no FAIL line (last step: $LAST_STEP)}"; fi
say "[dry-run] exit rc=$RC verdict=$VERDICT"
say "[dry-run] stopped at: $STOP"
# the replays
REPLAY_JSON="[]"; RFAIL=0
for d in "${REPLAYS[@]:-}"; do
  [ -n "$d" ] || continue
  rl="$OUT/replay-$(basename "$d").log"
  if ! grep -q -- "--replay" "loops/$ID.acceptance.py"; then
    # never start a script that would run its live sections: a driver without the flag is reported, not run
    echo "loops/$ID.acceptance.py has no --replay mode (the flag does not appear in the script); not run" > "$rl"; rrc=3; mode="no --replay mode"
  else
    ( CI=true PYTHONPATH="$WT/fitflow-app:$WT/loops:$WT" timeout -k 10 600 "$PY" "loops/$ID.acceptance.py" --replay "$d" ) > "$rl" 2>&1; rrc=$?; mode="ran"
  fi
  [ $rrc -eq 0 ] || RFAIL=1
  say "[dry-run] replay $d: rc=$rrc ($mode); $(tail -n 1 "$rl")"
  sed "s/^/  | /" "$rl" | tail -n 30 >> "$LOG"
  REPLAY_JSON="$("$PY" -c 'import json,sys; a=json.loads(sys.argv[1]); a.append({"dir":sys.argv[2],"rc":int(sys.argv[3]),"mode":sys.argv[4],"log":sys.argv[5]}); print(json.dumps(a))' "$REPLAY_JSON" "$d" "$rrc" "$mode" "$rl")"
done
if [ $RFAIL -ne 0 ] && [ $OPTIONAL -eq 0 ]; then say "[dry-run] a replay failed: the acceptance driver's checks do not hold on existing evidence (or lack --replay)"; [ "$VERDICT" = STOPPED_AT_PRODUCT ] && VERDICT="REPLAY_FAILED"; fi
SPEC_SHA="$("$PY" -c 'import hashlib,sys,json; print(json.dumps({f: hashlib.sha256(open(f,"rb").read()).hexdigest() for f in sys.argv[1:]}))' "loops/$ID.md" "loops/$ID.exit.sh" "loops/$ID.acceptance.py")"
"$PY" - "$OUT/dry-run.json" "$ID" "$MAIN" "$VERDICT" "$STOP" "$RC" "$REPLAY_JSON" "$SPEC_SHA" "$STAMP" "$LOG" <<'PYJ'
import json, sys
o, i, m, v, s, rc, rj, ss, st, log = sys.argv[1:]
json.dump({"id": i, "main": m, "verdict": v, "stopped_at": s, "exit_rc": int(rc), "replays": json.loads(rj), "spec_sha256": json.loads(ss), "at": st, "log": log}, open(o, "w"), indent=1)
PYJ
mkdir -p "${HYDRA_HOME:-$HOME}/coder"; cp "$OUT/dry-run.json" "${HYDRA_HOME:-$HOME}/coder/$ID-dry-run.json"
if [ -n "$PR" ]; then
  REMOTE="$(git -C "$REPO" remote get-url origin | sed -E 's#.*github.com[:/]##; s#\.git$##')"
  { echo "## Dry-run (${MAIN:0:8}, $STAMP)"; echo; echo "verdict: **$VERDICT**"; echo; echo "stopped at: \`$STOP\`"; echo
    [ "$REPLAY_JSON" != "[]" ] && { echo "replays:"; "$PY" -c 'import json,sys; [print(f"- {r[\"dir\"]}: rc={r[\"rc\"]} ({r[\"mode\"]})") for r in json.loads(sys.argv[1])]' "$REPLAY_JSON"; echo; }
    echo '```'; tail -n 40 "$OUT/exit.log"; echo '```'; } | gh pr comment "$PR" --repo "$REMOTE" --body-file - >/dev/null && say "[dry-run] posted on PR #$PR"
fi
case "$VERDICT" in STOPPED_AT_PRODUCT) exit 0;; *) exit 1;; esac
