# The manager

One persistent Claude Code session behind `@manager`, woken by events, reachable in Slack and on the box as `hydra`.
The design is `docs/architecture.md` section 2; the spec is `loops/b1.md`. Rules that bind everything here: the repo
is the memory; Slack drives, the console observes and repairs; one writer at a time; every instruction and reply ends
up in a thread; operators use Slack only; tokens never leave `credentials/`.

## Pieces

| File | What it is |
|---|---|
| `supervisor.py` | the turn loop: queue, wake, engine choice and rotation, reply delivery, handoff, bookkeeping, persistence, PAUSE, budgets, the WRITER lock, the dead-man |
| `bridge.py` | the Slack bridge (`slack_bolt` Socket Mode): allowlist, mirror, file download, the five commands, thread routing |
| `hydra` | the console CLI: `status`, `say`, `logs`, `tail`, `engine`, `pause`, `resume`, `attach` |
| `CLAUDE.md` | the manager's standing rules, read by Claude Code on every turn (installed at `$HYDRA_HOME/CLAUDE.md`) |
| `systemd/` | `hydra-manager.service` (supervisor loop) and `hydra-bridge.service` (bridge) |

Tests: `tests/manager/` (no network; fake engines, fake poster). Exit: `loops/b1.exit.sh` with the exit-owned
`loops/b1.acceptance.py`.

## `$HYDRA_HOME` (default `/srv/hydra/manager`)

```text
inbox/events.jsonl          append-only queue: {id, source, at, payload}; id is the provider's (Slack ts, CLI uuid, timer slot)
inbox/handled.jsonl         ids already processed (appended only after the reply was delivered)
inbox/pending-replies.jsonl replies whose delivery failed; delivered first on the next tick, without a new turn
inbox/replies/<id>.txt      replies to console (`hydra say`) events
inbox/files/<ts>-<name>     attachments downloaded by the bridge
mirror/<channel>.jsonl      every message in a channel the bot is in (copied to factory/log/ and committed)
logs/turns.jsonl            {n, at, engine, events, duration_s, tokens?, error?} per turn
logs/heartbeat, logs/supervisor.pid, logs/notes.json, logs/retry-after, logs/posts.jsonl (dry mode)
engine                      current engine: claude-r2d2 | claude-l | codex
session-id                  the manager's Claude session id (created on the first turn)
codex-session               marker: a Codex session exists, later Codex turns `exec resume --last`
MANAGER-HANDOFF.md          the manager's five-line summary, rewritten from every ---HANDOFF--- block
state.json                  the working copy of factory/state.json (copied into the faden clone after every turn)
budgets.json                {"turns_per_hour": n, "claude_turns_per_day": {"claude-r2d2": n, "claude-l": n}}
allowlist.json              {slack user or bot id: {"instructs": true|false}}
config.json                 optional: {"repo": <faden clone>, "dev_channel": "C…", "engines": {...}, "buildlog_webhook": url}
credentials/                claude-r2d2.env, claude-l.env (CLAUDE_CODE_OAUTH_TOKEN=…), slack.env (SLACK_BOT_TOKEN, SLACK_APP_TOKEN), buildlog.env (BUILDLOG_WEBHOOK)
PAUSE                       present: no turn runs; content is who paused
WRITER                      "<pid> <who>": the one writer; stale (dead pid) locks are reclaimed
.claude/                    CLAUDE_CONFIG_DIR of the manager session
venv/                       the project venv (slack_bolt); the entry points re-exec into it when it exists
app/manager/                this directory, installed by setup/manager-vm.sh
```

## A turn

1. `run_once()`: heartbeat; `PAUSE` present, nothing queued, a pending reply that still cannot be delivered, a
   retry-after from a failed turn, or `WRITER` held by a live process: no turn.
2. Every unhandled event is batched into one message: a fixed header per event (`source`, `channel`, `thread`,
   `sender`, `instructs`, `attachments`) and the text.
3. Engines in order from the current one: `claude -p --resume <session-id> --model claude-fable-5-1
   --dangerously-skip-permissions --output-format json` with `CLAUDE_CONFIG_DIR=$HYDRA_HOME/.claude` and only the
   chosen credential file's `CLAUDE_CODE_OAUTH_TOKEN` in that process's environment (all inherited `CLAUDE*` and
   `ANTHROPIC*` variables are dropped). A non-zero exit whose stderr mentions quota, usage limit, rate, 401 or 403
   moves to the next engine, persists the choice in `engine`, and adds `engine: <name>` to the reply. Over budget:
   same, with a `budget:` note. Codex: `codex exec [resume --last] --skip-git-repo-check -o <file> -` with
   `MANAGER-HANDOFF.md` and `state.json` prepended to the same message.
4. stdout is the reply; the text after `---HANDOFF---` is written to `MANAGER-HANDOFF.md`.
5. Delivery: one post per distinct Slack thread in the batch; console events get `inbox/replies/<id>.txt` and a
   mirror post in `#dev` ("from L via console"); replies over 40 lines go to a file (`factory/log/replies/turn-<n>.md`
   in the clone, linked). If the poster raises, the reply is saved to `pending-replies.jsonl` and the events stay
   unhandled; the next tick delivers it without a new engine turn.
6. Bookkeeping: `logs/turns.jsonl`, `handled.jsonl`, then `mirror/*.jsonl` to `<repo>/factory/log/` and `state.json`
   to `<repo>/factory/state.json`, commit `manager: turn <n>`, push.
7. Every engine failing: the turn is logged with `error`, one "manager unavailable, will retry" note per thread per
   hour, no retry for five minutes.

The service loop adds a timer event every 15 minutes (`timer-<slot>`), and a watchdog thread trips the dead-man
(events queued, no turn and no heartbeat for 30 minutes): a post to the buildlog webhook and exit 3, so systemd
restarts the service.

## The bridge

- Every message in a channel the bot is in is mirrored. Only allowlisted senders are queued; `instructs` comes from
  the allowlist (founder: true, operators: false). Strangers and bots not addressing the manager are never queued.
- A mention whose remaining text is exactly `status`, `pause`, `resume`, `engine <name>` or `digest now` is a
  command, answered without a turn. `status` for any allowlisted sender; the rest only for `instructs: true`;
  anyone else gets "not authorized" and nothing changes.
- Replies go to the originating thread (a top-level message starts its own). While paused the bridge answers
  "paused (by …)" to mentions and queues them; while `hydra attach` holds `WRITER` it answers "manager in console
  session".
- Files are downloaded with `files:read` to `inbox/files/<ts>-<name>`; the path travels in the event.

## The console

`hydra status | say "<text>" | logs [n] | tail <channel> [n] | engine [name] | pause [reason] | resume | attach`.
`say` queues a `cli` event and, when the service loop is alive, waits for the reply. `attach` takes `WRITER`, runs
`claude --resume <session-id>` interactively with the same config dir and token, releases the lock on exit and posts a
two-line summary to `#dev`.

## Dry mode

Without `SLACK_BOT_TOKEN` in `credentials/slack.env` the poster appends to `logs/posts.jsonl` and prints.
`python3 manager/supervisor.py --once` runs one tick; `python3 manager/bridge.py --check` validates `allowlist.json`
and the presence of `credentials/slack.env`.

## Install

`setup/manager-vm.sh` copies this directory to `/srv/hydra/manager/app/manager/`, installs `CLAUDE.md` at
`/srv/hydra/manager/CLAUDE.md`, creates `/srv/hydra/manager/venv` with `slack_bolt`, installs both units and
`/usr/local/bin/hydra`. Then (a human, over SSH, never through Slack): `credentials/slack.env`, `allowlist.json`,
`config.json` with the faden clone and the `#dev` channel id, `systemctl restart hydra-manager hydra-bridge`.
