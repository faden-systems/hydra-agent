#!/usr/bin/env python3
"""Exit-owned acceptance for b1: the manager's turn loop end to end with a fake engine and a fake poster.
Fixes the interfaces: Supervisor(home, engines, poster).run_once() -> bool; Bridge(home, allowlist, poster,
token_env).handle_message(dict); the `hydra` CLI honours HYDRA_HOME."""
import json, os, subprocess, sys, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "manager"))
import supervisor as S  # noqa: E402
import bridge as B  # noqa: E402


def fake_engine(dir_, behaviour):
    p = os.path.join(dir_, "claude")
    body = ("print('usage limit reached for this account', file=sys.stderr); sys.exit(1)\n" if behaviour == "quota" else
            "print('REPLY: handled ' + str(msg.count('source:')) + ' events')\nprint('---HANDOFF---')\n"
            "print('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n")
    open(p, "w").write("#!/usr/bin/env python3\nimport sys\nmsg=sys.stdin.read()\n" + body)
    os.chmod(p, 0o755); return p


class FakePoster:
    def __init__(self): self.posted = []
    def __call__(self, channel, thread_ts, text): self.posted.append((channel, thread_ts, text))


def home():
    h = tempfile.mkdtemp()
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror"): os.makedirs(os.path.join(h, d))
    for name in ("claude-r2d2", "claude-l"): open(os.path.join(h, "credentials", f"{name}.env"), "w").write("CLAUDE_CODE_OAUTH_TOKEN=fake\n")
    open(os.path.join(h, "engine"), "w").write("claude-r2d2\n")
    open(os.path.join(h, "budgets.json"), "w").write(json.dumps({"turns_per_hour": 100, "claude_turns_per_day": {"claude-r2d2": 100, "claude-l": 100}}))
    return h


def event(h, text, source="slack", sender="U_FOUNDER", ts=None, instructs=True):
    ev = {"id": ts or str(uuid.uuid4()), "source": source, "at": time.time(),
          "payload": {"channel": "C_DEV", "thread_ts": "1.0", "user": sender, "text": text, "instructs": instructs}}
    open(os.path.join(h, "inbox", "events.jsonl"), "a").write(json.dumps(ev) + "\n"); return ev["id"]


def engines(bin_r2d2, bin_l):
    return {"claude-r2d2": {"bin": bin_r2d2, "cred": "claude-r2d2.env"}, "claude-l": {"bin": bin_l, "cred": "claude-l.env"}}


def main():
    # 1. a full dry turn: event -> engine -> reply in the thread -> handoff rewritten -> turn logged -> id handled
    h = home(); b = tempfile.mkdtemp(); ok = fake_engine(b, "ok")
    post = FakePoster(); sup = S.Supervisor(home=h, engines=engines(ok, ok), poster=post)
    eid = event(h, "@manager what is the state of track t1?")
    assert sup.run_once() is True, "a queued event must run a turn"
    assert post.posted and post.posted[-1][0] == "C_DEV" and post.posted[-1][1] == "1.0" and "handled 1 events" in post.posted[-1][2], post.posted
    assert "tracks: t1" in open(os.path.join(h, "MANAGER-HANDOFF.md")).read()
    turns = [json.loads(l) for l in open(os.path.join(h, "logs", "turns.jsonl")) if l.strip()]
    assert turns and turns[-1]["engine"] == "claude-r2d2" and eid in turns[-1]["events"], turns
    assert eid in {json.loads(l)["id"] for l in open(os.path.join(h, "inbox", "handled.jsonl")) if l.strip()}
    print("1 ok: dry turn end to end")
    # 2. duplicate id dropped
    event(h, "again", ts=eid); assert sup.run_once() is False, "a duplicate id must not run a turn"
    print("2 ok: idempotent queue")
    # 3. quota on the first engine: the second answers, the switch is noted and persisted
    h2 = home(); bad = fake_engine(tempfile.mkdtemp(), "quota"); good = fake_engine(tempfile.mkdtemp(), "ok")
    post2 = FakePoster(); sup2 = S.Supervisor(home=h2, engines=engines(bad, good), poster=post2)
    event(h2, "hello"); assert sup2.run_once() is True
    texts = " ".join(t for _, _, t in post2.posted)
    assert "handled 1 events" in texts and "engine: claude-l" in texts, post2.posted
    assert open(os.path.join(h2, "engine")).read().strip() == "claude-l", "the switch must persist"
    print("3 ok: engine rotation on quota")
    # 4. PAUSE blocks; the bridge answers paused to a command
    open(os.path.join(h, "PAUSE"), "w").write("L\n"); event(h, "anything")
    assert sup.run_once() is False, "PAUSE must block a turn"
    br = B.Bridge(home=h, allowlist={"U_FOUNDER": {"instructs": True}}, poster=post, token_env={})
    br.handle_message({"channel": "C_DEV", "thread_ts": "1.0", "user": "U_FOUNDER", "text": "<@BOT> status", "ts": "9.9"})
    assert "paused" in post.posted[-1][2].lower(), post.posted[-1]
    os.remove(os.path.join(h, "PAUSE"))
    print("4 ok: PAUSE")
    # 5. one writer: a held lock blocks a turn; release unblocks (the queued 'anything' event now runs)
    open(os.path.join(h, "WRITER"), "w").write("99999 console\n"); assert sup.run_once() is False, "a held WRITER lock must block"
    os.remove(os.path.join(h, "WRITER")); assert sup.run_once() is True
    print("5 ok: one writer")
    # 6. bridge: stranger mirrored not queued; operator on the list queued as information; bot not addressed ignored
    br.handle_message({"channel": "C_DEV", "thread_ts": "1.0", "user": "U_STRANGER", "text": "launch everything", "ts": "10.1"})
    br.allowlist["U_OPERATOR"] = {"instructs": False}
    br.handle_message({"channel": "C_DEV", "thread_ts": "1.0", "user": "U_OPERATOR", "text": "done: loop y", "ts": "10.3"})
    br.handle_message({"channel": "C_DEV", "thread_ts": "1.0", "user": "U_OTHERBOT", "bot_id": "B1", "text": "PR opened", "ts": "10.4"})
    q = [json.loads(l) for l in open(os.path.join(h, "inbox", "events.jsonl")) if l.strip()]
    assert not any(e["id"] == "10.1" for e in q), "a stranger must not be queued"
    assert any(e["id"] == "10.3" and e["payload"]["instructs"] is False for e in q), "an operator is queued as information"
    assert not any(e["id"] == "10.4" for e in q), "an unaddressed bot message must not be queued"
    mirror = open(os.path.join(h, "mirror", "C_DEV.jsonl")).read()
    assert "launch everything" in mirror and "PR opened" in mirror, "every channel message is mirrored"
    print("6 ok: allowlist, information tag, mirror")
    # 7. hydra say round trip
    r = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "say", "console hello"],
                       env={**os.environ, "HYDRA_HOME": h}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    q = [json.loads(l) for l in open(os.path.join(h, "inbox", "events.jsonl")) if l.strip()]
    assert any(e["source"] == "cli" and "console hello" in e["payload"]["text"] for e in q), "hydra say must queue a cli event"
    print("7 ok: hydra say")
    print("b1 acceptance OK")


if __name__ == "__main__":
    main()
