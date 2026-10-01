#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:-origin/main}"
fence() {
  CHANGED=$( (git diff --name-only "$BASE"...HEAD; git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  BAD=$(printf '%s\n' "$CHANGED" | grep -Ev '^$|^(manager/|tests/manager/|setup/manager-vm\.md$|setup/slack-manifest\.json$|\.venv/)' || true)
  if [ -n "$BAD" ]; then echo "[b5] FAIL: outside fence: $BAD"; exit 1; fi
}
echo "[b5] fence..."; fence
test -f setup/slack-manifest.json && grep -q 'reactions:write' setup/slack-manifest.json || { echo "[b5] FAIL: setup/slack-manifest.json must exist and include reactions:write"; exit 1; }
grep -rEq 'xoxb-[0-9]|xapp-[0-9]|sk-ant-oat' manager/ tests/ setup/ 2>/dev/null && { echo "[b5] FAIL: token-shaped string"; exit 1; } || true
echo "[b5] venv + tests..."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q pytest slack_bolt >/dev/null 2>&1 || { echo "[b5] FAIL: pip"; exit 1; }
.venv/bin/python -m pytest -q tests/manager || { echo "[b5] FAIL: pytest"; exit 1; }
echo "[b5] exit-owned acceptance..."
for a in b5 b4 b3 b2 b1; do .venv/bin/python loops/$a.acceptance.py || { echo "[b5] FAIL: $a acceptance"; exit 1; }; done
echo "[b5] fence again..."; fence
echo "[b5] PASS"
