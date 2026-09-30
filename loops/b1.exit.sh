#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:-origin/main}"
fence() {
  CHANGED=$( (git diff --name-only "$BASE"...HEAD; git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  BAD=$(printf '%s\n' "$CHANGED" | grep -Ev '^$|^(manager/|tests/manager/|setup/manager-vm\.sh$|setup/manager-vm\.md$)' || true)
  if [ -n "$BAD" ]; then echo "[b1] FAIL: outside fence: $BAD"; exit 1; fi
}
echo "[b1] fence..."; fence
for f in manager/supervisor.py manager/bridge.py manager/hydra manager/CLAUDE.md manager/systemd/hydra-manager.service manager/systemd/hydra-bridge.service manager/README.md; do
  test -f "$f" || { echo "[b1] FAIL: $f missing"; exit 1; }
done
test -x manager/hydra || { echo "[b1] FAIL: manager/hydra is not executable"; exit 1; }
grep -q 'HANDOFF' manager/CLAUDE.md || { echo "[b1] FAIL: CLAUDE.md does not mention the handoff"; exit 1; }
grep -rEq 'xoxb-[0-9]|xapp-[0-9]|sk-ant-oat' manager/ tests/ setup/ 2>/dev/null && { echo "[b1] FAIL: a token-shaped string in the tree"; exit 1; } || true
echo "[b1] venv + tests..."
[ -d .venv ] || python3 -m venv .venv || { echo "[b1] FAIL: venv"; exit 1; }
.venv/bin/python -m pip install -q pytest slack_bolt >/dev/null 2>&1 || { echo "[b1] FAIL: pip"; exit 1; }
.venv/bin/python -m pytest -q tests/manager || { echo "[b1] FAIL: pytest"; exit 1; }
echo "[b1] exit-owned acceptance..."
.venv/bin/python loops/b1.acceptance.py || { echo "[b1] FAIL: acceptance"; exit 1; }
echo "[b1] entry points and units..."
H=$(mktemp -d); mkdir -p "$H/inbox" "$H/credentials" "$H/.claude" "$H/mirror" "$H/logs"; echo claude-r2d2 > "$H/engine"; echo '{}' > "$H/allowlist.json"; : > "$H/credentials/slack.env"
HYDRA_HOME="$H" .venv/bin/python manager/supervisor.py --once || { echo "[b1] FAIL: supervisor --once"; exit 1; }
HYDRA_HOME="$H" .venv/bin/python manager/bridge.py --check || { echo "[b1] FAIL: bridge --check"; exit 1; }
for u in manager/systemd/*.service; do
  exec_line=$(grep -E '^ExecStart=' "$u" | head -1); [ -n "$exec_line" ] || { echo "[b1] FAIL: $u has no ExecStart"; exit 1; }
  echo "$exec_line" | grep -qE 'manager/(supervisor|bridge)\.py' || { echo "[b1] FAIL: $u ExecStart does not run the entry point: $exec_line"; exit 1; }
  if command -v systemd-analyze >/dev/null 2>&1; then
    out=$(systemd-analyze verify "$u" 2>&1); rc=$?; [ "$rc" -eq 0 ] || { echo "[b1] FAIL: systemd-analyze verify $u: $out"; exit 1; }
  fi
done
rm -rf "$H"
echo "[b1] fence again..."; fence
echo "[b1] PASS"
