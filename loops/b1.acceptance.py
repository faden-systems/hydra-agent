#!/usr/bin/env python3
"""Exit-owned acceptance for b1: the manager's turn loop end to end with a fake engine and a fake poster.
Fixes the interfaces: Supervisor(home, engines, poster).run_once() -> bool; Bridge(home, allowlist, poster,
token_env).handle_message(dict); the `hydra` CLI honours HYDRA_HOME."""
import json, os, subprocess, sys, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "manager"))
import supervisor as S  # noqa: E402
import bridge as B  # noqa: E402


def fake_engine(dir_, behaviour, name="claude"):
    """A recording fake engine: writes argv, env and stdin to <dir>/calls.jsonl, then behaves."""
    p = os.path.join(dir_, name)
    body = ("print('usage limit reached for this account', file=sys.stderr); sys.exit(1)\n" if behaviour == "quota" else
            "print('REPLY: handled ' + str(msg.count('source:')) + ' events')\nprint('---HANDOFF---')\n"
            "print('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n")
    open(p, "w").write("#!/usr/bin/env python3\nimport sys, os, json\nmsg=sys.stdin.read()\n"
                       f"open({dir_!r} + '/calls.jsonl','a').write(json.dumps({{'argv': sys.argv[1:], 'env': {{k: v for k, v in os.environ.items() if k.startswith('CLAUDE') or k.startswith('CODEX')}}, 'stdin': msg}}) + '\\n')\n" + body)
    os.chmod(p, 0o755); return p


def calls(dir_):
    return [json.loads(l) for l in open(os.path.join(dir_, "calls.jsonl")) if l.strip()]


class FakePoster:
    def __init__(self): self.posted = []
    def __call__(self, channel, thread_ts, text): self.posted.append((channel, thread_ts, text))


def home():
    h = tempfile.mkdtemp()
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror"): os.makedirs(os.path.join(h, d))
    for name in ("claude-r2d2", "claude-l"): open(os.path.join(h, "credentials", f"{name}.env"), "w").write(f"CLAUDE_CODE_OAUTH_TOKEN=fake-{name}\n")
    open(os.path.join(h, "engine"), "w").write("claude-r2d2\n")
    open(os.path.join(h, "session-id"), "w").write("sess-0001\n")
    open(os.path.join(h, "budgets.json"), "w").write(json.dumps({"turns_per_hour": 100, "claude_turns_per_day": {"claude-r2d2": 100, "claude-l": 100}}))
    return h


def event(h, text, source="slack", sender="U_FOUNDER", ts=None, instructs=True):
    ev = {"id": ts or str(uuid.uuid4()), "source": source, "at": time.time(),
          "payload": {"channel": "C_DEV", "thread_ts": "1.0", "user": sender, "text": text, "instructs": instructs}}
    open(os.path.join(h, "inbox", "events.jsonl"), "a").write(json.dumps(ev) + "\n"); return ev["id"]


def engines(bin_r2d2, bin_l, bin_codex=None):
    e = {"claude-r2d2": {"bin": bin_r2d2, "cred": "claude-r2d2.env"}, "claude-l": {"bin": bin_l, "cred": "claude-l.env"}}
    if bin_codex: e["codex"] = {"bin": bin_codex, "cred": None}
    return e


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
    c = calls(b)[-1]
    assert "--resume" in c["argv"] and c["argv"][c["argv"].index("--resume") + 1] == "sess-0001", ("engine must resume the manager session", c["argv"])
    assert "--model" in c["argv"] and c["argv"][c["argv"].index("--model") + 1] == "claude-fable-5-1", c["argv"]
    assert c["env"].get("CLAUDE_CONFIG_DIR") == os.path.join(h, ".claude"), ("config dir", c["env"])
    assert c["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == "fake-claude-r2d2", ("token from the chosen credential file", c["env"])
    print("1 ok: dry turn end to end, engine contract")
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
    assert calls(os.path.dirname(good))[-1]["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == "fake-claude-l", "the second engine must run with its own token"
    # both Claude engines out: Codex takes the turn with the handoff and state on stdin
    h3 = home(); bad1 = fake_engine(tempfile.mkdtemp(), "quota"); bad2 = fake_engine(tempfile.mkdtemp(), "quota"); cdx_dir = tempfile.mkdtemp(); cdx = fake_engine(cdx_dir, "ok", name="codex")
    open(os.path.join(h3, "MANAGER-HANDOFF.md"), "w").write("tracks: t9\nwaiting on: L\n"); os.makedirs(os.path.join(h3, "repo", "factory")); open(os.path.join(h3, "state.json"), "w").write('{"tracks": {"t9": "review"}}')
    post3 = FakePoster(); sup3 = S.Supervisor(home=h3, engines=engines(bad1, bad2, cdx), poster=post3)
    event(h3, "still there?"); assert sup3.run_once() is True
    c3 = calls(cdx_dir)[-1]; assert "tracks: t9" in c3["stdin"] and '"t9"' in c3["stdin"], ("codex must receive the handoff and the state", c3["stdin"][:200])
    assert "engine: codex" in " ".join(t for _, _, t in post3.posted) and open(os.path.join(h3, "engine")).read().strip() == "codex"
    print("3 ok: engine rotation on quota, codex fallback with handoff + state")
    # 4. PAUSE blocks; the bridge answers paused to a command
    open(os.path.join(h, "PAUSE"), "w").write("L\n"); event(h, "anything")
    assert sup.run_once() is False, "PAUSE must block a turn"
    br = B.Bridge(home=h, allowlist={"U_FOUNDER": {"instructs": True}}, poster=post, token_env={})
    br.handle_message({"channel": "C_DEV", "thread_ts": "1.0", "user": "U_FOUNDER", "text": "<@BOT> status", "ts": "9.9"})
    assert "paused" in post.posted[-1][2].lower(), post.posted[-1]
    os.remove(os.path.join(h, "PAUSE"))
    print("4 ok: PAUSE")
    # 5. one writer: a live holder in another process blocks; a stale (dead pid) lock is reclaimed; the context
    #    manager releases on exceptions; acquisition is atomic (O_EXCL)
    holder = subprocess.Popen([sys.executable, "-c", f"import sys, time; sys.path.insert(0, {os.path.join(ROOT, 'manager')!r}); import supervisor as S\n"
                               f"with S.acquire_writer({h!r}, 'console'):\n    print('held', flush=True); time.sleep(8)"], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    assert sup.run_once() is False, "a live holder in another process must block"
    try:
        with S.acquire_writer(h, "supervisor"):
            raise AssertionError("second acquisition must fail while held")
    except S.WriterHeld:
        pass
    holder.wait(timeout=15)
    assert not os.path.exists(os.path.join(h, "WRITER")), "the holder must remove the lock on exit"
    open(os.path.join(h, "WRITER"), "w").write("999999 console\n")  # stale: no such pid
    assert sup.run_once() is True, "a stale lock must be reclaimed and the queued event run"
    try:
        with S.acquire_writer(h, "x"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not os.path.exists(os.path.join(h, "WRITER")), "the lock must be released on exceptions"
    print("5 ok: one writer, atomic, stale reclaim, cleanup")
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
    # command authority: an information-only sender may ask status but may not pause; the engine file is untouched
    before = open(os.path.join(h, "engine")).read()
    br.handle_message({"channel": "C_DEV", "thread_ts": "1.0", "user": "U_OPERATOR", "text": "<@BOT> pause", "ts": "10.5"})
    assert "not authorized" in post.posted[-1][2].lower() and not os.path.exists(os.path.join(h, "PAUSE")), post.posted[-1]
    br.handle_message({"channel": "C_DEV", "thread_ts": "1.0", "user": "U_OPERATOR", "text": "<@BOT> engine codex", "ts": "10.6"})
    assert open(os.path.join(h, "engine")).read() == before, "an information-only sender must not switch engines"
    br.handle_message({"channel": "C_DEV", "thread_ts": "1.0", "user": "U_OPERATOR", "text": "<@BOT> status", "ts": "10.7"})
    assert "engine" in post.posted[-1][2].lower(), "status is answered for any allowlisted sender"
    print("6 ok: allowlist, information tag, mirror, command authority")
    # 7. hydra say round trip
    r = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "say", "console hello"],
                       env={**os.environ, "HYDRA_HOME": h}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    q = [json.loads(l) for l in open(os.path.join(h, "inbox", "events.jsonl")) if l.strip()]
    assert any(e["source"] == "cli" and "console hello" in e["payload"]["text"] for e in q), "hydra say must queue a cli event"
    print("7 ok: hydra say")
    # 8. persistence: mirror and state land in the repo and reach the remote
    bare = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    repo = tempfile.mkdtemp(); subprocess.run(["git", "clone", "-q", bare, repo], check=True)
    gitenv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    os.makedirs(os.path.join(repo, "factory")); open(os.path.join(repo, "factory", "state.json"), "w").write("{}")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True); subprocess.run(["git", "-C", repo, "commit", "-qm", "init"], check=True, env=gitenv); subprocess.run(["git", "-C", repo, "push", "-q", "-u", "origin", "HEAD"], check=True)
    h8 = home(); b8 = tempfile.mkdtemp(); ok8 = fake_engine(b8, "ok")
    open(os.path.join(h8, "mirror", "C_DEV.jsonl"), "w").write(json.dumps({"ts": "1.0", "user": "U_FOUNDER", "text": "hello"}) + "\n")
    open(os.path.join(h8, "state.json"), "w").write('{"tracks": {"t1": "building"}}')
    post8 = FakePoster(); sup8 = S.Supervisor(home=h8, engines=engines(ok8, ok8), poster=post8, repo=repo)
    event(h8, "persist please"); os.environ.update({k: v for k, v in gitenv.items() if k.startswith("GIT_")})
    assert sup8.run_once() is True
    remote_log = subprocess.run(["git", "--git-dir", bare, "log", "--format=%s", "-n", "1"], capture_output=True, text=True).stdout.strip()
    assert remote_log.startswith("manager: turn"), ("the turn must be committed and pushed", remote_log)
    files = subprocess.run(["git", "--git-dir", bare, "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    assert "factory/log/C_DEV.jsonl" in files and "factory/state.json" in files, files
    state_remote = subprocess.run(["git", "--git-dir", bare, "show", "HEAD:factory/state.json"], capture_output=True, text=True).stdout
    assert "building" in state_remote, "the manager's state must reach the remote"
    print("8 ok: persistence to the repo and its remote")
    # 9. delivery failure: reply is kept pending, events stay unhandled, next run delivers without a new engine turn
    h9 = home(); b9 = tempfile.mkdtemp(); ok9 = fake_engine(b9, "ok")
    class FlakyPoster(FakePoster):
        def __init__(self): super().__init__(); self.fail_next = True
        def __call__(self, channel, thread_ts, text):
            if self.fail_next: self.fail_next = False; raise RuntimeError("slack down")
            super().__call__(channel, thread_ts, text)
    post9 = FlakyPoster(); sup9 = S.Supervisor(home=h9, engines=engines(ok9, ok9), poster=post9)
    e9 = event(h9, "deliver me"); assert sup9.run_once() is True
    handled = {json.loads(l)["id"] for l in open(os.path.join(h9, "inbox", "handled.jsonl"))} if os.path.exists(os.path.join(h9, "inbox", "handled.jsonl")) else set()
    assert e9 not in handled and os.path.exists(os.path.join(h9, "inbox", "pending-replies.jsonl")), "a failed delivery must leave the event unhandled and the reply pending"
    n_calls = len(calls(b9)); assert sup9.run_once() is True
    assert len(calls(b9)) == n_calls, "delivering a pending reply must not run the engine again"
    assert post9.posted and "handled 1 events" in post9.posted[-1][2]
    handled = {json.loads(l)["id"] for l in open(os.path.join(h9, "inbox", "handled.jsonl"))}
    assert e9 in handled, "the event is handled once the reply is delivered"
    print("9 ok: pending replies, handled only after delivery")
    print("b1 acceptance OK")


if __name__ == "__main__":
    main()
