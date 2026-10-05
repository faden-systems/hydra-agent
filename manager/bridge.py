#!/usr/bin/env python3
"""The Slack bridge: `@manager` in Slack, Socket Mode, no public endpoint.

`Bridge(home, allowlist, poster, token_env)` is testable without a client: `handle_message(event)`,
`handle_file(event)`, `handle_reaction(event)`. Every message in a channel the bot is in is mirrored to
`$HYDRA_HOME/mirror/<channel>.jsonl` with its `thread_ts` (null only for a true top-level post). An allowlisted
sender's message is queued for the supervisor when it mentions the manager, carries an `assignee:` line naming the
manager, or is in a thread the manager has joined (loops/b4.md: `$HYDRA_HOME/threads.json`, joined by a mention, by
the manager's own post, or by an assignee line; left by `@manager leave` (founder) or after 14 days of silence).
Anything else is mirrored only. The commands (`status`, `pause`, `resume`, `engine [acc=<account>] [model=<alias>]`,
`digest now`, `leave`, `compact`) are answered without a turn.

Entry points: `bridge.py --check` validates `allowlist.json` and the presence of `credentials/slack.env` without
connecting; with a bot token it also probes the `reactions:write` scope (loops/b5.md: `auth.test` cannot show scopes,
so a dry `reactions.add` goes on the bot's own last mirrored message, when there is one) and warns, never fails, when
the scope is missing or unverified. `SdkReactor(client)` is the working indicator's reactor over `slack_sdk`, the same
`add/remove(channel, ts, name)` interface the supervisor takes. No argument runs the Socket Mode service (systemd).

Direct posts (loops/b6.md): `Bridge.post(channel, thread_ts, text)` is the one path every post of the manager takes
(`supervisor.post_and_record`: post, record the join, mirror with `thread_ts`); `drain_outbox()` posts what `hydra post`
queued in `inbox/outbox.jsonl` through it, and the service runs `pump_outbox` in a thread that drains every second.
The service's poster returns Slack's answer, so a top-level post (`-`) joins the thread its `ts` starts. The service
writes `logs/bridge.pid` so `hydra post` knows whether a bridge is there to drain.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import supervisor as S  # noqa: E402

MENTION_RE = re.compile(r"<@([A-Za-z0-9_]+)(?:\|[^>]*)?>")
COMMAND_RE = re.compile(r"^(status|pause|resume|digest now|leave|compact|engine(?:\s+(.+?))?)\s*$", re.IGNORECASE)
INSTRUCT_COMMANDS = ("pause", "resume", "engine", "digest", "leave", "compact")
ASSIGNEE_RE = re.compile(r"^[ \t]*assignee:[ \t]*([^|\n]*)", re.IGNORECASE | re.MULTILINE)
MANAGER_NAMES = ("manager", "@manager")
PRUNE_EVERY_S = 86400
OUTBOX_POLL_S = 1.0
SKIPPED_SUBTYPES = {"message_changed", "message_deleted", "channel_join", "channel_leave", "channel_topic",
                    "channel_purpose", "channel_name", "group_join", "group_leave"}
FOOTER_RE = re.compile(r"[\n \t]+\*Sent using\*[ \t]*<@[A-Za-z0-9_]+(?:\|[^>\n]*)?>[ \t\n]*$")


def strip_attribution_footer(text):
    """Remove one trailing `*Sent using* <@ID>` or `*Sent using* <@ID|label>` footer (requirement 14, loops/b7.md):
    it must end the message, separated by a newline or spaces; a footer inside a quoted line (`>`) or an
    (optionally unclosed) triple-backtick fenced code block is never stripped. The rest of the message, including
    multiline arguments, is preserved untouched."""
    if not text:
        return text
    m = FOOTER_RE.search(text)
    if not m:
        return text
    prefix = text[:m.start()]
    if prefix.count("```") % 2 == 1:
        return text  # inside an unclosed fence
    line_start = prefix.rfind("\n") + 1
    if text[line_start:].lstrip().startswith(">"):
        return text  # a quoted line
    return prefix


def _render_leaves(elements):
    """Plain text out of a flat list of rich_text leaf elements: text, a user mention, a link (its label else its
    url), an emoji shortcode. Unknown/malformed elements are safely ignored (requirement 7, loops/b7.md)."""
    out = []
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        t = el.get("type")
        if t == "text":
            out.append(el.get("text") or "")
        elif t == "user":
            uid = el.get("user_id")
            if uid:
                out.append(f"<@{uid}>")
        elif t == "link":
            out.append(el.get("text") or el.get("url") or "")
        elif t == "emoji":
            name = el.get("name")
            if name:
                out.append(f":{name}:")
        elif t in ("channel", "usergroup"):
            val = el.get("channel_id") or el.get("usergroup_id")
            if val:
                out.append(f"<#{val}>" if t == "channel" else f"<!subteam^{val}>")
    return "".join(out)


def _render_rich_text(elements):
    """One `rich_text` block's elements: sections, lists (each item a section), quotes and preformatted (code)
    blocks, whose own `elements` are leaves directly. Readable separators between pieces are preserved."""
    parts = []
    for item in elements or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "rich_text_section":
            parts.append(_render_leaves(item.get("elements")))
        elif t == "rich_text_list":
            items = [_render_leaves(sub.get("elements")) for sub in (item.get("elements") or [])
                    if isinstance(sub, dict)]
            parts.append(" ".join(p for p in items if p))
        elif t in ("rich_text_quote", "rich_text_preformatted"):
            parts.append(_render_leaves(item.get("elements")))
        # an unknown container type is safely ignored
    return " ".join(p for p in parts if p)


def normalize_blocks(blocks):
    """Recursively render Slack `blocks` into readable plain text (requirement 7, loops/b7.md): rich_text
    sections/lists/quotes/preformatted, text, user mentions, links, emoji, and section/context plain or mrkdwn
    text. Unknown or malformed elements (including a bare `None`) are safely ignored."""
    parts = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "rich_text":
            text = _render_rich_text(block.get("elements"))
            if text:
                parts.append(text)
        elif t == "section":
            field = block.get("text")
            if isinstance(field, dict) and field.get("type") in ("plain_text", "mrkdwn"):
                text = field.get("text") or ""
                if text:
                    parts.append(text)
        elif t == "context":
            for el in block.get("elements") or []:
                if isinstance(el, dict) and el.get("type") in ("plain_text", "mrkdwn"):
                    text = el.get("text") or ""
                    if text:
                        parts.append(text)
        # an unknown block type is safely ignored
    return "\n".join(parts)


def load_allowlist(home):
    path = os.path.join(home, "allowlist.json")
    data = json.loads(S.read_text(path, "{}") or "{}")
    if not isinstance(data, dict):
        raise ValueError("allowlist.json must be a JSON object {user_id: {\"instructs\": bool}}")
    out = {}
    for uid, entry in data.items():
        if isinstance(entry, bool):
            entry = {"instructs": entry}
        if not isinstance(entry, dict) or not isinstance(entry.get("instructs"), bool):
            raise ValueError(f"allowlist.json: entry {uid!r} needs a boolean \"instructs\"")
        out[uid] = entry
    return out


def slack_downloader(token_env):
    token = (token_env or {}).get("SLACK_BOT_TOKEN")

    def download(url, dest):
        if not token:
            raise RuntimeError("no SLACK_BOT_TOKEN for files:read")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        return dest
    return download


class Bridge:
    def __init__(self, home, allowlist, poster, token_env, bot_user_id=None, downloader=None, allowlist_path=None,
                 repo=None):
        self.home = home
        self.allowlist = allowlist if allowlist is not None else {}
        self.poster = poster
        self.token_env = token_env or {}
        self.bot_user_id = bot_user_id
        self.downloader = downloader or slack_downloader(self.token_env)
        self.allowlist_path = allowlist_path
        self._allowlist_mtime = None
        self.repo = repo or S.load_config(home).get("repo")
        for d in ("inbox", "inbox/files", "mirror", "logs"):
            os.makedirs(os.path.join(home, d), exist_ok=True)
        self._last_prune = 0.0
        self.maybe_prune()  # the 14-day rule on bridge start

    # ---- helpers
    def path(self, *parts):
        return os.path.join(self.home, *parts)

    @property
    def bot_id(self):
        """The bot's own Slack user id (`bot_user_id`); settable after construction."""
        return self.bot_user_id

    @bot_id.setter
    def bot_id(self, value):
        self.bot_user_id = value

    # ---- threads (loops/b4.md)
    def join(self, channel, thread_ts):
        return S.note_thread(self.home, channel, thread_ts)

    def note_own_post(self, channel, thread_ts):
        """The manager posted in (channel, thread_ts): the thread is joined (the supervisor records its own
        deliveries the same way; this is for posts made elsewhere)."""
        return S.note_thread(self.home, channel, thread_ts)

    def joined(self, channel, thread_ts):
        return S.thread_joined(self.home, channel, thread_ts)

    def prune(self, days=S.THREAD_PRUNE_DAYS):
        """Drop threads without a message for `days` (default 14). Returns what was removed."""
        self._last_prune = time.time()
        removed = S.prune_threads(self.home, days)
        if removed:
            S.log(f"threads pruned: {', '.join(f'{c}/{t}' for c, t in removed)}")
        return removed

    def maybe_prune(self):
        if time.time() - self._last_prune >= PRUNE_EVERY_S:
            self.prune()

    def assigned_to_manager(self, text):
        """True when an `assignee:` line names the manager (`manager`, `@manager`, or the bot's mention)."""
        names = []
        for m in ASSIGNEE_RE.finditer(MENTION_RE.sub(lambda mm: " manager " if self._is_me(mm.group(1)) else " ", text or "")):
            names += [n.strip().strip("@").lower() for n in re.split(r"[,\s]+", m.group(1)) if n.strip()]
        return any(n in MANAGER_NAMES for n in names)

    def _is_me(self, uid):
        return self.bot_user_id is None or uid == self.bot_user_id

    def _maybe_reload_allowlist(self):
        if not self.allowlist_path or not os.path.exists(self.allowlist_path):
            return
        mtime = os.path.getmtime(self.allowlist_path)
        if mtime != self._allowlist_mtime:
            try:
                self.allowlist = load_allowlist(self.home)
                self._allowlist_mtime = mtime
            except ValueError as e:
                S.log(f"allowlist not reloaded: {e}")

    def mirror(self, channel, record):
        if not channel:
            return
        record = {"mirrored_at": time.time(), **record}
        S.append_jsonl(self.path("mirror", f"{channel}.jsonl"), record)

    def reply(self, channel, thread_ts, text):
        try:
            self.poster(channel, thread_ts, text)
        except Exception as e:  # a bridge answer is best effort; the event is still queued or mirrored
            S.log(f"bridge reply failed: {e}")

    # ---- direct posts (loops/b6.md)
    def post(self, channel, thread_ts, text, user=None):
        """A post the manager makes outside a delivery (from inside a turn, through `hydra post`): the one posting
        path, so the thread is joined and the line mirrored with its `thread_ts`. `thread_ts` None posts top level
        and the ts the poster returns names the thread. Raises what the poster raises."""
        return S.post_and_record(self.home, self.poster, channel, thread_ts, text, subtype="manager_post", user=user)

    def drain_outbox(self):
        """Post every line `hydra post` queued, oldest first, through `post`. Returns the number posted."""
        return S.drain_outbox(self.home, lambda channel, thread_ts, text: self.post(channel, thread_ts, text))

    def pump_outbox(self, stop, every=OUTBOX_POLL_S):
        """Drain the outbox every `every` seconds until `stop` (a threading.Event) is set; a failing drain is logged
        and tried again on the next tick."""
        while not stop.is_set():
            try:
                self.drain_outbox()
            except Exception as e:
                S.log(f"outbox drain failed: {e}")
            stop.wait(every)

    def parse_mention(self, text):
        """(addressed, rest): addressed when the bot is mentioned (any mention counts if the bot id is unknown)."""
        addressed = False
        for m in MENTION_RE.finditer(text or ""):
            if self.bot_user_id is None or m.group(1) == self.bot_user_id:
                addressed = True
        rest = MENTION_RE.sub("", text or "") if addressed else (text or "")
        return addressed, " ".join(rest.split())

    def sender_entry(self, ev):
        for key in ("user", "bot_id"):
            sid = ev.get(key)
            if sid and sid in self.allowlist:
                return sid, self.allowlist[sid]
        return None, None

    def paused_by(self):
        p = self.path("PAUSE")
        return (S.read_text(p).strip() or "unknown") if os.path.exists(p) else None

    def console_holder(self):
        ws = S.writer_status(self.home)
        if ws and ws[0] != "stale" and ws[1] and not ws[1].startswith("supervisor"):
            return ws[1]
        return None

    # ---- files
    def download_files(self, ev):
        out = []
        ts = ev.get("ts") or str(time.time())
        for f in ev.get("files") or []:
            if not isinstance(f, dict):
                continue
            name = os.path.basename(f.get("name") or f.get("title") or f.get("id") or "file")
            url = f.get("url_private_download") or f.get("url_private")
            dest = self.path("inbox", "files", f"{ts}-{name}")
            entry = {"name": name, "id": f.get("id"), "path": None}
            if url:
                try:
                    self.downloader(url, dest)
                    entry["path"] = dest
                except Exception as e:
                    entry["error"] = str(e)
            out.append(entry)
        return out

    # ---- events
    def handle_message(self, ev):
        """One Slack `message` event (dict). Mirrors it; queues it for allowlisted senders; answers commands.
        Nonblank top-level text is authoritative; only a blank text falls back to the normalized blocks
        (requirement 7, loops/b7.md). A trailing `*Sent using*` attribution footer is stripped from the routing
        copy after that normalization and before command parsing (requirement 14); the mirror may keep it."""
        self._maybe_reload_allowlist()
        self.maybe_prune()  # the 14-day rule, daily
        channel = ev.get("channel")
        subtype = ev.get("subtype")
        if subtype in SKIPPED_SUBTYPES:
            return None
        raw_text = ev.get("text") or ""
        text = raw_text if raw_text.strip() else normalize_blocks(ev.get("blocks"))
        cleaned = strip_attribution_footer(text)
        ts = ev.get("ts")
        files = self.download_files(ev)
        is_bot = bool(ev.get("bot_id")) or subtype == "bot_message"
        self.mirror(channel, {"type": "message", "ts": ts, "thread_ts": ev.get("thread_ts"), "user": ev.get("user"),
                              "bot_id": ev.get("bot_id"), "subtype": subtype, "text": text,
                              "files": [f["path"] or f["name"] for f in files]})
        if self.bot_user_id and ev.get("user") == self.bot_user_id:
            return None  # our own posts
        sender, entry = self.sender_entry(ev)
        if entry is None:
            return None  # a stranger: mirrored, never queued
        addressed, rest = self.parse_mention(cleaned)
        if is_bot and not addressed:
            return None  # another bot talking to the channel, not to us
        thread_ts = ev.get("thread_ts") or ts  # a thread root the manager answers starts its own thread
        if addressed:
            m = COMMAND_RE.match(rest)
            if m:
                if m.group(1).split()[0].lower() != "leave":
                    self.join(channel, thread_ts)  # a mention joins the thread, except the one that leaves it
                return self.command(m, sender, entry, channel, thread_ts)
        if addressed or self.assigned_to_manager(cleaned):
            self.join(channel, thread_ts)
        elif self.joined(channel, thread_ts):
            self.join(channel, thread_ts)  # touches last_seen
        else:
            return None  # an unjoined thread, or a top-level post without a mention: mirrored only
        event = S.append_event(self.home, S.new_event(
            "slack", {"channel": channel, "ts": ts, "thread_ts": thread_ts, "user": sender, "text": cleaned,
                      "instructs": bool(entry.get("instructs")), "addressed": addressed, "files": files},
            event_id=ts))
        if addressed:
            paused = self.paused_by()
            holder = self.console_holder()
            if paused:
                self.reply(channel, thread_ts, f"paused (by {paused}); queued for when the manager resumes")
            elif holder:
                self.reply(channel, thread_ts, f"manager in console session ({holder}); queued for after it ends")
        return event

    def handle_file(self, ev, file_info=None):
        """A `file_shared` event carries only ids; with `file_info(file_id) -> dict` the file is downloaded."""
        channel = ev.get("channel_id") or ev.get("channel")
        info = None
        if file_info and ev.get("file_id"):
            try:
                info = file_info(ev["file_id"])
            except Exception as e:
                S.log(f"files.info failed: {e}")
        files = self.download_files({"ts": ev.get("event_ts") or ev.get("ts"), "files": [info] if info else []})
        self.mirror(channel, {"type": "file_shared", "ts": ev.get("event_ts"), "user": ev.get("user_id") or ev.get("user"),
                              "file_id": ev.get("file_id"), "files": [f["path"] or f["name"] for f in files]})
        return files

    def handle_reaction(self, ev):
        """Reactions are mirrored (the manager reads them from the log); they never queue a turn."""
        item = ev.get("item") or {}
        self.mirror(item.get("channel"), {"type": "reaction", "ts": item.get("ts"), "user": ev.get("user"),
                                          "reaction": ev.get("reaction"), "event_ts": ev.get("event_ts")})
        return None

    # ---- the commands
    def command(self, m, sender, entry, channel, thread_ts):
        word = m.group(1).lower()
        name = word.split()[0]
        if name in INSTRUCT_COMMANDS and not entry.get("instructs"):
            self.reply(channel, thread_ts, f"not authorized: `{word}` needs the founder")
            return {"command": word, "ok": False}
        if name == "status":
            self.reply(channel, thread_ts, S.status_text(self.home, self.repo))
        elif name == "pause":
            paused = self.paused_by()
            if paused:
                self.reply(channel, thread_ts, f"already paused (by {paused})")
            else:
                S.write_text(self.path("PAUSE"), f"{sender}\n")
                self.reply(channel, thread_ts, f"paused (by {sender}); no turn runs until `resume`")
        elif name == "resume":
            if self.paused_by() is None:
                self.reply(channel, thread_ts, "not paused")
            else:
                os.remove(self.path("PAUSE"))
                self.reply(channel, thread_ts, f"resumed; {S.queue_depth(self.home)} event(s) queued")
        elif name == "engine":
            target = (m.group(2) or "").strip()
            current = S.read_engine(self.home)
            if not target:
                self.reply(channel, thread_ts, f"engine: {S.engine_label(current)} [{current['mode']}]")
            else:
                try:
                    pair = S.parse_engine_command(target, current=current)
                except S.BadEngine as e:
                    self.reply(channel, thread_ts, f"{e}; nothing changed (engine: {S.engine_label(current)} [{current['mode']}])")
                    return {"command": word, "ok": False}
                S.set_engine(self.home, pair["acc"], pair["model"], mode=pair.get("mode"))
                updated = S.read_engine(self.home)
                self.reply(channel, thread_ts, f"engine: {S.engine_label(updated)} [{updated['mode']}]")
        elif name == "digest":
            S.append_event(self.home, S.new_event(
                "timer", {"text": "digest now: write the digest (what ran, what it found, what it cost, what needs a "
                                  "decision) for this cycle.", "digest": True, "channel": channel,
                          "thread_ts": thread_ts, "user": sender, "instructs": True}))
            self.reply(channel, thread_ts, "digest queued")
        elif name == "leave":
            if S.forget_thread(self.home, channel, thread_ts):
                self.reply(channel, thread_ts, "left this thread; mention me, or assign me, to bring me back")
            else:
                self.reply(channel, thread_ts, "not in this thread")
                return {"command": word, "ok": False}
        elif name == "compact":
            S.write_text(self.path("COMPACT"), f"{sender}\n")
            self.reply(channel, thread_ts, "compaction scheduled; it runs before the manager's next turn")
        return {"command": word, "ok": True}


# ----------------------------------------------------------------------------------------------- reactions

class SdkReactor:
    """The working indicator's reactor over a `slack_sdk` WebClient (`App.client`, or `sdk_reactor(token)` for the
    supervisor): `add` and `remove` raise on failure, except when Slack says the reaction is already there or
    already gone; the supervisor treats every failure as best effort."""

    def __init__(self, client):
        self.client = client

    def _call(self, action, channel, ts, name):
        method = getattr(self.client, f"reactions_{action}")
        try:
            return method(channel=channel, timestamp=ts, name=name)
        except Exception as e:
            if _slack_error(e) in S.REACTION_IDEMPOTENT[action]:
                return None
            raise

    def add(self, channel, ts, name):
        return self._call("add", channel, ts, name)

    def remove(self, channel, ts, name):
        return self._call("remove", channel, ts, name)


def _slack_error(e):
    """Slack's error string out of a `SlackApiError` (its `response`, a SlackResponse or a dict), else None."""
    response = getattr(e, "response", None)
    try:
        return response.get("error") if response is not None else None
    except (AttributeError, TypeError):
        return None


def sdk_reactor(token):
    """The real reactor for `token`: `SdkReactor` over a fresh `slack_sdk.WebClient`. Raises ImportError without
    slack_sdk (the supervisor then falls back to its dry reactor)."""
    from slack_sdk import WebClient
    return SdkReactor(WebClient(token=token))


OWN_SUBTYPES = ("manager_reply", "manager_post")


def own_last_message(home):
    """(channel, ts) of the latest post the manager mirrored with a Slack ts (a delivery or a direct post), or None
    (dry posts carry none)."""
    best = None
    mirror = os.path.join(home, "mirror")
    for name in sorted(os.listdir(mirror)) if os.path.isdir(mirror) else []:
        if not name.endswith(".jsonl"):
            continue
        channel = name[:-len(".jsonl")]
        for rec in S.read_jsonl(os.path.join(mirror, name)):
            if rec.get("subtype") not in OWN_SUBTYPES or not rec.get("ts"):
                continue
            at = rec.get("mirrored_at") or 0
            if best is None or at > best[0]:
                best = (at, channel, str(rec["ts"]))
    return (best[1], best[2]) if best else None


def sdk_scope_probe(token, channel, ts):
    """A dry `reactions.add` (then `reactions.remove`) of `eyes` on (channel, ts) with `token`. Returns None when
    the scope works, else Slack's error string (`missing_scope` when the token lacks `reactions:write`)."""
    from slack_sdk.errors import SlackApiError
    reactor = sdk_reactor(token)
    try:
        reactor.add(channel, ts, S.REACTION_WORKING)
    except SlackApiError as e:
        return _slack_error(e) or str(e)
    try:
        reactor.remove(channel, ts, S.REACTION_WORKING)
    except SlackApiError:
        pass  # the probe worked; a reaction left behind is harmless
    return None


def check_reactions_scope(home, token, probe=None):
    """One line about `reactions:write`, printed; never fails the check. The token is never printed."""
    if not token:
        print("reactions:write: unverified (no SLACK_BOT_TOKEN in credentials/slack.env)")
        return
    target = own_last_message(home)
    if target is None:
        print("reactions:write: unverified (no own message to probe yet; add the scope at api.slack.com if the eyes "
              "never appear)")
        return
    channel, ts = target
    try:
        error = (probe or sdk_scope_probe)(token, channel, ts)
    except Exception as e:  # no network, no slack_sdk, anything: unverified, not a failure
        print(f"reactions:write: unverified ({type(e).__name__}: {e})")
        return
    if error is None:
        print(f"reactions:write: ok (probed on {channel}/{ts})")
    elif error == "missing_scope":
        print("WARNING: reactions:write: missing (the bot token lacks the scope: add it under OAuth & Permissions at "
              "api.slack.com and reinstall the app; the working indicator stays off until then)")
    else:
        print(f"reactions:write: unverified ({error})")


# ----------------------------------------------------------------------------------------------- the service's poster and pid

def sdk_poster(client):
    """`chat.postMessage` over a slack_sdk WebClient (bolt's `App.client`), returning Slack's answer as a dict so
    the posting path learns the `ts` of a top-level post. Raises on failure (slack_sdk's SlackApiError)."""
    def post(channel, thread_ts, text):
        kw = {"channel": channel, "text": text}
        if thread_ts:
            kw["thread_ts"] = thread_ts
        res = client.chat_postMessage(**kw)
        data = getattr(res, "data", res)
        return data if isinstance(data, dict) else {}
    return post


def bridge_pid_path(home):
    return os.path.join(home, "logs", "bridge.pid")


def write_bridge_pid(home):
    S.write_text(bridge_pid_path(home), f"{os.getpid()}\n")


def bridge_alive(home):
    """True when a bridge service is running: its pid file names a live process."""
    pid = S.read_text(bridge_pid_path(home)).strip()
    return pid.isdigit() and S.pid_alive(int(pid))


# ----------------------------------------------------------------------------------------------- entry points

def check(home, probe=None):
    """`--check`: the allowlist, the credentials file, and the `reactions:write` scope (a warning at most).
    `probe(token, channel, ts)` replaces the slack_sdk probe in tests."""
    ok = True
    try:
        allow = load_allowlist(home)
        print(f"allowlist.json: {len(allow)} sender(s), {sum(1 for e in allow.values() if e['instructs'])} instruct")
    except (ValueError, json.JSONDecodeError) as e:
        print(f"allowlist.json: invalid ({e})")
        ok = False
    slack_env = os.path.join(home, "credentials", "slack.env")
    token = None
    if os.path.exists(slack_env):
        env = S.read_env_file(slack_env)
        keys = sorted(env)
        token = env.get("SLACK_BOT_TOKEN")
        print(f"credentials/slack.env: present ({', '.join(keys) if keys else 'empty'})")
    else:
        print("credentials/slack.env: missing")
        ok = False
    check_reactions_scope(home, token, probe=probe)
    return ok


class SocketHealth:
    """Bridge service liveness (requirement 1, loops/b7.md): healthy while `client.is_connected()` and either a
    Socket Mode envelope (`note_envelope`, installed as a raw `socket_mode_request_listener`, before Bolt's own
    app-level filtering) or a changed `client.current_session.last_ping_pong_time` has been observed within
    `timeout_s` seconds of monotonic elapsed time. An idle channel with fresh pongs stays healthy; outbound pings
    and Web API calls never count. A session replacement that happens to carry the same old pong timestamp cannot
    buy fresh activity repeatedly: only a *changed* value counts.

    Requirement 1, loops/b9.md: `envelope_count` and `pong_count` count those same two kinds of inbound activity
    (never outbound traffic), starting at 0 and never resetting while the process lives, exposed as integer
    attributes and by `counters()`."""

    def __init__(self, client, clock=time.monotonic, timeout_s=300):
        self.client = client
        self.clock = clock
        self.timeout_s = timeout_s
        self._last_activity = clock()
        self._last_pong_seen = self._current_pong()
        self.pong_count = 0
        self.envelope_count = 0

    def _current_pong(self):
        session = getattr(self.client, "current_session", None)
        return getattr(session, "last_ping_pong_time", None) if session is not None else None

    def note_envelope(self, *args):
        """A raw Socket Mode receipt listener: `(client, request)`, but any arguments are accepted and ignored."""
        self._last_activity = self.clock()
        self.envelope_count += 1

    def _note_pong_if_changed(self):
        current = self._current_pong()
        if current is not None and current != self._last_pong_seen:
            self._last_pong_seen = current
            self._last_activity = self.clock()
            self.pong_count += 1

    def check(self):
        """True when healthy. Never reconnects by itself."""
        if not self.client.is_connected():
            return False
        self._note_pong_if_changed()
        return (self.clock() - self._last_activity) < self.timeout_s

    def counters(self):
        return {"pong_count": self.pong_count, "envelope_count": self.envelope_count}


def _reconnect_or_die(client, timeout_s):
    """Force `client.connect_to_new_endpoint(force=True)` bounded to `timeout_s` seconds, in a daemon thread so a
    hung SDK call cannot block the watchdog past the bound. Exception, timeout, or a still-disconnected result is
    fatal (requirement 2/23, loops/b7.md: B4): raises so the caller's process exits nonzero. The installed SDK's
    `connect_to_new_endpoint(force=False)` only reconnects when already disconnected, so an established-but-
    silent connection (is_connected() still True) needs `force=True` to actually request a new endpoint at all."""
    outcome = {}

    def attempt():
        try:
            client.connect_to_new_endpoint(force=True)
        except Exception as e:  # noqa: BLE001 - captured across the thread boundary, re-raised below
            outcome["error"] = e
    t = threading.Thread(target=attempt, daemon=True, name="socket-reconnect")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        reason = f"reconnect timed out after {timeout_s}s"
        S.log(reason)
        raise TimeoutError(reason)
    if "error" in outcome:
        S.log(f"reconnect failed: {outcome['error']}")
        raise outcome["error"]
    if not client.is_connected():
        reason = "reconnect returned but the socket is still disconnected"
        S.log(reason)
        raise RuntimeError(reason)


def _verify_fresh_activity_or_die(health, clock, grace_s, stop):
    """A forced reconnect that returns connected is not proof of a working replacement (requirement 23, loops/
    b7.md: B4): the old session's pong cannot certify it, so this blocks for a genuinely new envelope or pong
    value before accepting the connection as healthy again, bounded by `grace_s` of the supplied monotonic
    `clock` -- never the injected `stop.wait`, whose caller-controlled clock jumps must not corrupt this bound.
    No fresh activity within the grace period is fatal, same as a failed reconnect itself."""
    baseline_activity, baseline_pong = health._last_activity, health._last_pong_seen
    deadline = clock() + grace_s
    while True:
        health._note_pong_if_changed()
        if health._last_activity != baseline_activity or health._last_pong_seen != baseline_pong:
            return
        if stop.is_set():
            return
        if clock() >= deadline:
            reason = "reconnected endpoint showed no fresh inbound activity within the grace period"
            S.log(reason)
            raise RuntimeError(reason)
        time.sleep(min(0.01, grace_s))


def _iso_utc(ts=None):
    """UTC ISO 8601 ending in `Z`, millisecond precision; `ts` a `time.time()`-style epoch, else now."""
    moment = dt.datetime.now(dt.timezone.utc) if ts is None else dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


_PROCESS_STARTED_AT = _iso_utc()  # fixed for the life of the process (requirement 3, loops/b9.md)


def health_snapshot(health, reconnect_count, poll_s, clock):
    """The telemetry-only snapshot of one poll's observable state (requirement 3, loops/b9.md): exactly the
    metadata keys below, nothing from Slack. `reconnect_count` and `poll_s` are owned by the caller
    (`run_socket_mode`); `clock` supplies both `monotonic` and the age of the last inbound activity."""
    now = clock()
    counts = health.counters()
    return {
        "at": _iso_utc(),
        "monotonic": now,
        "pid": os.getpid(),
        "process_started_at": _PROCESS_STARTED_AT,
        "connected": bool(health.client.is_connected()),
        "pong_count": counts["pong_count"],
        "envelope_count": counts["envelope_count"],
        "reconnect_count": reconnect_count,
        "last_activity_age_s": now - health._last_activity,
        "poll_s": poll_s,
    }


def write_health_observation(path, snapshot):
    """Atomic telemetry write (requirement 3/4, loops/b9.md): a sibling temporary file in `path`'s own directory,
    then `os.replace`, so a reader never sees a partial file and none is left behind by any failure -- a bad
    snapshot, an unwritable directory, or a failed replacement. Returns True or False; never raises."""
    try:
        text = json.dumps(snapshot)
    except (TypeError, ValueError):
        return False
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".bridge-health-", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
        tmp = None
        return True
    except Exception:
        return False
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.remove(tmp)


def run_socket_mode(handler, stop, clock=time.monotonic, poll_s=10, reconnect_timeout_s=30, observation_path=None):
    """The required testable seam (requirement 2, loops/b7.md): connects once, installs the raw receipt listener,
    then monitors `SocketHealth` in the calling thread (never a background one: "a background SystemExit alone is
    insufficient" means the process must actually fail from its own main thread) every `poll_s` seconds, at most
    `reconnect_timeout_s` seconds per recovery attempt plus the same bound again to verify fresh activity
    (requirement 23, loops/b7.md: B4). Disables the SDK's own competing auto-reconnect for this managed lifecycle.
    Raises (an exception, or lets one propagate) on failed recovery; it never calls `handler.start()`, which
    blocks forever instead of returning control to the caller.

    Requirement 2/3/4, loops/b9.md: `reconnect_count` accumulates recovery attempts across this invocation,
    counted before an attempt's outcome is known. With `observation_path` set, every poll iteration writes
    exactly one snapshot in that iteration's `finally`, after the health check and any recovery, through
    `health_snapshot` and `write_health_observation` by those module-level names (so a fatal recovery attempt is
    still counted in the last snapshot written). Snapshot construction and writing share one telemetry-only
    exception boundary: a failure there is logged at most once per failed-poll streak and never changes the
    health result, the recovery decision, or a recovery exception in flight."""
    client = handler.client
    client.auto_reconnect_enabled = False
    client.default_auto_reconnect_enabled = False
    health = SocketHealth(client, clock=clock, timeout_s=300)
    client.socket_mode_request_listeners.append(health.note_envelope)
    handler.connect()
    reconnect_count = 0
    failed_streak = False
    while not stop.is_set():
        try:
            if not health.check():
                reconnect_count += 1
                _reconnect_or_die(client, reconnect_timeout_s)
                _verify_fresh_activity_or_die(health, clock, reconnect_timeout_s, stop)
        finally:
            if observation_path is not None:
                try:
                    snapshot = health_snapshot(health, reconnect_count, poll_s, clock)
                    wrote = write_health_observation(observation_path, snapshot)
                except Exception:
                    wrote = False
                if wrote:
                    failed_streak = False
                elif not failed_streak:
                    S.log("health observation write failed; monitoring continues")
                    failed_streak = True
        if stop.wait(poll_s):
            return


def serve(home):
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    token_env = S.read_env_file(os.path.join(home, "credentials", "slack.env"))
    bot_token, app_token = token_env.get("SLACK_BOT_TOKEN"), token_env.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise SystemExit("credentials/slack.env needs SLACK_BOT_TOKEN and SLACK_APP_TOKEN")
    app = App(token=bot_token)
    bot_user_id = app.client.auth_test()["user_id"]
    poster = sdk_poster(app.client)

    def file_info(file_id):
        return app.client.files_info(file=file_id)["file"]

    bridge = Bridge(home, load_allowlist(home), poster, token_env, bot_user_id=bot_user_id,
                    allowlist_path=os.path.join(home, "allowlist.json"))

    @app.event("message")
    def _message(event):
        bridge.handle_message(event)

    @app.event("app_mention")
    def _mention(event):
        pass  # the same message arrives as a `message` event; handled once there

    @app.event("reaction_added")
    def _reaction(event):
        bridge.handle_reaction(event)

    @app.event("file_shared")
    def _file(event):
        bridge.handle_file(event, file_info=file_info)

    write_bridge_pid(home)
    stop = threading.Event()
    pump = threading.Thread(target=bridge.pump_outbox, args=(stop,), daemon=True, name="outbox")
    pump.start()
    handler = SocketModeHandler(app, app_token)
    S.log(f"bridge up as {bot_user_id}, home={home}")
    try:
        # connects once; raises on failed recovery (requirement 2, loops/b7.md); writes the idle-pong observation
        # file every poll (requirement 3, loops/b9.md)
        run_socket_mode(handler, stop, observation_path=os.path.join(home, "logs", "bridge-health.json"))
    finally:
        stop.set()
        pump.join(5)
        with contextlib.suppress(Exception):
            handler.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="the manager's Slack bridge")
    ap.add_argument("--check", action="store_true", help="validate allowlist.json and credentials/slack.env; exit 0/1")
    ap.add_argument("--home", default=None)
    args = ap.parse_args(argv)
    home = args.home or S.home_dir()
    if args.check:
        return 0 if check(home) else 1
    try:
        serve(home)
    except SystemExit as e:
        code = e.code
        return code if isinstance(code, int) and code != 0 else 1
    except Exception as e:
        S.log(f"bridge error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    S._reexec_into_venv()
    sys.exit(main())
