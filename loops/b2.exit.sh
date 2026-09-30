#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:-origin/main}"
fence() {
  CHANGED=$( (git diff --name-only "$BASE"...HEAD; git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  BAD=$(printf '%s\n' "$CHANGED" | grep -Ev '^$|^(manager/|tests/manager/|setup/manager-vm\.sh$|setup/manager-vm\.md$|\.venv/)' || true)
  if [ -n "$BAD" ]; then echo "[b2] FAIL: outside fence: $BAD"; exit 1; fi
}
echo "[b2] fence..."; fence
for f in manager/models.json manager/CLAUDE.md; do test -f "$f" || { echo "[b2] FAIL: $f missing"; exit 1; }; done
grep -q 'Newer wins' manager/CLAUDE.md || { echo "[b2] FAIL: the memory rule is not in CLAUDE.md"; exit 1; }
grep -q 'update' manager/hydra || { echo "[b2] FAIL: hydra has no update command"; exit 1; }
grep -rEq 'xoxb-[0-9]|xapp-[0-9]|sk-ant-oat' manager/ tests/ setup/ 2>/dev/null && { echo "[b2] FAIL: token-shaped string"; exit 1; } || true
echo "[b2] venv + tests..."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q pytest slack_bolt >/dev/null 2>&1 || { echo "[b2] FAIL: pip"; exit 1; }
.venv/bin/python -m pytest -q tests/manager || { echo "[b2] FAIL: pytest"; exit 1; }
echo "[b2] exit-owned acceptance..."
.venv/bin/python loops/b2.acceptance.py || { echo "[b2] FAIL: b2 acceptance"; exit 1; }
.venv/bin/python loops/b1.acceptance.py || { echo "[b2] FAIL: b1 acceptance regressed"; exit 1; }
echo "[b2] fence again..."; fence
echo "[b2] PASS"
