#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:-origin/main}"
fence() {
  CHANGED=$( (git diff --name-only "$BASE"...HEAD; git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  BAD=$(printf '%s\n' "$CHANGED" | grep -Ev '^$|^(manager/|tests/manager/|setup/manager-vm\.sh$|setup/manager-vm\.md$|\.venv/)' || true)
  if [ -n "$BAD" ]; then echo "[b3] FAIL: outside fence: $BAD"; exit 1; fi
}
echo "[b3] fence..."; fence
test -f manager/transcript.py || { echo "[b3] FAIL: manager/transcript.py missing"; exit 1; }
ls tests/manager/fixtures/transcripts/*claude*.jsonl >/dev/null 2>&1 || { echo "[b3] FAIL: recorded Claude transcript fixture missing"; exit 1; }
ls tests/manager/fixtures/transcripts/*codex*.jsonl >/dev/null 2>&1 || { echo "[b3] FAIL: recorded Codex rollout fixture missing"; exit 1; }
grep -rEq 'xoxb-[0-9]|xapp-[0-9]|sk-ant-oat|ghp_[A-Za-z0-9]{20}' tests/manager/fixtures manager/ 2>/dev/null && { echo "[b3] FAIL: token-shaped string in fixtures or code"; exit 1; } || true
grep -q '\[transition\]' manager/CLAUDE.md || { echo "[b3] FAIL: the transition rule is not in CLAUDE.md"; exit 1; }
echo "[b3] venv + tests..."
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q pytest slack_bolt >/dev/null 2>&1 || { echo "[b3] FAIL: pip"; exit 1; }
.venv/bin/python -m pytest -q tests/manager || { echo "[b3] FAIL: pytest"; exit 1; }
echo "[b3] exit-owned acceptance..."
.venv/bin/python loops/b3.acceptance.py || { echo "[b3] FAIL: b3 acceptance"; exit 1; }
.venv/bin/python loops/b2.acceptance.py || { echo "[b3] FAIL: b2 acceptance regressed"; exit 1; }
.venv/bin/python loops/b1.acceptance.py || { echo "[b3] FAIL: b1 acceptance regressed"; exit 1; }
echo "[b3] fence again..."; fence
echo "[b3] PASS"
