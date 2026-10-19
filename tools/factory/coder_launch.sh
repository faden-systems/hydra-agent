#!/usr/bin/env bash
# The coder launcher (one script for every loop; replaces the per-loop coder/<id>-launch.sh copies):
#   tools/factory/coder_launch.sh <id> <full sha of main at launch> <spec commit sha> [--repo-url <url>] [--dry]
# The dry-run gate runs before anything else that costs (founder standing rule 2026-10-19, 1792422361): a spec without a
# current dry-run (tools/factory/dry_run_gate.sh) is refused, the clone removed, exit 6. Then, as before: the three spec
# files at <main> must equal the spec commit's; the frozen hashes, prompt and run script come from
# $HYDRA_HOME/coder/<id>-prompt.txt.tmpl, <id>-run.py.tmpl ({MERGE} = main sha), the one-call account check is
# $HYDRA_HOME/coder/<id>-l-check.py; the coder starts detached. --dry stops after the gate and the frozen check.
set -euo pipefail
ID="${1:?loop id}"; M="${2:?full sha of main}"; SPEC="${3:?spec commit sha}"; shift 3
URL="https://github.com/faden-systems/faden.git"; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in --repo-url) URL="$2"; shift 2;; --dry) DRY=1; shift;; *) echo "unknown argument: $1" >&2; exit 2;; esac
done
[[ "$M" =~ ^[0-9a-f]{40}$ ]] || { echo "need a full sha for main"; exit 2; }
[[ "$SPEC" =~ ^[0-9a-f]{7,40}$ ]] || { echo "need a sha for the spec commit"; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
C="${HYDRA_HOME:-$HOME}/coder"; cd "$C"; R="$C/$ID-$M"; L="$R-logs"
for f in "$C/$ID-prompt.txt.tmpl" "$C/$ID-run.py.tmpl" "$C/$ID-l-check.py"; do [ -s "$f" ] || { echo "missing: $f"; exit 2; }; done
[ -e "$R" ] && { echo "exists: $R"; exit 2; }; mkdir "$L"
git clone -q "$URL" "$R"; cd "$R"
git checkout -q -b "loop/$ID" "$M"; git config user.name hydra-coder; git config user.email coder@hydra.local
for f in "loops/$ID.md" "loops/$ID.exit.sh" "loops/$ID.acceptance.py"; do
  [ "$(git rev-parse "$M:$f")" = "$(git rev-parse "$SPEC:$f")" ] || { echo "FROZEN FILE DIFFERS FROM THE SPEC COMMIT: $f"; rm -rf "$R" "$L"; exit 3; }
done
# the gate: refuses a spec without a current dry-run (verdict STOPPED_AT_PRODUCT, the three spec files unchanged since)
if ! bash "$HERE/dry_run_gate.sh" "$ID" "$R" | tee "$L/dry-run-gate.txt"; then
  echo "LAUNCH REFUSED by the dry-run gate (see $L/dry-run-gate.txt); run tools/factory/loop_dry_run.sh $ID first"; rm -rf "$R"; exit 6
fi
cp "$C/$ID-dry-run.json" "$L/dry-run.json"
python3 - "$R" "$L" "$ID" <<'PY'
import sys,json,hashlib,pathlib
R=pathlib.Path(sys.argv[1]); L=pathlib.Path(sys.argv[2]); i=sys.argv[3]
files=[f'loops/{i}.md',f'loops/{i}.exit.sh',f'loops/{i}.acceptance.py']
(L/'frozen-hashes.json').write_text(json.dumps({f:hashlib.sha256((R/f).read_bytes()).hexdigest() for f in files},indent=2)+'\n')
PY
if [ $DRY -eq 1 ]; then echo "DRY: gate and frozen check passed for $ID at $M; not launching"; rm -rf "$R" "$L"; exit 0; fi
[ -d "$C/$ID-claude" ] || python3 "$C/new-claude-config.py" "$C/$ID-claude"
sed "s/{MERGE}/$M/g" "$C/$ID-prompt.txt.tmpl" > "$L/prompt.txt"
sed "s/{MERGE}/$M/g" "$C/$ID-run.py.tmpl" > "$C/$ID-$M-run.py"
python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$C/$ID-$M-run.py"
echo "account check (one real call on the L account)..."; python3 "$C/$ID-l-check.py" "$L/account-check.json" || { echo "L ACCOUNT CHECK FAILED"; exit 4; }
cd "$C"
nohup setsid python3 "$C/$ID-$M-run.py" > "$L/launcher.out" 2>&1 < /dev/null &
sleep 6; cat "$L/coder.json" 2>/dev/null || { echo "coder did not start"; tail -20 "$L/launcher.out"; exit 5; }
echo "LAUNCHED: repo $R logs $L"
