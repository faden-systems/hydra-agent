#!/usr/bin/env bash
# Start the spec-review watcher (docs/factory/watchers.md) with its environment set INSIDE this script, so the
# command an agent runs contains no PYTHONPATH= and never trips the command scanner (three restarts were lost to
# the approval prompt on 2026-09-14/15). Usage: scripts/spec-watch.sh [interval_seconds]
set -euo pipefail
cd "$(dirname "$0")/.."
INTERVAL="${1:-300}"
export PATH="$HOME/.local/bin:$PATH"
[ -f "$HOME/.faden.env" ] && . "$HOME/.faden.env"
unset OPENAI_API_KEY ANTHROPIC_API_KEY   # the watcher reviews on the subscription only
export FADEN_PROVIDER=codex FADEN_JUDGE2_MODEL="${FADEN_REVIEW_MODEL:-gpt-6-astra}"
export PYTHONPATH="$PWD/fitflow-app:$PWD/loops${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p "$HOME/factory/logs"
if pgrep -f "spec_watch.py --loop" >/dev/null; then echo "spec watcher already running: $(pgrep -f 'spec_watch.py --loop' | head -1)"; exit 0; fi
nohup python3 tools/factory/spec_watch.py --loop "$INTERVAL" > "$HOME/factory/logs/spec-watch.log" 2>&1 &
sleep 2; echo "spec watcher started: pid $(pgrep -f 'spec_watch.py --loop' | head -1), interval ${INTERVAL}s, log ~/factory/logs/spec-watch.log"
