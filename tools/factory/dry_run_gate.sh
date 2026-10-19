#!/usr/bin/env bash
# The launch gate of the dry-run rule: a coder launch (or a manager build) refuses a spec without a current dry-run.
#   tools/factory/dry_run_gate.sh <id> <checkout with loops/<id>.* at the spec commit>
# Current = $HYDRA_HOME/coder/<id>-dry-run.json exists, its verdict is STOPPED_AT_PRODUCT, and the sha256 of the three
# spec files it recorded equal the checkout's (an amendment after the dry-run needs a new dry-run). Exit 0 when current.
set -uo pipefail
ID="${1:?loop id}"; DIR="${2:?checkout}"
M="${HYDRA_HOME:-$HOME}/coder/$ID-dry-run.json"
[ -s "$M" ] || { echo "DRY-RUN GATE: no dry-run recorded for $ID ($M missing); run tools/factory/loop_dry_run.sh $ID first"; exit 6; }
python3 - "$M" "$DIR" "$ID" <<'PY'
import hashlib, json, os, sys
m, d, i = sys.argv[1:]
j = json.load(open(m))
if j.get("verdict") != "STOPPED_AT_PRODUCT":
    raise SystemExit(f"DRY-RUN GATE: the recorded dry-run of {i} has verdict {j.get('verdict')} (stopped at: {j.get('stopped_at')}); fix the spec and rerun")
if not j.get("posted"):
    # the rule: the dry-run's results are a comment on the spec PR before its last review round; a record without
    # that comment refuses the launch unless the caller says the spec had no PR (DRY_RUN_GATE_NO_PR=1, printed)
    if os.environ.get("DRY_RUN_GATE_NO_PR") == "1":
        print(f"DRY-RUN GATE: {i}'s dry-run was not posted on a spec PR; allowed by DRY_RUN_GATE_NO_PR=1")
    else:
        raise SystemExit(f"DRY-RUN GATE: {i}'s dry-run was not posted on the spec PR (rerun tools/factory/loop_dry_run.sh {i} --pr <spec PR>; DRY_RUN_GATE_NO_PR=1 only for a spec without a PR)")
for f, h in j.get("spec_sha256", {}).items():
    now = hashlib.sha256(open(f"{d}/{f}", "rb").read()).hexdigest()
    if now != h:
        raise SystemExit(f"DRY-RUN GATE: {f} changed since the dry-run ({j.get('at')}); rerun tools/factory/loop_dry_run.sh {i}")
print(f"dry-run gate ok: {i} dry-run {j.get('at')} on main {j.get('main')[:8]} (stopped at the product step)")
PY
