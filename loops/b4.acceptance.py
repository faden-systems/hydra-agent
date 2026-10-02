#!/usr/bin/env python3
"""Exit-owned acceptance for b4: threads reach the manager once joined and replies stay in-thread; compaction is
scheduled by recorded tokens, runs a [compaction] memory turn then starts a new session, is logged, and never
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
    open(os.path.join(h, "config.json"), "w").write(json.dumps({"compaction": {"rollover_enabled": True, "threshold_tokens": 300000, "max_bytes": 50000000, "codex_every_turns": 25, "quiet_hours": [2, 5], "quiet_hours_tz": "UTC"}}))
    return h


def queued(h):
    p = os.path.join(h, "inbox", "events.jsonl")
    return [json.loads(l) for l in open(p) if l.strip()] if os.path.exists(p) else []


def fake_claude(dir_, tokens=None, fail_compact=False, after_tokens=40000, name="claude", flush_marker=None):
    """Recording engine. b7 amendment: flush writes memory and handoff; fresh session reduces context.
    fail_compact now fails the memory-writing turn, preserving old session/backoff coverage."""
    p = os.path.join(dir_, name)
    open(p, "w").write("#!/usr/bin/env python3\nimport sys, os, json\nmsg=sys.stdin.read() if not sys.stdin.isatty() else ''\n"
                       f"D={dir_!r}\n"
                       "open(D+'/calls.jsonl','a').write(json.dumps({'argv': sys.argv[1:], 'stdin': msg})+'\\n')\n"
                       "assert '/compact' not in sys.argv, 'obsolete compaction invocation'\n"
                       f"if '[compaction]' in msg:\n    if {fail_compact!r}: sys.exit(1)\n    from pathlib import Path\n    m=Path(os.environ['HYDRA_MEMORY_DIR'])/'MEMORY.md'; m.write_text(m.read_text()+'\\nfixture memory flushed\\n')\n    print('compacted\\n---HANDOFF---\\ntracks: t1\\nwaiting on: none\\nlast decision: flush\\nnext action: resume\\nopen question: none')\n    sys.exit(0)\n"
                       "if '--session-id' in sys.argv and sys.argv[sys.argv.index('--session-id')+1]!='sess-b4': open(D+'/compacted','w').write('1')\n"
                       + (f"if {flush_marker!r} and {flush_marker!r} in msg:\n    open(D+'/flushes','a').write('1\\n')\n" if flush_marker else "")
                       + "print('REPLY: ok')\n"
                       + (f"print('[usage] input_tokens=' + str({after_tokens} if os.path.exists(D+'/compacted') else {tokens}))\n" if tokens else "")
                       + "print('---HANDOFF---')\nprint('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n")
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
    c2_dir = tempfile.mkdtemp(); c2 = fake_claude(c2_dir, tokens=350000)
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
    assert not any("[compaction]" in c["stdin"] for c in calls(c2_dir)), "within 25% over threshold and outside quiet hours: deferred"
    NOW[0] = dt.datetime(2026, 10, 3, 3, 0, 0, tzinfo=dt.timezone.utc)  # quiet hours (02:00 to 05:00)
    ev3 = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h2, "inbox", "events.jsonl"), "a").write(json.dumps(ev3) + "\n")
    assert sup2.run_once() is True
    cs = calls(c2_dir)
    stdins = [c["stdin"] for c in cs]
    i_comp = next((i for i, s_ in enumerate(stdins) if "[compaction]" in s_), None)
    assert i_comp is not None, "a [compaction] turn must run in quiet hours"
    assert not any('/compact' in c['argv'] for c in cs)
    new_sid=open(os.path.join(h2,'session-id')).read().strip()
    assert new_sid != 'sess-b4', 'rollover must select a new session'
    ev4 = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h2, "inbox", "events.jsonl"), "a").write(json.dumps(ev4) + "\n")
    assert sup2.run_once() is True  # the turn after compaction reports the reduced count
    ledger_p = os.path.join(h2, "manager-memory", "LEDGER.jsonl")
    comps = [json.loads(l)["compaction"] for l in open(ledger_p) if l.strip() and "compaction" in json.loads(l)]
    assert comps, "ledger must record the compaction"
    c = comps[-1]; assert c["before_tokens"] == 350000 and c["after_tokens"] == 40000 and c["ok"] is True and c["at"], ("real reduction recorded", c)
    turns2 = [json.loads(l) for l in open(os.path.join(h2, "logs", "turns.jsonl")) if l.strip()]
    assert turns2[-1]["input_tokens"] == 40000 and sup2.compaction_pending() is False, "after a good compaction nothing is pending"
    # immediate when far over threshold
    h3 = home(); c3_dir = tempfile.mkdtemp(); c3 = fake_claude(c3_dir, tokens=740000)
    engines3 = {"claude-r2d2": {"bin": c3, "cred": "claude-r2d2.env"}, "claude-l": {"bin": c3, "cred": "claude-l.env"}}
    NOW[0] = dt.datetime(2026, 10, 2, 15, 0, 0, tzinfo=dt.timezone.utc)
    sup3 = S.Supervisor(home=h3, engines=engines3, poster=Poster(), clock=clock, buildlog_poster=blog)
    for _ in range(2):
        e = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h3, "inbox", "events.jsonl"), "a").write(json.dumps(e) + "\n"); assert sup3.run_once() is True
    assert any("[compaction]" in c["stdin"] for c in calls(c3_dir)), "more than 25% over threshold: compaction runs immediately before the next turn"
    # failure path: memory flush fails -> one buildlog post, session id unchanged, turns continue
    h4 = home(); c4_dir = tempfile.mkdtemp(); c4 = fake_claude(c4_dir, tokens=740000, fail_compact=True)
    engines4 = {"claude-r2d2": {"bin": c4, "cred": "claude-r2d2.env"}, "claude-l": {"bin": c4, "cred": "claude-l.env"}}
    blog4 = Poster(); sup4 = S.Supervisor(home=h4, engines=engines4, poster=Poster(), clock=clock, buildlog_poster=blog4)
    for _ in range(5):
        e = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h4, "inbox", "events.jsonl"), "a").write(json.dumps(e) + "\n"); assert sup4.run_once() is True
    assert open(os.path.join(h4, "session-id")).read().strip() == "sess-b4", "a failed compaction must never discard the session"
    assert len(blog4.posted) == 1 and "compaction" in blog4.posted[0][2].lower(), ("exactly one buildlog post across repeated failures", blog4.posted)
    assert sum(1 for c in calls(c4_dir) if "[compaction]" in c["stdin"]) >= 1
    assert not any("/compact" in c["argv"] for c in calls(c4_dir))
    # hydra compact forces it (CLI), founder-only in Slack
    r = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "compact"], env={**os.environ, "HYDRA_HOME": h}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and os.path.exists(os.path.join(h, "COMPACT")), "hydra compact must schedule a compaction (flag file)"
    br.handle_message({"channel": "C_DEV", "ts": "300.1", "user": "U_OP", "text": "<@U_BOT> compact"}); assert "not authorized" in post.posted[-1][2].lower()
    br.handle_message({"channel": "C_DEV", "ts": "300.2", "user": "U_FOUNDER", "text": "<@U_BOT> compact"}); assert os.path.exists(os.path.join(h, "COMPACT"))
    print("2 ok: compaction: tokens recorded, deferral, quiet hours, real reduction, immediate when far over, failure keeps the session with one post, CLI and authority")

    # ---------- coverage: joins by own post and by assignee line, prune, bytes trigger, codex flush ----------
    h5 = home(); post5 = Poster()
    br5 = B.Bridge(home=h5, allowlist={"U_FOUNDER": {"instructs": True}, "U_OP": {"instructs": False}}, poster=post5, token_env={}); br5.bot_id = "U_BOT"
    br5.handle_message({"channel": "C_DEV", "ts": "400.1", "user": "U_FOUNDER", "text": "assignee: manager | track: t5\nplease plan"})  # no mention: assignee line joins
    assert "400.1" in [e["id"] for e in queued(h5)], "an assignee line naming the manager joins and queues"
    br5.note_own_post("C_DEV", "500.1")  # the bridge records the manager's own post as a join
    br5.handle_message({"channel": "C_DEV", "ts": "500.2", "thread_ts": "500.1", "user": "U_FOUNDER", "text": "reply to the manager's post"})
    assert "500.2" in [e["id"] for e in queued(h5)], "a thread the manager posted in is joined"
    th = json.load(open(os.path.join(h5, "threads.json"))); th["C_DEV"]["400.1"]["last_seen"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=15)).isoformat()
    json.dump(th, open(os.path.join(h5, "threads.json"), "w")); br5.prune()
    th = json.load(open(os.path.join(h5, "threads.json"))); assert "400.1" not in th.get("C_DEV", {}) and "500.1" in th.get("C_DEV", {}), "14-day prune drops stale threads only"
    # bytes trigger
    h6 = home(); open(os.path.join(h6, "config.json"), "w").write(json.dumps({"compaction": {"rollover_enabled": True, "threshold_tokens": 300000, "max_bytes": 1000, "codex_every_turns": 25, "quiet_hours": [2, 5], "quiet_hours_tz": "UTC"}}))
    sd = os.path.join(h6, ".claude", "projects", h6.replace("/", "-")); os.makedirs(sd); open(os.path.join(sd, "sess-b4.jsonl"), "w").write("x" * 5000)
    c6_dir = tempfile.mkdtemp(); c6 = fake_claude(c6_dir, tokens=1000)
    sup6 = S.Supervisor(home=h6, engines={"claude-r2d2": {"bin": c6, "cred": "claude-r2d2.env"}, "claude-l": {"bin": c6, "cred": "claude-l.env"}}, poster=Poster(), clock=clock, buildlog_poster=Poster())
    e = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h6, "inbox", "events.jsonl"), "a").write(json.dumps(e) + "\n"); assert sup6.run_once() is True
    assert sup6.compaction_pending() is True, "a session file over max_bytes schedules a compaction"
    # codex periodic flush
    h7 = home(); open(os.path.join(h7, "config.json"), "w").write(json.dumps({"compaction": {"rollover_enabled": True, "threshold_tokens": 300000, "max_bytes": 50000000, "codex_every_turns": 2, "quiet_hours": [2, 5], "quiet_hours_tz": "UTC"}}))
    open(os.path.join(h7, "engine"), "w").write(json.dumps({"acc": "codex", "model": "gpt-6-astra"}))
    cx_dir = tempfile.mkdtemp(); cx = fake_claude(cx_dir, name="codex", flush_marker="MEMORY.md")
    sup7 = S.Supervisor(home=h7, engines={"claude-r2d2": {"bin": c6, "cred": "claude-r2d2.env"}, "claude-l": {"bin": c6, "cred": "claude-l.env"}, "codex": {"bin": cx, "cred": None}}, poster=Poster(), clock=clock, buildlog_poster=Poster())
    for _ in range(4):
        e = dict(ev, id=str(uuid.uuid4())); open(os.path.join(h7, "inbox", "events.jsonl"), "a").write(json.dumps(e) + "\n"); assert sup7.run_once() is True
    flush_turns = [c for c in calls(cx_dir) if "[flush]" in c["stdin"]]
    assert len(flush_turns) == 2, ("with codex_every_turns=2, four codex turns carry two [flush] requests", len(flush_turns))
    print("3 ok: joins by assignee line and own post, prune, bytes trigger, codex periodic flush")
    print("b4 acceptance OK")


if __name__ == "__main__":
    main()
