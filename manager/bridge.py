#!/usr/bin/env python3
"""The Slack bridge: `@manager` in Slack, Socket Mode, no public endpoint.

`Bridge(home, allowlist, poster, token_env)` is testable without a client: `handle_message(event)`,
`handle_file(event)`, `handle_reaction(event)`. Every message in a channel the bot is in is mirrored to
`$HYDRA_HOME/mirror/<channel>.jsonl`; messages from allowlisted senders are queued for the supervisor; five commands
(`status`, `pause`, `resume`, `engine [acc=<account>] [model=<alias>]`, `digest now`) are answered without a turn.

Entry points: `bridge.py --check` validates `allowlist.json` and the presence of `credentials/slack.env` without
connecting; no argument runs the Socket Mode service (systemd).
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
COMMAND_RE = re.compile(r"^(status|pause|resume|digest now|engine(?:\s+(.+?))?)\s*$", re.IGNORECASE)
INSTRUCT_COMMANDS = ("pause", "resume", "engine", "digest")
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

    # ---- helpers
    def path(self, *parts):
        return os.path.join(self.home, *parts)

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
        thread_ts = ev.get("thread_ts") or ts
        if addressed:
            m = COMMAND_RE.match(rest)
            if m:
                return self.command(m, sender, entry, channel, thread_ts)
        event = S.append_event(self.home, S.new_event(
            "slack", {"channel": channel, "thread_ts": thread_ts, "user": sender, "text": text,
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

    # ---- the five commands
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
        return {"command": word, "ok": True}


# ----------------------------------------------------------------------------------------------- entry points

def check(home):
    ok = True
    try:
        allow = load_allowlist(home)
        print(f"allowlist.json: {len(allow)} sender(s), {sum(1 for e in allow.values() if e['instructs'])} instruct")
    except (ValueError, json.JSONDecodeError) as e:
        print(f"allowlist.json: invalid ({e})")
        ok = False
    slack_env = os.path.join(home, "credentials", "slack.env")
    if os.path.exists(slack_env):
        keys = sorted(S.read_env_file(slack_env))
        print(f"credentials/slack.env: present ({', '.join(keys) if keys else 'empty'})")
    else:
        print("credentials/slack.env: missing")
        ok = False
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
