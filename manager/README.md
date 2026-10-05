# The manager

One persistent Claude Code session behind `@manager`, woken by events, reachable in Slack and on the box as `hydra`.
The design is `docs/architecture.md` section 2; the spec is `loops/b1.md`. Rules that bind everything here: the repo
is the memory; Slack drives, the console observes and repairs; one writer at a time; every instruction and reply ends
up in a thread; operators use Slack only; tokens never leave `credentials/`.

## Pieces

| File | What it is |
|---|---|
| `supervisor.py` | the turn loop: queue, wake, engine choice and rotation, reply delivery, handoff, bookkeeping, persistence, PAUSE, budgets, the WRITER lock, the dead-man, the compaction policy, thread participation records |
| `bridge.py` | the Slack bridge (`slack_bolt` Socket Mode): allowlist, mirror, file download, the commands, thread participation and routing |
| `hydra` | the console CLI: `status`, `say`, `logs`, `tail`, `engine`, `pause`, `resume`, `attach`, `compact`, `post`, `update` |
| `models.json` | model aliases per family (`claude`, `codex`) with the family default; extendable by `$HYDRA_HOME/models.json` |
| `CLAUDE.md` | the manager's standing rules, read by Claude Code on every turn (installed at `$HYDRA_HOME/CLAUDE.md`) |
| `systemd/` | `hydra-manager.service` (supervisor loop) and `hydra-bridge.service` (bridge) |

Tests: `tests/manager/` (no network; fake engines, fake poster). Exit: `loops/b1.exit.sh` with the exit-owned
`loops/b1.acceptance.py`; later loops (`b2` shared memory, `b3` the transition read, `b4` threads and compaction,
`b5` the working indicator, `b6` the heartbeat, direct posts and the `hydra update` privilege split) add their own
exit scripts and acceptance harnesses under `loops/`.

## `$HYDRA_HOME` (default `/srv/hydra/manager`)

```text
inbox/events.jsonl          append-only queue: {id, source, at, payload}; id is the provider's (Slack ts, CLI uuid, timer slot)
inbox/handled.jsonl         ids already processed (appended only after the reply was delivered)
inbox/pending-replies.jsonl replies whose delivery failed; delivered first on the next tick, without a new turn
inbox/replies/<id>.txt      replies to console (`hydra say`) events
inbox/outbox.jsonl          posts queued by `hydra post` {id, at, channel, thread_ts, text, user}; the bridge drains it (see "Direct posts")
inbox/outbox-failed.jsonl   outbox lines set aside after 5 failed posts (with attempts, error, failed_at)
inbox/files/<ts>-<name>     attachments downloaded by the bridge
mirror/<channel>.jsonl      every message in a channel the bot is in (copied to factory/log/ and committed)
logs/turns.jsonl            {n, at, engine, model, events, reacted, duration_s, tokens?, input_tokens?, kind?, error?} per turn (kind: compaction for the compaction turn; reacted: the Slack message ts the working indicator went on)
logs/compaction.json        the compaction policy's state: pending, verify, last, failed_at, fail_posted, baseline_bytes
logs/reactions.json         {event id: {channel, ts, thread_ts, name, at}}: the reactions the working indicator has standing on messages (name: eyes, hourglass_flowing_sand or x)
logs/outbox.json            {posted, failed, last_at}: what the bridge has posted from the outbox (`hydra status` shows it)
logs/bridge.pid             the bridge service's pid: `hydra post` waits for the post when a bridge is alive
logs/bridge-health.json     the bridge's own idle-pong telemetry, rewritten every poll (see "The bridge")
logs/heartbeat, logs/supervisor.pid, logs/notes.json, logs/retry-after, logs/posts.jsonl and logs/reactions.jsonl (dry mode)
threads.json                {channel: {thread_ts: {joined_at, last_seen}}}: the threads the manager is part of (bridge and supervisor both write it, under threads.json.lock)
COMPACT                     present: a compaction runs before the next turn (hydra compact, @manager compact); content is who asked
engine                      JSON {"acc": claude-r2d2 | claude-l | codex, "model": <full id>}; a legacy one-word file is upgraded on the next turn
session-id                  the manager's Claude session id (created on the first turn)
codex-session               marker: a Codex session exists, later Codex turns `exec resume --last`
logs/codex-rollout          the rollout file `codex exec` last reported (when it printed one); the transition read prefers it
AGENTS.md                   symlink to app/manager/CLAUDE.md: Codex's mirror of the standing rules
MANAGER-HANDOFF.md          symlink into factory/manager-memory/ (the five-line summary, stamped `updated_at:`/`engine:` on every rewrite)
manager-memory/             the shared memory folder only when no faden clone is configured (normally it is <repo>/factory/manager-memory)
state.json                  the working copy of factory/state.json (copied into the faden clone after every turn)
budgets.json                {"turns_per_hour": n, "claude_turns_per_day": {"claude-r2d2": n, "claude-l": n}}
allowlist.json              {slack user or bot id: {"instructs": true|false}}
config.json                 optional: {"repo": <faden clone>, "dev_channel": "C…", "engines": {...}, "buildlog_webhook": url,
                            "codex_home": <Codex's $CODEX_HOME, default ~/.codex>, "transition": {"max_tokens": 100000, "tool_result_max_chars": 4000, "enabled": true},
                            "compaction": {"threshold_tokens": 300000, "max_bytes": 50000000, "codex_every_turns": 25, "quiet_hours": [2, 5], "quiet_hours_tz": "local"},
                            "reactions": {"heartbeat_seconds": 20}}
credentials/                claude-r2d2.env, claude-l.env (CLAUDE_CODE_OAUTH_TOKEN=…), slack.env (SLACK_BOT_TOKEN, SLACK_APP_TOKEN), buildlog.env (BUILDLOG_WEBHOOK)
PAUSE                       present: no turn runs; content is who paused
WRITER                      "<pid> <who>": the one writer; stale (dead pid) locks are reclaimed
.claude/                    CLAUDE_CONFIG_DIR of the manager session
venv/                       the project venv (slack_bolt); the entry points re-exec into it when it exists
app/manager/                this directory, installed by setup/manager-vm.sh
```

## Repository configuration

The service reads `/srv/hydra/manager/config.json` (`$HYDRA_HOME/config.json`), not a
config in the deployed `app/manager/` directory. `setup/manager-vm.sh` seeds:

```json
{"repo": "/srv/hydra/repos/faden"}
```

The bootstrap uses `$REPOS/faden` and adds `repo` **only when the key is missing**,
creating the config if absent. Other keys and an existing custom `repo` are preserved;
an existing key, including an explicit empty or null value, is not overwritten. Writes
are atomic, mode `0600`, and owned by `hydra`; reruns with a repo key leave the file
untouched. Invalid JSON, non-object configs and symlinks fail without overwriting them.
The unit intentionally has no `--repo` override, so this config remains authoritative.

With the default, shared memory lives at `/srv/hydra/repos/faden/factory/manager-memory/`,
and the supervisor commits memory, state and logs to that clone. Without a configured
repo it falls back to `$HYDRA_HOME/manager-memory/` and does not persist to Git.
Bootstrap only sets the path: it neither migrates existing local memory nor validates
that a private clone is ready. Before an operator starts/restarts the service, ensure
the configured clone exists and migrate any existing local memory separately.
`hydra update` refreshes code but does not seed or migrate this config; existing
installations need a separately approved config change. Repo changes take effect
when the supervisor restarts.

## Engines

The supervisor loads `default_engines()` in `app/manager/supervisor.py`, then applies optional
`config.json.engines` overrides. Default order is **`claude-r2d2` → `claude-l` → `codex`**. Binary paths
are resolved with `shutil.which()` using the supervisor process's `PATH` (not the operator's login-shell
`PATH`). On the manager VM the service resolves Claude and Codex under `/opt/hydra-tools/bin/`:

```json
{
  "claude-r2d2": {"bin": "/opt/hydra-tools/bin/claude", "cred": "claude-r2d2.env"},
  "claude-l": {"bin": "/opt/hydra-tools/bin/claude", "cred": "claude-l.env"},
  "codex": {"bin": "/opt/hydra-tools/bin/codex", "cred": null}
}
```

Claude credential paths are relative to `$HYDRA_HOME/credentials`. Codex uses its own login as `hydra`,
not a Claude credential (`cred: null`). The table is loaded at supervisor startup; configuration-table
changes require a supervisor restart, while the current `engine` file is read each turn.

### Models

`models.json` maps aliases to model ids per family. Claude: `fable5.1 → claude-fable-5-1` (default), `sonnet5 →
claude-sonnet-5`, `opus5 → claude-opus-5`, `haiku4.5 → claude-haiku-4-5-20251001`. Codex: `gpt6`/`astra → gpt-6-astra`
(default), `gpt5.6`/`sol → gpt-5.6-sol`. A full id of the family (`claude-…`, `gpt-…`) is accepted as-is. The Claude
engine gets `--model <id>`, Codex gets `-m <id>`. An automatic switch on quota or budget keeps the alias when the new
family has it, else takes the family default, and says so in the thread note: `engine: claude-l (claude-fable-5-1)`.

### Switching and forcing an engine

`engine acc=<claude-r2d2|claude-l|codex> [model=<alias|id>]` in Slack (`@manager engine …`, founder only) and on the
console (`hydra engine …`). The old form `engine <account>` still works and means the family default model;
`model=<alias>` alone keeps the account; `engine` alone prints the current pair. A model of the wrong family or an
unknown alias is rejected with the list of valid ones and nothing changes. `status` shows `engine: <acc> (<model>)`.

Each turn starts at the current engine and tries the remaining engines in cyclic order. A budget limit
or a nonzero exit whose stderr matches quota, usage limit, rate, 401, or 403 causes fallback and persists
the next successful engine. Other nonzero exits (including launch failures and timeouts) also try the
next engine, but do not persist a switch by themselves. A successful fallback adds `engine: <name>` to
the reply; `hydra logs 3` records the actual engine per turn even when no switch occurred. If every engine
fails, events remain pending and the supervisor waits five minutes before retrying.

Force the next turn without exhausting any account (this does not interrupt a turn already running):

```bash
hydra engine codex
hydra status
hydra logs 3
# Restore the preferred engine after testing:
hydra engine claude-r2d2
```

In Slack, the equivalent is `@manager engine codex` or `@manager engine claude-r2d2`, using a real bot
mention. Switching via Slack requires an allowlisted sender with `instructs: true`. `hydra engine` or
`@manager engine` without a name reports the current choice; forcing an engine still permits fallback
if that engine fails or is over budget.

### Shared rules and continuity

Both engines launch in `$HYDRA_HOME` (normally `/srv/hydra/manager`). Claude reads `CLAUDE.md`; Codex reads
`AGENTS.md`. Bootstrap installs the same standing rules for Claude and creates the relative mirror:

```text
/srv/hydra/manager/AGENTS.md -> app/manager/CLAUDE.md
```

This is a symlink, not a second editable rules file; reinstalling manager code refreshes its target.
The working directory is not a Git checkout, so a direct rules probe as `hydra` needs the same flag used
by the supervisor: `codex exec --skip-git-repo-check "summarize the rules you were given in three lines"`.

Claude accounts share the manager's Claude session ID and config directory. Codex does **not** inherit
the Claude transcript. Its first successful `codex exec` creates the `codex-session` marker; subsequent
Codex turns use `codex exec resume --last` in the same working directory. This marker is not a pinned
session ID, so avoid unrelated Codex sessions in the manager directory once it is in use.

Every Codex invocation also receives the current `MANAGER-HANDOFF.md` and working `state.json` (falling
back to `factory/state.json`) before the queued events. Every engine ends with `---HANDOFF---` and the
five-line handoff, which the supervisor writes back to disk. Files, not the other engine's transcript,
provide cross-engine continuity: if an old continuity-test word is absent from the available recorded
memory, say it is unknown rather than inventing it.

## A turn

1. `run_once()`: heartbeat; `PAUSE` present, nothing queued, a pending reply that still cannot be delivered, a
   retry-after from a failed turn, or `WRITER` held by a live process: no turn.
2. Every unhandled event is batched into one message: a fixed header per event (`source`, `channel`, `thread`,
   `sender`, `instructs`, `attachments`) and the text. Before the engine runs, every Slack message in the batch
   gets the working indicator, an `eyes` reaction (see "The working indicator").
3. Engines in order from the current one: `claude -p --resume <session-id> --model <id>
   --dangerously-skip-permissions --output-format json` with `CLAUDE_CONFIG_DIR=$HYDRA_HOME/.claude`, `HYDRA_HOME`,
   `HYDRA_MEMORY_DIR` and only the chosen credential file's `CLAUDE_CODE_OAUTH_TOKEN` in that process's environment
   (all inherited `CLAUDE*` and `ANTHROPIC*` variables are dropped). Every attempt gets the three-line `[memory]`
   preamble for its family in front of the batched message, and the `[transition]` block after it when the attempt
   changes the engine family (see "The transition read"). A non-zero exit whose stderr mentions quota, usage
   limit, rate, 401 or 403 moves to the next engine, persists the choice in `engine`, and adds `engine: <name>
   (<model>)` to the reply. Over budget: same, with a `budget:` note. Codex: `codex exec [resume --last]
   --skip-git-repo-check -m <id> -o <file> -` with `MANAGER-HANDOFF.md` and `state.json` prepended to the same message.
4. stdout is the reply; the text after `---HANDOFF---` is written to `factory/manager-memory/MANAGER-HANDOFF.md`
   with the `updated_at:`/`engine:` header. After a Claude turn the Claude memory files are snapshotted into
   `manager-memory/claude/`; the ledger line `{turn, at, engine, model, files_written, handoff_sha, transition?}` is
   appended, `files_written` being the hash diff of the folder before and after the turn (the supervisor's own
   `transition/` records excluded).
5. Delivery: one post per distinct Slack thread in the batch, into the thread the event came from (a top-level
   event's reply starts the thread on it); every thread posted to is recorded in `threads.json` as joined. Console
   events get `inbox/replies/<id>.txt` and a mirror post in `#dev` ("from L via console"); replies over 40 lines go
   to a file (`factory/log/replies/turn-<n>.md` in the clone, linked). If the poster raises, the reply is saved to
   `pending-replies.jsonl` and the events stay unhandled; the next tick delivers it without a new engine turn.
   As each thread's reply lands, the working indicator comes off that thread's messages; a pending reply keeps it.
   Every post goes through `post_and_record` (see "Direct posts"): post, record the join, mirror.
6. Bookkeeping: `logs/turns.jsonl`, `handled.jsonl`, the manager's own reply appended to `mirror/<channel>.jsonl`
   (`user: manager`), then `mirror/*.jsonl` to `<repo>/factory/log/`, `state.json` to `<repo>/factory/state.json`
   and `factory/manager-memory/`, commit `manager: turn <n>`, push.
7. Every engine failing: the turn is logged with `error`, one "manager unavailable, will retry" note per thread per
   hour, the `eyes` on the batch's messages replaced by `x`, no retry for five minutes.

The service loop adds a timer event every 15 minutes (`timer-<slot>`), and a watchdog thread trips the dead-man
(events queued, no turn and no heartbeat for 30 minutes): a post to the buildlog webhook and exit 3, so systemd
restarts the service.

## The working indicator (`loops/b5.md`)

A turn can run minutes with nothing visible in Slack, so the supervisor reacts to the messages it is working on:

- At the start of a turn, `reactor.add(channel, ts, "eyes")` for every Slack event in the batch (the payload's
  `ts`, which is also the event id). Timer and console events have no message and get nothing.
- When the reply for a thread is posted, `reactor.remove(channel, ts, "eyes")` for that thread's messages. A reply
  kept pending (the poster raised) keeps the reaction until the pending reply is delivered.
- When every engine failed, `eyes` becomes `x`; the `x` comes off when a later turn handles the event (the turn
  that picks it up puts `eyes` back on first).
- Reactions are best effort: a failing `add` or `remove` (missing scope, rate limit, deleted message) is logged
  once per hour (`logs/notes.json`, key `reactions`) and never blocks the turn or the reply.
- The heartbeat (`loops/b6.md`): while the engine runs, every `reactions.heartbeat_seconds` (`config.json`, default
  20, fractional allowed, `0` disables) a ticker thread swaps the reaction on each of the batch's messages between
  `eyes` and `hourglass_flowing_sand` (`remove` the standing one, `add` the other: two calls per message per
  interval). The ticker starts after the `eyes` go on and is stopped, and waited for, before delivery, so the final
  `remove` targets whatever stands and nothing is added after the reply. A failed `remove` does not stop the `add`
  that follows; `logs/reactions.json` records the name of the last `add` attempted, which is what delivery, a
  pending reply, or the `x` of a failed turn removes. The failure log shares the hourly `reactions` key. The ticker
  waits with `Supervisor(..., sleep=)` when one is given, else on its own stop flag.

`Supervisor(..., reactor=)` takes anything with `add(channel, ts, name)` and `remove(channel, ts, name)`; the default
is the bridge's `SdkReactor` (reactions.add / reactions.remove over a `slack_sdk` WebClient, the bot token from
`credentials/slack.env`; "already reacted" and "no reaction" count as done), or `DryReactor` without a token (appends
to `logs/reactions.jsonl`). What stands on which message is kept in `logs/reactions.json`, keyed by event id, so a
restart or a pending reply still knows what to take off; `logs/turns.jsonl` records `reacted: [ts...]` per turn.

The bot needs the `reactions:write` scope (`setup/slack-manifest.json` has it). `bridge.py --check` probes it when a
bot token is present: `auth.test` cannot list scopes, so it does a dry `reactions.add` (then `remove`) on the bot's
own last mirrored message and prints `reactions:write: ok`, a `WARNING` when Slack answers `missing_scope`, or
`unverified` when there is no own message yet, no token, or no network. The check never fails on this.

## Direct posts (`loops/b6.md`)

Every post the manager makes takes one path, `supervisor.post_and_record(home, poster, channel, thread_ts, text)`:
post through the poster, record the thread as joined in `threads.json`, mirror the line with its `thread_ts`
(`user: manager`; `subtype` `manager_reply` for a delivery, `manager_post` for a direct post). The supervisor's
deliveries use it; so does `Bridge.post(channel, thread_ts, text)`. A post with `thread_ts` None goes top level and
the `ts` the poster returns (`SlackPoster` and the bridge's `sdk_poster` return Slack's answer) names the thread it
starts, in `threads.json` and in the mirror line; a poster that returns nothing (dry mode) records no join.

The engine posts from inside a turn with `hydra post <channel> <thread_ts|-> <text>` (the rule is in `CLAUDE.md`;
no Slack tool is configured for the engine). The command appends to `inbox/outbox.jsonl`; the bridge service drains
it every second (`Bridge.drain_outbox`, oldest first) through `Bridge.post`, so replies to a direct post reach the
manager like replies to any of its posts. A line whose post raises stays with `attempts` and `error` and the drain
stops there (order is kept); after 5 attempts it is moved to `inbox/outbox-failed.jsonl` and the next line goes out.
With a live bridge (`logs/bridge.pid`) `hydra post` waits up to `HYDRA_POST_TIMEOUT` (30 s) and prints `posted`, or
exits 1 with the error when the line was set aside or is still queued; without one it prints `queued` and exits 0.
`hydra status` shows `direct posts: <n> posted, <k> queued`.

## The compaction policy (`loops/b4.md`)

The resumed Claude session grows without bound, and a long one costs hundreds of thousands of input tokens per turn.
The supervisor keeps it within `compaction.threshold_tokens` (default 300000):

- Every Claude turn records `input_tokens` in `turns.jsonl` (input + cache creation + cache read from the JSON
  usage; a plain-text `[usage] input_tokens=N` line is read the same way). A turn over the threshold, or a session
  file (`$CLAUDE_CONFIG_DIR/projects/<encoded cwd>/<session-id>.jsonl`) that grew by more than `compaction.max_bytes`
  (default 50 MB) since the last compaction (the file never shrinks, so growth is what counts), schedules a compaction
  before the next turn (`logs/compaction.json: pending`; `hydra status` says `compaction: pending (...)`).
- When the limit is crossed by less than 25% the compaction waits for `compaction.quiet_hours` (default `[2, 5]`,
  02:00 to 05:00) evaluated in `compaction.quiet_hours_tz` (`local`, the VM's zone, by default; `UTC` or any IANA
  name); 25% or more over, it runs immediately before the next turn. The timer events keep turns coming, so a
  deferred compaction runs in the first quiet-hours turn.
- The compaction itself, under the WRITER lock and before the batched turn: a dedicated turn on the same session,
  `[compaction] Compact: write everything from this session that must survive into MEMORY.md (facts, decisions,
  open questions, with dates), update MANAGER-HANDOFF.md, then reply only \`compacted\`.` (its handoff and memory
  writes are kept; its own ledger line has `kind: compaction`), then `claude -p --resume <session-id> --model <id>
  --dangerously-skip-permissions "/compact"`. The session id is never touched.
- The next Claude turn verifies: its `input_tokens` must be below the threshold. That turn's ledger line records
  `compaction: {before_tokens, after_tokens, at, ok, reason}`; `hydra status` shows `last compaction: <at>
  (<before> -> <after>)`.
- A failure (the `[compaction]` turn fails, `/compact` exits non-zero, or the tokens did not drop) is recorded with
  `ok: false` and `error` (on the compaction's own ledger line when known at once, else on the verifying turn's),
  posted once per streak to the buildlog (the webhook, or `buildlog_poster` in tests), and the policy backs off for
  `compaction.retry_after_s` (default 6 hours) before trying again. The session is kept as it is.
- `hydra compact` and `@manager compact` (founder only) write `COMPACT`: the compaction runs on the next tick, with or
  without queued events, regardless of quiet hours and the back-off. With a live loop `hydra compact` waits for it
  (`HYDRA_COMPACT_TIMEOUT`, default 900 s) and prints the outcome.
- Codex is not compacted (its continuity is `resume --last` plus the handoff and the memory files). Every
  `compaction.codex_every_turns` Codex turns (default 25; 0 turns it off) the turn starts with a `[flush]` line asking
  it to write everything durable since its last flush to `MEMORY.md` and `codex/NOTES.md` before the events.
- The engine's side (`[compaction]`: memory, handoff, reply `compacted`, nothing else; `[flush]`) is in `CLAUDE.md`
  under "Compaction and flushes".

## Shared memory across engines (`factory/manager-memory/`)

The two Claude accounts share one config directory, so a switch between them keeps the transcript and Claude Code's
memory notes; Codex has neither. The memory folder in the faden clone makes memory shared and ordered:

```text
MEMORY.md            canonical notes, read and written by every engine; entries `YYYY-MM-DD HH:MMZ <engine>:`; ## Facts, ## Decisions, ## Conflicts
claude/              snapshot of Claude Code's memory files ($CLAUDE_CONFIG_DIR/projects/<encoded cwd>/memory/*.md) after every Claude turn; read-only for engines
codex/NOTES.md       what the manager writes when it runs on Codex; Claude reads it
MANAGER-HANDOFF.md   the handoff, header `updated_at: <UTC ISO>` and `engine: <name>`; $HYDRA_HOME/MANAGER-HANDOFF.md is a symlink to it
LEDGER.jsonl         one line per turn {turn, at, engine, model, files_written, handoff_sha, transition?}: the clock; engines never compare file dates
transition/          <from>-to-<to>-<at>.md: the transition read given to the incoming engine at each family switch, for the record
```

Before every attempt the supervisor prepends exactly three lines:

```text
[memory] last turn: <at> on <engine> (turn N). your last turn on <family>: turn M at <at> | none.
[memory] changed by other engines since then: <path> (<at>), ... | none (same engine since your last turn).
[memory] MEMORY.md last written <at> by <engine> | never. Read the changed files before acting.
```

"Other engine" means the other family: `claude-r2d2` and `claude-l` are one, `codex` the other. The changed list is
the `files_written` of ledger turns after M whose family differs, plus the handoff when its `engine:` header is from
the other family. The engines' side of the contract (newer wins, conflicts, write to `MEMORY.md`) is in `CLAUDE.md`
under "Shared memory across engines"; Codex reads the same text through `AGENTS.md`.

### The transition read (a family switch, `loops/b3.md`)

What is said in a Claude session and not written down does not reach Codex through the files, and vice versa. So
when the family of the attempt about to run differs from the family of the last ledger turn, the supervisor reads
the other family's transcript and puts it inline, after the three `[memory]` lines, under this header:

```text
[transition] engine family switched from <a> to <b> at <at>. Below is the other engine's transcript since the last switch, flattened, nothing summarized. Read it fully before acting. Then write to MEMORY.md anything in it that must survive the next switch.
Transcript window: <first_at> to <last_at>, <kept> of <total> entries; <n> earlier entries not included.

HH:MMZ user: ...

HH:MMZ assistant: tool Bash({"command": "..."})

HH:MMZ tool: ...
```

- Sources (`manager/transcript.py`), both keyed by the directory the engines run in (`$HYDRA_HOME`): Claude Code's
  session file `$CLAUDE_CONFIG_DIR/projects/<encoded cwd>/<session-id>.jsonl` (`session-id` names it), and Codex's
  rollout under `$CODEX_HOME/sessions/` (the file the supervisor recorded from `codex exec`, else the latest whose
  `session_meta.cwd` is that directory). Another project's transcript is never used: with no match the block is one
  line, `[transition] ... no codex transcript found for <cwd> ...`, and the turn proceeds.
- Flattening keeps every user/assistant text whole, renders tool calls as `tool <name>(<arguments>)` (both Codex
  shapes, `function_call`/`arguments` and `custom_tool_call`/`input`), keeps tool results whole up to
  `transition.tool_result_max_chars` (then `[... N more chars omitted]`), keeps compaction summaries as `summary:`
  entries, and drops thinking/reasoning and the harnesses' own records. Nothing is summarized or reordered.
- `since` is the `at` of the last ledger turn run by the incoming family (the whole transcript when it never ran);
  transcript timestamps are compared with the ledger's, both UTC. The window keeps the newest whole entries within
  `transition.max_tokens` (chars / 3.5), never cutting inside an entry; only when the single newest entry alone
  exceeds the budget is it kept cut from its beginning, behind a first line `[entry truncated: N chars omitted]`.
- Same-family switches (`claude-r2d2` <-> `claude-l`) share one transcript and get no block. A transcript that
  cannot be found gives one `[transition]` line saying so, and the turn proceeds. `transition.enabled: false`
  turns the read off.
- The ledger line gains `transition: {from, to, source_path, since, first_at, last_at, entries_kept, entries_total,
  est_tokens}` and the same text is saved as `manager-memory/transition/<from>-to-<to>-<at>.md`.
- The engine's side ("read it entirely, then write what must survive to `MEMORY.md`, dated and tagged with the
  engine that said it") is in `CLAUDE.md` under "The transition read".

## The bridge

- Every message in a channel the bot is in is mirrored, with its `thread_ts` (null only for a true top-level post).
  Only allowlisted senders are queued; `instructs` comes from the allowlist (founder: true, operators: false).
  Strangers and bots not addressing the manager are never queued.
- Threads (`loops/b4.md`): an event's thread is its `thread_ts`, or its own `ts` for a top-level message the manager
  answers (the reply then starts the thread on it). The manager is part of a thread (`threads.json`) once it is
  mentioned in it, once it posts in it (the supervisor records its deliveries; `Bridge.note_own_post` records
  others), or once a message in it carries an `assignee:` line naming the manager (`manager`, `@manager`, or its
  mention). In a joined thread every later message from an allowlisted sender is queued, mention or not, with the
  allowlist's `instructs`. Messages in threads it has not joined, and top-level messages without a mention or an
  assignee line, are mirrored only. `@manager leave` (founder) leaves the thread; a thread without a message for
  14 days is pruned (on bridge start and daily, `Bridge.prune()`).
- A mention whose remaining text is exactly `status`, `pause`, `resume`, `engine [acc=…] [model=…]`, `digest now`,
  `leave` or `compact` is a command, answered without a turn (a command mention still joins the thread, except
  `leave`). `status` for any allowlisted sender; the rest only for `instructs: true`; anyone else gets "not
  authorized" and nothing changes. `status` includes `threads: <n> joined` and `last compaction: …`; `compact`
  writes the `COMPACT` flag.
- Replies go to the originating thread (a top-level message starts its own). While paused the bridge answers
  "paused (by …)" to mentions and queues them; while `hydra attach` holds `WRITER` it answers "manager in console
  session".
- Files are downloaded with `files:read` to `inbox/files/<ts>-<name>`; the path travels in the event.
- Idle-pong telemetry (`loops/b9.md`): `SocketHealth` counts the two kinds of inbound Socket Mode activity it
  already watches for liveness -- `envelope_count` (one per raw receipt) and `pong_count` (one per *changed*
  `last_ping_pong_time`; an unchanged value, or a replacement session carrying the old one, never counts).
  Outbound pings and Web API calls stay invisible. `run_socket_mode` also counts `reconnect_count` (one per
  recovery attempt, before its outcome is known, so a fatal attempt is still counted) and, every poll, writes
  `logs/bridge-health.json` (`health_snapshot` then `write_health_observation`, an atomic sibling-temp-file-then-
  `os.replace`): `at`, `monotonic`, `pid`, `process_started_at`, `connected`, `pong_count`, `envelope_count`,
  `reconnect_count`, `last_activity_age_s` and `poll_s` -- metadata only, nothing from Slack. A failed snapshot or
  write never changes the health result or the recovery decision, and logs at most one `health observation`
  diagnostic per streak of failed polls.

## The console

`hydra status | say "<text>" | logs [n] | tail <channel> [n] | engine [acc=<a>] [model=<m>] | pause [reason] | resume |
attach | compact | post <channel> <thread_ts|-> <text> | update`. `say` queues a `cli` event and, when the service loop is alive, waits for the reply. `attach`
takes `WRITER`, runs `claude --resume <session-id> --model <current model>` interactively with the same config dir and
token, releases the lock on exit and posts a two-line summary to `#dev`. `compact` forces a compaction (see "The
compaction policy"); with a live loop it waits for it and exits 1 when it failed.

`hydra update` (after every merge to `manager/`; `sudo -n hydra update` from an admin account): fast-forwards `main`
in the hydra-agent clone (`HYDRA_REPO`, default `/srv/hydra/repos/hydra-agent`), re-copies `manager/` into the deploy
dir (`HYDRA_APP`, default `/srv/hydra/manager/app/manager`, staged and swapped), refreshes `$HYDRA_HOME/CLAUDE.md` and
the `AGENTS.md` link, runs `systemctl restart hydra-bridge hydra-manager` (`systemctl` from `PATH`) and prints
`updated to <commit>`. The privilege split (`loops/b6.md`): the clone is `hydra`'s, so as root every git step runs
through `sudo -n -u hydra -H git -C <clone> ...` (git's ownership check passes without any `safe.directory`); as any
other user git runs directly. The copy into the root-owned deploy dir, the link and the restarts are root's: any
other caller, `hydra` itself on the VM for instance, gets the git steps done and the line `... need root: run
\`sudo -n hydra update\`` with exit 0, nothing copied or restarted. A clone that does not fast-forward, a failed git
step, or a failed restart exits 1 and says so. `HYDRA_FAKE_UID=<int>` stands in for the effective uid (the acceptance
harness's injection).

## Dry mode

Without `SLACK_BOT_TOKEN` in `credentials/slack.env` the poster appends to `logs/posts.jsonl` and prints, and the
working indicator's reactor appends to `logs/reactions.jsonl`. `python3 manager/supervisor.py --once` runs one tick;
`python3 manager/bridge.py --check` validates `allowlist.json` and the presence of `credentials/slack.env`, and reports
the `reactions:write` scope as unverified without a token.

## Install

`setup/manager-vm.sh` copies this directory to `/srv/hydra/manager/app/manager/`, installs `CLAUDE.md` at
`/srv/hydra/manager/CLAUDE.md`, creates `AGENTS.md -> app/manager/CLAUDE.md` in the same working directory,
creates `/srv/hydra/manager/venv` with `slack_bolt` and `slack_sdk`, installs both units and
`/usr/local/bin/hydra`, and defaults a missing `config.json.repo` to `/srv/hydra/repos/faden`
without replacing other settings. Then (a human, over SSH, never through Slack):
`credentials/slack.env`, `allowlist.json`, add the `#dev` channel id to the existing
`config.json`, verify the configured clone/memory, and `systemctl restart hydra-manager hydra-bridge`.
