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
"""
import argparse
import json
import os
import re
import sys
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
SKIPPED_SUBTYPES = {"message_changed", "message_deleted", "channel_join", "channel_leave", "channel_topic",
                    "channel_purpose", "channel_name", "group_join", "group_leave"}


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
        """One Slack `message` event (dict). Mirrors it; queues it for allowlisted senders; answers commands."""
        self._maybe_reload_allowlist()
        self.maybe_prune()  # the 14-day rule, daily
        channel = ev.get("channel")
        subtype = ev.get("subtype")
        if subtype in SKIPPED_SUBTYPES:
            return None
        text = ev.get("text") or ""
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
        addressed, rest = self.parse_mention(text)
        if is_bot and not addressed:
            return None  # another bot talking to the channel, not to us
        thread_ts = ev.get("thread_ts") or ts  # a thread root the manager answers starts its own thread
        if addressed:
            m = COMMAND_RE.match(rest)
            if m:
                if m.group(1).split()[0].lower() != "leave":
                    self.join(channel, thread_ts)  # a mention joins the thread, except the one that leaves it
                return self.command(m, sender, entry, channel, thread_ts)
        if addressed or self.assigned_to_manager(text):
            self.join(channel, thread_ts)
        elif self.joined(channel, thread_ts):
            self.join(channel, thread_ts)  # touches last_seen
        else:
            return None  # an unjoined thread, or a top-level post without a mention: mirrored only
        event = S.append_event(self.home, S.new_event(
            "slack", {"channel": channel, "ts": ts, "thread_ts": thread_ts, "user": sender, "text": text,
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
                self.reply(channel, thread_ts, f"engine: {S.engine_label(current)}")
            else:
                try:
                    pair = S.parse_engine_command(target, current=current)
                except S.BadEngine as e:
                    self.reply(channel, thread_ts, f"{e}; nothing changed (engine: {S.engine_label(current)})")
                    return {"command": word, "ok": False}
                S.set_engine(self.home, pair["acc"], pair["model"])
                self.reply(channel, thread_ts, f"engine: {S.engine_label(pair)}")
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


def own_last_message(home):
    """(channel, ts) of the latest post the supervisor mirrored with a Slack ts, or None (dry posts carry none)."""
    best = None
    mirror = os.path.join(home, "mirror")
    for name in sorted(os.listdir(mirror)) if os.path.isdir(mirror) else []:
        if not name.endswith(".jsonl"):
            continue
        channel = name[:-len(".jsonl")]
        for rec in S.read_jsonl(os.path.join(mirror, name)):
            if rec.get("subtype") != "manager_reply" or not rec.get("ts"):
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


def serve(home):
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
    token_env = S.read_env_file(os.path.join(home, "credentials", "slack.env"))
    bot_token, app_token = token_env.get("SLACK_BOT_TOKEN"), token_env.get("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        raise SystemExit("credentials/slack.env needs SLACK_BOT_TOKEN and SLACK_APP_TOKEN")
    app = App(token=bot_token)
    bot_user_id = app.client.auth_test()["user_id"]

    def poster(channel, thread_ts, text):
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)

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

    S.log(f"bridge up as {bot_user_id}, home={home}")
    SocketModeHandler(app, app_token).start()


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
    except Exception as e:
        S.log(f"bridge error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    S._reexec_into_venv()
    sys.exit(main())
