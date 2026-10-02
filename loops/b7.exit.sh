#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:-origin/main}"
fail() { echo "[b7] FAIL: $*"; exit 1; }
git rev-parse --verify "$BASE" >/dev/null || fail "missing base $BASE"
fence() {
  local changed bad
  changed=$( (git diff --name-only "$BASE"...HEAD; git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  bad=$(printf '%s\n' "$changed" | grep -Ev '^$|^manager/(bridge\.py|supervisor\.py|hydra|README\.md|CLAUDE\.md)$|^tests/manager/|^docs/notes/b7\.md$' || true)
  test -z "$bad" || fail "outside fence: $bad"
  for f in loops/b7.md loops/b7.exit.sh loops/b7.acceptance.py; do
    git show "$BASE:$f" | cmp -s - "$f" || fail "exit-owned file changed: $f"
  done
}
fence
test -f docs/notes/b7.md || fail 'notes missing'
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import pytest, slack_bolt' || fail 'install pytest and slack_bolt in selected Python environment'
TMP=$(mktemp -d)
trap 'git worktree remove --force "$TMP/base" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT
git worktree add --detach "$TMP/base" "$BASE" >/dev/null
"$PYTHON" -m pytest --collect-only -q "$TMP/base/tests/manager" >"$TMP/base.nodes" || fail 'base collection'
"$PYTHON" -m pytest --collect-only -q tests/manager >"$TMP/head.nodes" || fail 'head collection'
"$PYTHON" - "$TMP/base.nodes" "$TMP/head.nodes" <<'PY'
import sys
from pathlib import Path
def nodes(p):
    return {line[line.index('tests/manager/'):].strip() for line in Path(p).read_text().splitlines() if 'tests/manager/' in line and '::' in line}
a,b=map(nodes,sys.argv[1:]); assert a, 'empty baseline node set'; assert a <= b, f'dropped existing tests: {a-b}'
PY
"$PYTHON" -m pytest -q tests/manager || fail pytest
# Watchdog bounds all exit-owned subprocess cases, including a broken reconnect.
"$PYTHON" - "$PYTHON" <<'PY'
import subprocess,sys
for loop in ('b7','b6','b5','b4','b3','b2','b1'):
    try: result=subprocess.run([sys.argv[1],f'loops/{loop}.acceptance.py'],timeout=180)
    except subprocess.TimeoutExpired: raise SystemExit(f'[b7] FAIL: {loop} acceptance timeout')
    if result.returncode: raise SystemExit(f'[b7] FAIL: {loop} acceptance exit {result.returncode}')
PY
fence
echo '[b7] PASS'
