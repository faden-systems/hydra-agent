#!/usr/bin/env python3
"""Exit-owned acceptance for b4: threads reach the manager once joined and replies stay in-thread; compaction is
scheduled by recorded tokens, runs a [compaction] turn then /compact on the same session, is logged, and never
discards the session. Interfaces: Bridge(home, allowlist, poster, token_env) with handle_message(event) and
bot_id attribute; Supervisor(home, engines, poster, repo, clock=, buildlog_poster=); config.json compaction keys."""
import datetime as dt, json, os, subprocess, sys, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "manager"))
import supervisor as S  # noqa: E402
import bridge as B  # noqa: E402

T0 = dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=dt.timezone.utc); NOW = [T0]
def clock():
    NOW[0] = NOW[0] + dt.timedelta(seconds=1); return NOW[0]


class Poster:
    def __init__(self): self.posted = []
    def __call__(self, channel, thread_ts, text): self.posted.append((channel, thread_ts, text))


def home():
    h = tempfile.mkdtemp()
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror"): os.makedirs(os.path.join(h, d))
    for n in ("claude-r2d2", "claude-l"): open(os.path.join(h, "credentials", f"{n}.env"), "w").write(f"CLAUDE_CODE_OAUTH_TOKEN=fake-{n}\n")
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-fable-5-1"}))
    open(os.path.join(h, "session-id"), "w").write("sess-b4\n")
    open(os.path.join(h, "budgets.json"), "w").write(json.dumps({"turns_per_hour": 100, "claude_turns_per_day": {"claude-r2d2": 100, "claude-l": 100}}))
    open(os.path.join(h, "config.json"), "w").write(json.dumps({"compaction": {"threshold_tokens": 300000, "max_bytes": 50000000, "codex_every_turns": 25, "quiet_hours": [2, 5]}}))
    return h


def queued(h):
    p = os.path.join(h, "inbox", "events.jsonl")
    return [json.loads(l) for l in open(p) if l.strip()] if os.path.exists(p) else []


def fake_claude(dir_, tokens_line=None, fail_compact=False):
    """Recording fake claude: logs argv and stdin; on '/compact' exits 1 if fail_compact; prints a reply with a
    usage line the supervisor parses for input tokens when tokens_line is given."""
    p = os.path.join(dir_, "claude")
    open(p, "w").write("#!/usr/bin/env python3\nimport sys, os, json\nmsg=sys.stdin.read() if not sys.stdin.isatty() else ''\n"
                       f"open({dir_!r}+'/calls.jsonl','a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': msg}})+'\\n')\n"
                       f"if any(a=='/compact' for a in sys.argv[1:]):\n    sys.exit({1 if fail_compact else 0})\n"
                       "if '[compaction]' in msg:\n    print('compacted')\n    sys.exit(0)\n"
                       "print('REPLY: ok')\n" + (f"print({tokens_line!r})\n" if tokens_line else "") +
                       "print('---HANDOFF---')\nprint('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n")
    os.chmod(p, 0o755); return p


def calls(dir_): return [json.loads(l) for l in open(os.path.join(dir_, "calls.jsonl")) if l.strip()]


def main():
    # ---------- threads ----------
    h = home(); post = Poster()
    br = B.Bridge(home=h, allowlist={"U_FOUNDER": {"instructs": True}, "U_OP": {"instructs": False}}, poster=post, token_env={})
    br.bot_id = "U_BOT"
    # a root message mentioning the manager joins the thread (thread_ts = its own ts)
    br.handle_message({"channel": "C_DEV", "ts": "100.1", "user": "U_FOUNDER", "text": "<@U_BOT> assignee: manager | track: t1\nstart here"})
    q = queued(h); assert q and q[-1]["payload"]["thread_ts"] == "100.1", ("root message: thread_ts is its own ts", q[-1]["payload"])
    # three later replies in that thread, no mention, from founder and operator: all queued with thread_ts 100.1
    br.handle_message({"channel": "C_DEV", "ts": "100.2", "thread_ts": "100.1", "user": "U_FOUNDER", "text": "first follow-up"})
    br.handle_message({"channel": "C_DEV", "ts": "100.3", "thread_ts": "100.1", "user": "U_OP", "text": "done: step one"})
    br.handle_message({"channel": "C_DEV", "ts": "100.4", "thread_ts": "100.1", "user": "U_FOUNDER", "text": "third"})
    q = queued(h); ids = [e["id"] for e in q]
    assert "100.2" in ids and "100.3" in ids and "100.4" in ids, ("replies in a joined thread must be queued without a mention", ids)
    assert all(e["payload"]["thread_ts"] == "100.1" for e in q if e["id"] in ("100.2", "100.3", "100.4"))
    assert next(e for e in q if e["id"] == "100.3")["payload"]["instructs"] is False
    # a reply in a thread the manager has not joined: mirrored, not queued
    br.handle_message({"channel": "C_DEV", "ts": "200.2", "thread_ts": "200.1", "user": "U_FOUNDER", "text": "unrelated thread chatter"})
    assert "200.2" not in [e["id"] for e in queued(h)], "unjoined thread must not be queued"
    assert "unrelated thread chatter" in open(os.path.join(h, "mirror", "C_DEV.jsonl")).read()
    # the manager's reply goes into the thread, not top-level: run a turn and check the poster target
    cl_dir = tempfile.mkdtemp(); cl = fake_claude(cl_dir)
    engines = {"claude-r2d2": {"bin": cl, "cred": "claude-r2d2.env"}, "claude-l": {"bin": cl, "cred": "claude-l.env"}}
    sup = S.Supervisor(home=h, engines=engines, poster=post, clock=clock)
    assert sup.run_once() is True
    assert post.posted and post.posted[-1][0] == "C_DEV" and post.posted[-1][1] == "100.1", ("reply must target the thread", post.posted[-1][:2])
    mirror = [json.loads(l) for l in open(os.path.join(h, "mirror", "C_DEV.jsonl")) if l.strip()]
    own = [m for m in mirror if "REPLY: ok" in (m.get("text") or "")]; assert own and own[-1].get("thread_ts") == "100.1", "own reply mirrored with its thread_ts"
    # leave: founder only; afterwards replies are not queued
    br.handle_message({"channel": "C_DEV", "ts": "100.5", "thread_ts": "100.1", "user": "U_OP", "text": "<@U_BOT> leave"})
    assert "not authorized" in post.posted[-1][2].lower()
    br.handle_message({"channel": "C_DEV", "ts": "100.6", "thread_ts": "100.1", "user": "U_FOUNDER", "text": "<@U_BOT> leave"})
    br.handle_message({"channel": "C_DEV", "ts": "100.7", "thread_ts": "100.1", "user": "U_FOUNDER", "text": "after leave"})
    assert "100.7" not in [e["id"] for e in queued(h)], "after leave, thread replies are not queued"
    threads = json.load(open(os.path.join(h, "threads.json"))); assert "100.1" not in threads.get("C_DEV", {}), threads
    br.handle_message({"channel": "C_DEV", "ts": "100.8", "thread_ts": "100.1", "user": "U_FOUNDER", "text": "<@U_BOT> status"})
    assert "threads:" in post.posted[-1][2], post.posted[-1][2]
    print("1 ok: threads: join by mention, replies queued without mention, in-thread reply, unjoined ignored, leave, status")

    # ---------- compaction ----------
    h2 = home(); post2 = Poster(); blog = Poster()
    c2_dir = tempfile.mkdtemp(); c2 = fake_claude(c2_dir, tokens_line="[usage] input_tokens=350000")
    engines2 = {"claude-r2d2": {"bin": c2, "cred": "claude-r2d2.env"}, "claude-l": {"bin": c2, "cred": "claude-l.env"}}
    NOW[0] = dt.datetime(2026, 10, 2, 14, 0, 0, tzinfo=dt.timezone.utc)  # outside quiet hours; 350k > 300k by less than 25%, so it defers
    sup2 = S.Supervisor(home=h2, engines=engines2, poster=post2, clock=clock, buildlog_poster=blog)
    ev = {"id": str(uuid.uuid4()), "source": "slack", "at": time.time(), "payload": {"channel": "C_DEV", "thread_ts": "1.0", "user": "U_FOUNDER", "text": "work", "instructs": True}}
    open(os.path.join(h2, "inbox", "events.jsonl"), "a").write(json.dumps(ev) + "\n")
    assert sup2.run_once() is True
    turns = [json.loads(l) for l in open(os.path.join(h2, "logs", "turns.jsonl")) if l.strip()]
    assert turns[-1].get("input_tokens") == 350000, ("input tokens recorded from the engine's usage line", turns[-1])
    assert sup2.compaction_pending() is True, "350k over a 300k threshold must schedule a compaction"
    ev2 = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h2, "inbox", "events.jsonl"), "a").write(json.dumps(ev2) + "\n")
    assert sup2.run_once() is True
    argvs = [c["argv"] for c in calls(c2_dir)]
    assert not any("/compact" in a for a in argvs), "within 25% over threshold and outside quiet hours: deferred"
    NOW[0] = dt.datetime(2026, 10, 3, 3, 0, 0, tzinfo=dt.timezone.utc)  # quiet hours (02:00 to 05:00)
    ev3 = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h2, "inbox", "events.jsonl"), "a").write(json.dumps(ev3) + "\n")
    assert sup2.run_once() is True
    cs = calls(c2_dir)
    stdins = [c["stdin"] for c in cs]
    i_comp = next((i for i, s_ in enumerate(stdins) if "[compaction]" in s_), None)
    assert i_comp is not None, "a [compaction] turn must run in quiet hours"
    assert any("/compact" in c["argv"] for c in cs[i_comp + 1:]), "then /compact on the session"
    comp_call = next(c for c in cs[i_comp + 1:] if "/compact" in c["argv"])
    assert "--resume" in comp_call["argv"] and comp_call["argv"][comp_call["argv"].index("--resume") + 1] == "sess-b4", "same session id"
    ledger_p = os.path.join(h2, "manager-memory", "LEDGER.jsonl")
    assert os.path.exists(ledger_p) and any("compaction" in json.loads(l) for l in open(ledger_p) if l.strip()), "ledger must record the compaction"
    # immediate when far over threshold
    h3 = home(); c3_dir = tempfile.mkdtemp(); c3 = fake_claude(c3_dir, tokens_line="[usage] input_tokens=740000")
    engines3 = {"claude-r2d2": {"bin": c3, "cred": "claude-r2d2.env"}, "claude-l": {"bin": c3, "cred": "claude-l.env"}}
    NOW[0] = dt.datetime(2026, 10, 2, 15, 0, 0, tzinfo=dt.timezone.utc)
    sup3 = S.Supervisor(home=h3, engines=engines3, poster=Poster(), clock=clock, buildlog_poster=blog)
    for _ in range(2):
        e = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h3, "inbox", "events.jsonl"), "a").write(json.dumps(e) + "\n"); assert sup3.run_once() is True
    assert any("/compact" in c["argv"] for c in calls(c3_dir)), "more than 25% over threshold: compaction runs immediately before the next turn"
    # failure path: /compact fails -> one buildlog post, session id unchanged, turns continue
    h4 = home(); c4_dir = tempfile.mkdtemp(); c4 = fake_claude(c4_dir, tokens_line="[usage] input_tokens=740000", fail_compact=True)
    engines4 = {"claude-r2d2": {"bin": c4, "cred": "claude-r2d2.env"}, "claude-l": {"bin": c4, "cred": "claude-l.env"}}
    blog4 = Poster(); sup4 = S.Supervisor(home=h4, engines=engines4, poster=Poster(), clock=clock, buildlog_poster=blog4)
    for _ in range(3):
        e = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h4, "inbox", "events.jsonl"), "a").write(json.dumps(e) + "\n"); assert sup4.run_once() is True
    assert open(os.path.join(h4, "session-id")).read().strip() == "sess-b4", "a failed compaction must never discard the session"
    assert len(blog4.posted) >= 1 and "compaction" in blog4.posted[0][2].lower(), "one buildlog post on failure"
    # hydra compact forces it (CLI), founder-only in Slack
    r = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "compact"], env={**os.environ, "HYDRA_HOME": h}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and os.path.exists(os.path.join(h, "COMPACT")), "hydra compact must schedule a compaction (flag file)"
    br.handle_message({"channel": "C_DEV", "ts": "300.1", "user": "U_OP", "text": "<@U_BOT> compact"}); assert "not authorized" in post.posted[-1][2].lower()
    print("2 ok: compaction: tokens recorded, deferral within 25% outside quiet hours, run in quiet hours, immediate when far over, failure keeps the session, CLI and authority")
    print("b4 acceptance OK")


if __name__ == "__main__":
    main()
