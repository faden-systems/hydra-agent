#!/usr/bin/env bash
# Faden factory: pick which Claude account the launcher uses.
# Usage: scripts/account.sh use <name> | show | list
# A named account is a Claude Code config dir: ~/.claude-<name>  (created by: CLAUDE_CONFIG_DIR=~/.claude-<name> claude  -> /login)
# "default" = ~/.claude (the normal login). The active name lives in ~/factory/account and is read by launch-loop.sh.
set -uo pipefail
FILE="$HOME/factory/account"; export PATH="$HOME/.local/bin:$PATH"; mkdir -p "$HOME/factory"
dir_for() { if [ "$1" = "default" ]; then echo "$HOME/.claude"; else echo "$HOME/.claude-$1"; fi; }
email_for() {
  local d="$1" f="$1/.claude.json"
  [ "$d" = "$HOME/.claude" ] && [ -f "$HOME/.claude.json" ] && f="$HOME/.claude.json"
  python3 - "$f" << 'PY' 2>/dev/null || echo "?"
import json, sys
try:
    d = json.load(open(sys.argv[1])); print(d.get("oauthAccount", {}).get("emailAddress") or "?")
except Exception:
    print("?")
PY
}
case "${1:-show}" in
  login)
    # Start the browser login for a named account inside a detached tmux session (it must survive between an agent's
    # tool calls) and print the sign-in URL. A human completes it and relays the code with: account.sh code <n> <code>
    NAME="${2:?usage: account.sh login <n>}"; D=$(dir_for "$NAME"); mkdir -p "$D"
    command -v tmux >/dev/null || { echo "tmux is required for login relay (brew install tmux)"; exit 2; }
    tmux kill-session -t "claude-login-$NAME" 2>/dev/null || true
    tmux new-session -d -s "claude-login-$NAME" -x 200 -y 50 "CLAUDE_CONFIG_DIR=$D env -u ANTHROPIC_API_KEY claude"
    sleep 4; tmux send-keys -t "claude-login-$NAME" "/login" Enter; sleep 6
    URL=$(tmux capture-pane -p -t "claude-login-$NAME" -S -200 | grep -o 'https://[^ ]*oauth/authorize[^ ]*' | tail -1)
    [ -n "$URL" ] || { echo "no sign-in URL yet; pane says:"; tmux capture-pane -p -t "claude-login-$NAME" | tail -15; exit 3; }
    echo "sign in as the '$NAME' account, then run: scripts/account.sh code $NAME <code>"
    echo "$URL" ;;
  code)
    NAME="${2:?usage: account.sh code <n> <code>}"; CODE="${3:?usage: account.sh code <n> <code>}"; D=$(dir_for "$NAME")
    tmux has-session -t "claude-login-$NAME" 2>/dev/null || { echo "no login session for $NAME; run: account.sh login $NAME"; exit 2; }
    tmux send-keys -t "claude-login-$NAME" "$CODE" Enter; sleep 8
    tmux capture-pane -p -t "claude-login-$NAME" | grep -iE "logged in|login successful|welcome|error|invalid" | tail -3
    tmux kill-session -t "claude-login-$NAME" 2>/dev/null || true
    echo "probe (must answer ok):"
    CLAUDE_CONFIG_DIR="$D" env -u ANTHROPIC_API_KEY claude -p "reply with exactly: ok" --model claude-sonnet-5 2>&1 | tail -2
    echo "email=$(email_for "$D")  dir=$D" ;;
  use)
    NAME="${2:?usage: account.sh use <name>}"; D=$(dir_for "$NAME")
    [ -d "$D" ] || { echo "no config dir $D. Log in first (human, browser): CLAUDE_CONFIG_DIR=$D claude  then /login"; exit 2; }
    echo "$NAME" > "$FILE"
    echo "active account: $NAME  email=$(email_for "$D")  dir=$D"
    echo "probe (must answer ok):"
    if [ "$NAME" = "default" ]; then env -u ANTHROPIC_API_KEY claude -p "reply with exactly: ok" --model claude-sonnet-5 2>&1 | tail -3
    else CLAUDE_CONFIG_DIR="$D" env -u ANTHROPIC_API_KEY claude -p "reply with exactly: ok" --model claude-sonnet-5 2>&1 | tail -3; fi ;;
  show)
    NAME=$(tr -d '[:space:]' < "$FILE" 2>/dev/null || true); NAME="${NAME:-default}"; D=$(dir_for "$NAME")
    echo "active account: $NAME  email=$(email_for "$D")  dir=$D" ;;
  list)
    for d in "$HOME/.claude" "$HOME"/.claude-*; do
      [ -d "$d" ] || continue
      n=$(basename "$d"); n="${n#.claude-}"; [ "$n" = ".claude" ] && n="default"
      echo "$n  email=$(email_for "$d")  dir=$d"
    done ;;
  *) echo "usage: account.sh use <name> | show | list"; exit 2 ;;
esac
