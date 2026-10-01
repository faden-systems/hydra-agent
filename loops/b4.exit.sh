#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:-origin/main}"
fence() {
  CHANGED=$( (git diff --name-only "$BASE"...HEAD; git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  BAD=$(printf '%s\n' "$CHANGED" | grep -Ev '^$|^(manager/|tests/manager/|setup/manager-vm\.md$|\.venv/)' || true)
  if [ -n "$BAD" ]; then echo "[b4] FAIL: outside fence: $BAD"; exit 1; fi
}
echo "[b4] fence..."; fence
grep -q '\[compaction\]' manager/CLAUDE.md || { echo "[b4] FAIL: the compaction rule is not in CLAUDE.md"; exit 1; }
grep -rEq 'xoxb-[0-9]|xapp-[0-9]|sk-ant-oat' manager/ tests/ 2>/dev/null && { echo "[b4] FAIL: token-shaped string"; exit 1; } || true
echo "[b4] venv + tests..."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q pytest slack_bolt >/dev/null 2>&1 || { echo "[b4] FAIL: pip"; exit 1; }
.venv/bin/python -m pytest -q tests/manager || { echo "[b4] FAIL: pytest"; exit 1; }
echo "[b4] exit-owned acceptance..."
for a in b4 b3 b2 b1; do .venv/bin/python loops/$a.acceptance.py || { echo "[b4] FAIL: $a acceptance"; exit 1; }; done
echo "[b4] fence again..."; fence
echo "[b4] PASS"
