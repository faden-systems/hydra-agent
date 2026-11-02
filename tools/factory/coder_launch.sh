#!/usr/bin/env bash
# The coder launcher (one script for every loop; replaces the per-loop coder/<id>-launch.sh copies):
#   tools/factory/coder_launch.sh <id> <full sha of the base commit> <spec commit sha> [--repo-url <url>] [--dry]
# The base commit is main at launch, or the loop's base branch (c0: the merged spec commit on loop/lw1-base); the three
# spec files must be at the base, equal to the spec commit's.
# The dry-run gate runs before anything else that costs (founder standing rule 2026-10-19, 1792422361): a spec without a
# current dry-run (tools/factory/dry_run_gate.sh) is refused, the clone removed, exit 6. Then, as before: the three spec
# files at <main> must equal the spec commit's; the frozen hashes, prompt and run script come from
# $HYDRA_HOME/coder/<id>-prompt.txt.tmpl, <id>-run.py.tmpl ({MERGE} = main sha), the one-call account check is
# $HYDRA_HOME/coder/r2d2-check.py (on the loop's own config dir $HYDRA_HOME/coder/<id>-claude); the coder starts detached.
# Adoption step A (founder brief 2026-11-01): every coder carries the protections. tools/factory/coder-settings.json,
# the two hooks and docs/CONSTITUTION.md must be at the base; the settings are installed as <clone>/.claude/settings.json,
# excluded from git locally (so an exit's "everything committed" gate ignores them). The deny list is the protected paths
# minus the loop's fence (FENCE= in loops/<id>.exit.sh), written with the fence to .claude/fence.json for the hook. Founder rule 1 (2026-10-22): every
# prompt carries the Stage 1 smoke block: $HYDRA_HOME/coder/<id>-smoke.txt (the loop's smoke command, one line) is
# substituted into $HYDRA_HOME/coder/stage1-smoke.txt and appended to the prompt; a loop without a smoke file is refused.
# --dry stops after the gate, the frozen check and the protections check (nothing is launched, the clone is removed).
set -euo pipefail
ID="${1:?loop id}"; M="${2:?full sha of the base commit}"; SPEC="${3:?spec commit sha}"; shift 3
URL="https://github.com/faden-systems/faden.git"; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in --repo-url) URL="$2"; shift 2;; --dry) DRY=1; shift;; *) echo "unknown argument: $1" >&2; exit 2;; esac
done
[[ "$M" =~ ^[0-9a-f]{40}$ ]] || { echo "need a full sha for the base commit"; exit 2; }
[[ "$SPEC" =~ ^[0-9a-f]{7,40}$ ]] || { echo "need a sha for the spec commit"; exit 2; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
C="${HYDRA_HOME:-$HOME}/coder"; cd "$C"; R="$C/$ID-$M"; L="$R-logs"
for f in "$C/$ID-prompt.txt.tmpl" "$C/$ID-run.py.tmpl" "$C/r2d2-check.py" "$C/$ID-smoke.txt" "$C/stage1-smoke.txt"; do [ -s "$f" ] || { echo "missing: $f"; exit 2; }; done
[ -e "$R" ] && { echo "exists: $R"; exit 2; }; mkdir "$L"
git clone -q "$URL" "$R"; cd "$R"
git checkout -q -b "loop/$ID" "$M"; git config user.name hydra-coder; git config user.email coder@hydra.local
for f in "loops/$ID.md" "loops/$ID.exit.sh" "loops/$ID.acceptance.py"; do
  [ "$(git rev-parse "$M:$f")" = "$(git rev-parse "$SPEC:$f")" ] || { echo "FROZEN FILE DIFFERS FROM THE SPEC COMMIT: $f"; rm -rf "$R" "$L"; exit 3; }
done
# the gate: refuses a spec without a current dry-run (verdict STOPPED_AT_PRODUCT, the three spec files unchanged since)
if ! bash "$HERE/dry_run_gate.sh" "$ID" "$R" | tee "$L/dry-run-gate.txt"; then
  # a refusal leaves nothing behind that a retry would trip on: the gate's text is kept beside the logs dirs
  REF="$C/$ID-$M-gate-refused-$(date -u +%Y%m%dT%H%M%SZ).txt"; mv "$L/dry-run-gate.txt" "$REF"; rm -rf "$R" "$L"
  echo "LAUNCH REFUSED by the dry-run gate (see $REF); run tools/factory/loop_dry_run.sh $ID first, then retry this launch"; exit 6
fi
cp "$C/$ID-dry-run.json" "$L/dry-run.json"
python3 - "$R" "$L" "$ID" <<'PY'
import sys,json,hashlib,pathlib
R=pathlib.Path(sys.argv[1]); L=pathlib.Path(sys.argv[2]); i=sys.argv[3]
files=[f'loops/{i}.md',f'loops/{i}.exit.sh',f'loops/{i}.acceptance.py']
(L/'frozen-hashes.json').write_text(json.dumps({f:hashlib.sha256((R/f).read_bytes()).hexdigest() for f in files},indent=2)+'\n')
PY
# the protections (adoption step A): the settings, the hooks and the constitution must be at the base; installed, excluded
for f in tools/factory/coder-settings.json tools/factory/coder_hooks/pre_tool_use.py tools/factory/coder_hooks/stop.sh docs/CONSTITUTION.md; do
  git cat-file -e "$M:$f" 2>/dev/null || { echo "MISSING AT THE BASE: $f (adoption step A must be on the base line before a coder launches)"; rm -rf "$R" "$L"; exit 3; }
done
mkdir -p .claude && cp tools/factory/coder-settings.json .claude/settings.json
grep -qx '.claude/' .git/info/exclude 2>/dev/null || echo '.claude/' >> .git/info/exclude
python3 -c "import json,sys; json.load(open('.claude/settings.json'))" || { echo "coder-settings.json at the base is not valid JSON"; rm -rf "$R" "$L"; exit 3; }
# The deny list is computed per loop: the protected paths minus the loop's fence (founder review of c0, 2026-11-02: a deny
# on a path the fence grants made the c0 coder write through one-off scripts). The fence is the exit's own FENCE= regex
# (loops/<id>.exit.sh, one line); it is written to .claude/fence.json for the PreToolUse hook (which exempts exactly the
# paths the regex matches), and every deny rule whose protected path is granted in part by the fence is dropped (the hook
# and the exit's fence check stay exact). An exit without a FENCE= line keeps the full deny list.
python3 - "$ID" "$M" <<'PY' || { echo "the per-loop deny list could not be computed"; rm -rf "$R" "$L"; exit 3; }
import json, re, sys
loop, base = sys.argv[1], sys.argv[2]
matches = [m for m in (re.match(r"^FENCE='([^']*)'\s*$", l) for l in open(f"loops/{loop}.exit.sh", encoding="utf-8").read().splitlines()) if m]
if len(matches) != 1:
    print(f"deny list: loops/{loop}.exit.sh has {len(matches)} FENCE= line(s); the full protected list stays"); raise SystemExit(0)
fence = matches[0].group(1)  # the quoted regex only (trailing whitespace after the quote is not part of it)
re.compile(fence)
def literal_prefix(alt):
    out, i = [], 0
    alt = alt[1:] if alt.startswith("^") else alt
    while i < len(alt):
        c = alt[i]
        if c == "\\" and i + 1 < len(alt): out.append(alt[i + 1]); i += 2; continue
        if c in "[](){}*+?$|.": break
        out.append(c); i += 1
    return "".join(out)
# the exits' convention: anchored alternatives joined by | at the top level (no groups). A grouped regex such as
# ^(a|b)$ cannot be split by this parser: then nothing is dropped (the hook still exempts by the full regex) and the launch says so.
if "(" in fence:
    print(f"deny list: the fence of {loop} uses groups; no deny rule dropped (the hook exempts by the full regex; write the fence as anchored alternatives to drop deny rules)")
    prefixes = []
else:
    prefixes = [literal_prefix(a) for a in fence.split("|")]
settings = json.load(open(".claude/settings.json"))
kept, dropped = [], []
for rule in settings["permissions"]["deny"]:
    m = re.match(r"^(\w+)\((.*)\)$", rule)
    if not m: kept.append(rule); continue
    pat = m.group(2); base_path = pat[:-3] if pat.endswith("/**") else pat
    granted = any(p == base_path or p.startswith(base_path + "/") or (not pat.endswith("/**") and p == base_path) for p in prefixes if p)
    (dropped if granted else kept).append(rule)
settings["permissions"]["deny"] = kept
json.dump(settings, open(".claude/settings.json", "w"), indent=2)
json.dump({"loop": loop, "base": base, "fence": fence, "deny_dropped": dropped}, open(".claude/fence.json", "w"), indent=2)
print(f"deny list for {loop}: {len(kept)} rule(s) kept, {len(dropped)} dropped as granted by the fence: " + (" ".join(dropped) or "none"))
PY
echo "coder protections installed (.claude/settings.json from tools/factory/coder-settings.json at $M minus the fence; .claude/fence.json; excluded locally)"
if [ $DRY -eq 1 ]; then echo "DRY: gate, frozen check and protections check passed for $ID at $M; not launching"; rm -rf "$R" "$L"; exit 0; fi
[ -d "$C/$ID-claude" ] || python3 "$C/new-claude-config.py" "$C/$ID-claude"
sed "s/{MERGE}/$M/g" "$C/$ID-prompt.txt.tmpl" > "$L/prompt.txt"
grep -q '{SMOKE_CMD}' "$C/stage1-smoke.txt" || { echo "$C/stage1-smoke.txt has no {SMOKE_CMD} placeholder; the smoke command would not reach the prompt"; rm -rf "$R" "$L"; exit 2; }
printf '\n\n=== SMOKE ===\n' >> "$L/prompt.txt"
SMOKE_CMD="$(cat "$C/$ID-smoke.txt")" python3 -c 'import os,sys; sys.stdout.write(open(sys.argv[1]).read().replace("{SMOKE_CMD}", os.environ["SMOKE_CMD"]))' "$C/stage1-smoke.txt" >> "$L/prompt.txt"
grep -q '{SMOKE_CMD}' "$L/prompt.txt" && { echo "the smoke block still holds {SMOKE_CMD}"; exit 2; }
sed "s/{MERGE}/$M/g" "$C/$ID-run.py.tmpl" > "$C/$ID-$M-run.py"
python3 -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$C/$ID-$M-run.py"
echo "account check (one real call on the R2D2 account, config dir $ID-claude)..."; R2D2_CONFIG_DIR="$C/$ID-claude" python3 "$C/r2d2-check.py" "$L/account-check.json" || { echo "R2D2 ACCOUNT CHECK FAILED"; exit 4; }
cd "$C"
nohup setsid python3 "$C/$ID-$M-run.py" > "$L/launcher.out" 2>&1 < /dev/null &
sleep 6; cat "$L/coder.json" 2>/dev/null || { echo "coder did not start"; tail -20 "$L/launcher.out"; exit 5; }
MA=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["model"], "on", d["account"])' "$L/launcher.json" 2>/dev/null) || { echo "LAUNCHED $ID BUT UNVERIFIED: $L/launcher.json is missing or lacks model/account (the coder is running: pid in $L/coder.json; check $C/$ID-run.py.tmpl)"; exit 7; }
echo "LAUNCHED $ID: repo $R logs $L ($MA)"
