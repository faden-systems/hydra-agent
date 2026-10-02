#!/usr/bin/env bash
# Faden factory loop launcher.
# Usage: scripts/launch-loop.sh <loop-id> [model] [max-attempts]
# Detached use: python3 -c "import subprocess; subprocess.Popen([...], start_new_session=True)"  (see agent rule)
# Account: reads ~/factory/account (set by scripts/account.sh) -> CLAUDE_CONFIG_DIR=~/.claude-<name>
# Retry policy: attempt 1 fresh; attempt 2 CONTINUES the same session with the exit failure fed in (no reset);
#               attempt 3+ fresh with the previous failure tail + PROGRESS notes as a hint.
# A closed usage window is not a failed attempt: limit_hit event, wait, retry.
# On PASS the launcher opens the PR and stops; the manager verifies on a fresh clone and merges (am1, founder 2026-10-02).
# Claude Code honours ANTHROPIC_API_KEY over the subscription login, so the key from ~/.faden.env (needed by the
# app and the simulator) is stripped from every `claude` invocation: loops bill the Max plan, sessions bill the API.
set -uo pipefail
LOOP="${1:?loop id required}"; MODEL="${2:-claude-fable-5-1}"; MAX="${3:-3}"
REPO="$HOME/factory/faden"; WT="$HOME/factory/wt-$LOOP"; BR="loop/$LOOP"
FAILED="$HOME/factory/failed"; LOGS="$HOME/factory/logs"
LIMIT_WAIT_SECS="${LIMIT_WAIT_SECS:-900}"; LIMIT_MAX_WAITS="${LIMIT_MAX_WAITS:-8}"
export PATH="$HOME/.local/bin:$PATH"
[ -f "$HOME/.faden.env" ] && . "$HOME/.faden.env"
mkdir -p "$FAILED" "$LOGS"
HOST=$(hostname -s)
ACC="default"
if [ -f "$HOME/factory/account" ]; then
  A=$(tr -d '[:space:]' < "$HOME/factory/account"); if [ -n "$A" ] && [ "$A" != "default" ] && [ -d "$HOME/.claude-$A" ]; then export CLAUDE_CONFIG_DIR="$HOME/.claude-$A"; ACC="$A"; fi
fi

notify() {
  echo "[launch] $1"
  if [ -n "${SLACK_BUILDLOG_WEBHOOK:-}" ]; then
    curl -s -X POST -H 'Content-type: application/json' \
      --data "{\"text\":\"[launch] $HOST/$LOOP account=$ACC: $1\"}" "$SLACK_BUILDLOG_WEBHOOK" >/dev/null 2>&1 || true
  fi
}
gov() { [ -f tools/governor/governor.py ] && python3 tools/governor/governor.py "$@" 2>/dev/null; }

cd "$REPO" || exit 2
git fetch origin
git worktree remove "$WT" --force 2>/dev/null || true
git branch -D "$BR" 2>/dev/null || true
git worktree add "$WT" -b "$BR" origin/main || exit 2
cd "$WT" || exit 2
test -f "loops/$LOOP.md" || { notify "FAIL: loops/$LOOP.md not found on main"; exit 2; }
test -f "loops/$LOOP.exit.sh" || { notify "FAIL: loops/$LOOP.exit.sh not found on main"; exit 2; }
notify "started model=$MODEL max=$MAX"

BASE_PROMPT="You are loop $LOOP. Read CLAUDE.md, loops/$LOOP.md, and every file in seams/. Build exactly what the spec says, nothing more. When done, run: BASE_REF=origin/main bash loops/$LOOP.exit.sh - fix until it prints PASS. Then stop."
attempt=1; limit_waits=0; mode="fresh"; LAST_FAIL=""; LAST_NOTES=""
while [ "$attempt" -le "$MAX" ]; do
  echo "[launch] $LOOP attempt $attempt/$MAX model=$MODEL mode=$mode account=$ACC $(date '+%H:%M')"
  OUT=$(gov check --model "$MODEL" || true)
  case "$OUT" in WAIT*) SECS=$(echo "$OUT" | awk '{print $2}'); echo "[launch] governor: $OUT"; sleep "${SECS:-300}";; esac
  gov event attempt_start --note "$LOOP $attempt $MODEL $ACC" >/dev/null || true
  CLOG="$LOGS/$LOOP-claude-$(date '+%Y%m%d-%H%M%S').log"
  case "$mode" in
    continue)
      env -u ANTHROPIC_API_KEY claude -p "The exit script failed. Its output tail:
$LAST_FAIL
Fix the problems, re-run: BASE_REF=origin/main bash loops/$LOOP.exit.sh - until it prints PASS. Then stop." \
        --continue --model "$MODEL" --dangerously-skip-permissions 2>&1 | tee "$CLOG" ;;
    hint)
      env -u ANTHROPIC_API_KEY claude -p "$BASE_PROMPT
Context: a previous attempt failed its exit criteria with this output tail:
$LAST_FAIL
Its notes were:
$LAST_NOTES" --model "$MODEL" --dangerously-skip-permissions 2>&1 | tee "$CLOG" ;;
    *)
      env -u ANTHROPIC_API_KEY claude -p "$BASE_PROMPT" --model "$MODEL" --dangerously-skip-permissions 2>&1 | tee "$CLOG" ;;
  esac
  gov event attempt_end --note "$LOOP $attempt" >/dev/null || true
  if grep -qiE "hit your session limit|usage limit reached|rate limit" "$CLOG"; then
    limit_waits=$((limit_waits + 1))
    gov event limit_hit --note "$LOOP attempt=$attempt model=$MODEL account=$ACC" >/dev/null || true
    if [ "$limit_waits" -gt "$LIMIT_MAX_WAITS" ]; then notify "FAIL: usage limit still closed after $LIMIT_MAX_WAITS waits - needs a human"; exit 3; fi
    notify "usage limit hit (wait $limit_waits/$LIMIT_MAX_WAITS); sleeping ${LIMIT_WAIT_SECS}s, attempt not consumed"
    sleep "$LIMIT_WAIT_SECS"
    continue
  fi
  git add -A && git commit -qm "$LOOP: attempt $attempt ($MODEL, $mode)" || true
  EXITLOG="$LOGS/$LOOP-exit-$attempt-$(date '+%Y%m%d-%H%M%S').log"
  BASE_REF=origin/main bash "loops/$LOOP.exit.sh" 2>&1 | tee "$EXITLOG"
  RC=${PIPESTATUS[0]}
  if [ "$RC" -eq 0 ]; then
    git push -u origin "$BR"
    gh pr create --fill --label "loop:$LOOP" || gh pr create --fill
    URL=$(gh pr view --json url -q .url 2>/dev/null || echo "no-pr-url")
    notify "PASS on attempt $attempt/$MAX ($mode) -> $URL (PR open, awaiting the manager's verification and merge)"
    exit 0
  fi
  LAST_FAIL=$(tail -25 "$EXITLOG")
  LAST_NOTES=$(tail -30 PROGRESS.md 2>/dev/null || echo "(no notes)")
  git diff origin/main...HEAD > "$FAILED/$LOOP-attempt$attempt-$(date '+%Y%m%d-%H%M').diff" 2>/dev/null || true
  cp PROGRESS.md "$FAILED/$LOOP-attempt$attempt-PROGRESS.md" 2>/dev/null || true
  if [ "$attempt" -eq 1 ]; then
    echo "[launch] attempt 1 failed exit criteria; attempt 2 will CONTINUE the session (no reset)"
    mode="continue"
  else
    echo "[launch] attempt $attempt failed exit criteria; resetting worktree; next attempt fresh with hint"
    git reset -q --hard origin/main && git clean -qfd
    mode="hint"
  fi
  attempt=$((attempt + 1))
done
notify "FAILED after $MAX attempts - needs a human (see $FAILED)"
# 2026-09-15: a three-attempt failure also lands in #faden-relay with the last exit lines and the artifact paths,
# so the steering conversation sees it on its next check without asking (six hours were lost to a silent failure)
if [ -n "${SLACK_RELAY_WEBHOOK:-}" ]; then
  LAST=$(tail -n 8 "$LOG" 2>/dev/null | tr -d '"' | tr '\n' ' ' | cut -c1-900)
  ARTS=$(ls -t "$FAILED"/${LOOP}-* 2>/dev/null | head -3 | tr '\n' ' ')
  curl -s -X POST -H 'Content-type: application/json' --data "{\"text\":\"[launch] $HOST/$LOOP FAILED after $MAX attempts. Last exit lines: $LAST | Artifacts: $ARTS\"}" "$SLACK_RELAY_WEBHOOK" >/dev/null 2>&1 || true
fi
exit 1
