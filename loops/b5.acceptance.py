#!/usr/bin/env python3
"""Exit-owned acceptance for b5: the working indicator. Supervisor(..., reactor=) with add/remove(channel, ts, name)."""
import datetime as dt, json, os, sys, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "manager"))
import supervisor as S  # noqa: E402

T0 = dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=dt.timezone.utc); NOW = [T0]
def clock():
    NOW[0] = NOW[0] + dt.timedelta(seconds=1); return NOW[0]


class Recorder:
    def __init__(self, fail_add=False): self.calls = []; self.fail_add = fail_add
    def add(self, channel, ts, name):
        self.calls.append(("add", channel, ts, name, time.monotonic()))
        if self.fail_add: raise RuntimeError("missing_scope")
    def remove(self, channel, ts, name): self.calls.append(("remove", channel, ts, name, time.monotonic()))


class Poster:
    def __init__(self, fail_first=False): self.posted = []; self.fail_first = fail_first; self.t = []
    def __call__(self, channel, thread_ts, text):
        if self.fail_first: self.fail_first = False; raise RuntimeError("slack down")
        self.posted.append((channel, thread_ts, text)); self.t.append(time.monotonic())


def fake_engine(dir_, behaviour="ok"):
    p = os.path.join(dir_, "claude")
    body = ("print('usage limit reached', file=sys.stderr); sys.exit(1)\n" if behaviour == "quota" else
            f"open({dir_!r}+'/ran','a').write(str(__import__('time').monotonic())+'\\n')\nprint('REPLY: ok')\nprint('---HANDOFF---')\nprint('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n")
    open(p, "w").write("#!/usr/bin/env python3\nimport sys\nmsg=sys.stdin.read()\n" + body); os.chmod(p, 0o755); return p


def home():
    h = tempfile.mkdtemp()
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror"): os.makedirs(os.path.join(h, d))
    for n in ("claude-r2d2", "claude-l"): open(os.path.join(h, "credentials", f"{n}.env"), "w").write(f"CLAUDE_CODE_OAUTH_TOKEN=fake-{n}\n")
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-fable-5-1"}))
    open(os.path.join(h, "session-id"), "w").write("sess-b5\n")
    open(os.path.join(h, "budgets.json"), "w").write(json.dumps({"turns_per_hour": 100, "claude_turns_per_day": {"claude-r2d2": 100, "claude-l": 100}}))
    return h


def event(h, text, source="slack", ts=None):
    ev = {"id": ts or str(uuid.uuid4()), "source": source, "at": time.time(), "payload": {"channel": "C_DEV", "thread_ts": "1.0", "ts": ts or str(uuid.uuid4()), "user": "U_FOUNDER", "text": text, "instructs": True}}
    open(os.path.join(h, "inbox", "events.jsonl"), "a").write(json.dumps(ev) + "\n"); return ev["payload"]["ts"]


def engines(b): return {"claude-r2d2": {"bin": b, "cred": "claude-r2d2.env"}, "claude-l": {"bin": b, "cred": "claude-l.env"}}


def main():
    # 1. two events in one batch: add eyes for both before the engine runs, remove after the reply is posted
    h = home(); d = tempfile.mkdtemp(); ok = fake_engine(d); rec = Recorder(); post = Poster()
    sup = S.Supervisor(home=h, engines=engines(ok), poster=post, clock=clock, reactor=rec)
    t1 = event(h, "one", ts="10.1"); t2 = event(h, "two", ts="10.2"); assert sup.run_once() is True
    adds = [c for c in rec.calls if c[0] == "add"]; rems = [c for c in rec.calls if c[0] == "remove"]
    assert {c[2] for c in adds} == {"10.1", "10.2"} and all(c[3] == "eyes" for c in adds), adds
    ran = float(open(os.path.join(d, "ran")).read().split()[0])
    assert all(c[4] < ran for c in adds), "reactions must be added before the engine runs"
    assert {c[2] for c in rems} == {"10.1", "10.2"} and all(c[4] >= post.t[-1] for c in rems), "removed after the reply is posted"
    turns = [json.loads(l) for l in open(os.path.join(h, "logs", "turns.jsonl")) if l.strip()]
    assert set(turns[-1].get("reacted", [])) == {"10.1", "10.2"}, turns[-1]
    print("1 ok: add before engine, remove after delivery, for both events")
    # 2. delivery fails once: the reaction stays until the pending reply is delivered on the next run
    h2 = home(); d2 = tempfile.mkdtemp(); ok2 = fake_engine(d2); rec2 = Recorder(); post2 = Poster(fail_first=True)
    sup2 = S.Supervisor(home=h2, engines=engines(ok2), poster=post2, clock=clock, reactor=rec2)
    event(h2, "flaky", ts="20.1"); assert sup2.run_once() is True
    assert not [c for c in rec2.calls if c[0] == "remove"], "reaction must stay while the reply is pending"
    assert sup2.run_once() is True  # delivers the pending reply
    assert [c for c in rec2.calls if c[0] == "remove" and c[2] == "20.1"], "removed once the pending reply is delivered"
    print("2 ok: stays while pending, removed on delivery")
    # 3. all engines fail: eyes replaced by x; a later successful turn clears it
    h3 = home(); d3 = tempfile.mkdtemp(); bad = fake_engine(d3, "quota"); rec3 = Recorder(); post3 = Poster()
    sup3 = S.Supervisor(home=h3, engines=engines(bad), poster=post3, clock=clock, reactor=rec3)
    event(h3, "doomed", ts="30.1"); sup3.run_once()
    names = [(c[0], c[3]) for c in rec3.calls if c[2] == "30.1"]
    assert ("add", "eyes") in names and ("remove", "eyes") in names and ("add", "x") in names, names
    good = fake_engine(tempfile.mkdtemp()); sup3.engines = engines(good)
    assert sup3.run_once() is True, "the still-pending event is handled by a working engine"
    assert ("remove", "x") in [(c[0], c[3]) for c in rec3.calls if c[2] == "30.1"], "x cleared once handled"
    print("3 ok: x on failure, cleared when handled")
    # 4. add() raising must not block the turn; timer events get no reactions
    h4 = home(); d4 = tempfile.mkdtemp(); ok4 = fake_engine(d4); rec4 = Recorder(fail_add=True); post4 = Poster()
    sup4 = S.Supervisor(home=h4, engines=engines(ok4), poster=post4, clock=clock, reactor=rec4)
    event(h4, "no scope", ts="40.1"); assert sup4.run_once() is True and post4.posted, "a failing reaction must not block the reply"
    h5 = home(); d5 = tempfile.mkdtemp(); ok5 = fake_engine(d5); rec5 = Recorder(); sup5 = S.Supervisor(home=h5, engines=engines(ok5), poster=Poster(), clock=clock, reactor=rec5)
    ev = {"id": str(uuid.uuid4()), "source": "timer", "at": time.time(), "payload": {"digest": False}}
    open(os.path.join(h5, "inbox", "events.jsonl"), "a").write(json.dumps(ev) + "\n"); sup5.run_once()
    assert not rec5.calls, "timer events have no message to react to"
    print("4 ok: add failure tolerated; no reactions for timer events")
    print("b5 acceptance OK")


if __name__ == "__main__":
    main()
