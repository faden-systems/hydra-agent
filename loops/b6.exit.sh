#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:-origin/main}"
fence() {
  CHANGED=$( (git diff --name-only "$BASE"...HEAD; git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  BAD=$(printf '%s\n' "$CHANGED" | grep -Ev '^$|^(manager/|tests/manager/|setup/manager-vm\.md$|setup/manager-vm\.sh$|\.venv/)' || true)
  if [ -n "$BAD" ]; then echo "[b6] FAIL: outside fence: $BAD"; exit 1; fi
}
git rev-parse --verify -q "$BASE" >/dev/null || { echo "[b6] FAIL: base $BASE does not resolve (fetch origin main)"; exit 1; }
for a in b1 b2 b3 b4 b5 b6; do test -f "loops/$a.acceptance.py" || { echo "[b6] FAIL: loops/$a.acceptance.py missing"; exit 1; }; done
echo "[b6] fence..."; fence
grep -q 'hydra post' manager/CLAUDE.md && grep -q 'never call the Slack API directly' manager/CLAUDE.md || { echo "[b6] FAIL: the posting rule (hydra post ... never call the Slack API directly) is not in CLAUDE.md"; exit 1; }
grep -q 'safe.directory' setup/manager-vm.md && { echo "[b6] FAIL: the safe.directory workaround must be gone from the runbook"; exit 1; } || true
grep -rEq 'xoxb-[0-9]|xapp-[0-9]|sk-ant-oat' manager/ tests/ setup/ 2>/dev/null && { echo "[b6] FAIL: token-shaped string"; exit 1; } || true
echo "[b6] venv + tests..."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q pytest slack_bolt >/dev/null 2>&1 || { echo "[b6] FAIL: pip"; exit 1; }
.venv/bin/python -m pytest -q tests/manager || { echo "[b6] FAIL: pytest"; exit 1; }
echo "[b6] exit-owned acceptance..."
for a in b6 b5 b4 b3 b2 b1; do .venv/bin/python loops/$a.acceptance.py || { echo "[b6] FAIL: $a acceptance"; exit 1; }; done
echo "[b6] fence again..."; fence
echo "[b6] PASS"
