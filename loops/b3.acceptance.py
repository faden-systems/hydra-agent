#!/usr/bin/env python3
"""Exit-owned acceptance for b3: at a family switch the incoming engine's first turn carries the other family's
transcript, flattened, windowed, inline, and the ledger records it. Interfaces: Supervisor(home, engines, poster,
repo, codex_home=...) with the Claude config dir at $HYDRA_HOME/.claude and the session id in $HYDRA_HOME/session-id;
transcript.flatten_claude/flatten_codex/render/window as in loops/b3.md."""
import json, os, subprocess, sys, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "manager"))
import supervisor as S  # noqa: E402
import transcript as Tr  # noqa: E402


def fake_engine(dir_, name):
    p = os.path.join(dir_, name)
    open(p, "w").write("#!/usr/bin/env python3\nimport sys, os, json\nmsg=sys.stdin.read()\n"
                       f"open({dir_!r}+'/calls.jsonl','a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': msg}})+'\\n')\n"
                       "print('REPLY: ok')\nprint('---HANDOFF---')\nprint('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n")
    os.chmod(p, 0o755); return p


def calls(dir_): return [json.loads(l) for l in open(os.path.join(dir_, "calls.jsonl")) if l.strip()]


class Poster:
    def __init__(self): self.posted = []
    def __call__(self, channel, thread_ts, text): self.posted.append((channel, thread_ts, text))


def home():
    h = tempfile.mkdtemp()
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror", "codex-home"): os.makedirs(os.path.join(h, d))
    for n in ("claude-r2d2", "claude-l"): open(os.path.join(h, "credentials", f"{n}.env"), "w").write(f"CLAUDE_CODE_OAUTH_TOKEN=fake-{n}\n")
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-fable-5-1"}))
    open(os.path.join(h, "session-id"), "w").write("sess-b3\n")
    open(os.path.join(h, "budgets.json"), "w").write(json.dumps({"turns_per_hour": 100, "claude_turns_per_day": {"claude-r2d2": 100, "claude-l": 100}}))
    open(os.path.join(h, "config.json"), "w").write(json.dumps({"transition": {"max_tokens": 100000, "tool_result_max_chars": 4000, "enabled": True}}))
    return h


def repo():
    bare = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    r = tempfile.mkdtemp(); subprocess.run(["git", "clone", "-q", bare, r], check=True)
    os.makedirs(os.path.join(r, "factory")); open(os.path.join(r, "factory", "state.json"), "w").write("{}")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", r, "add", "-A"], check=True); subprocess.run(["git", "-C", r, "commit", "-qm", "init"], check=True, env=env); subprocess.run(["git", "-C", r, "push", "-q", "-u", "origin", "HEAD"], check=True)
    os.environ.update({k: v for k, v in env.items() if k.startswith("GIT_")}); return r


def event(h, text):
    ev = {"id": str(uuid.uuid4()), "source": "slack", "at": time.time(), "payload": {"channel": "C_DEV", "thread_ts": "1.0", "user": "U_FOUNDER", "text": text, "instructs": True}}
    open(os.path.join(h, "inbox", "events.jsonl"), "a").write(json.dumps(ev) + "\n")


def claude_transcript(h, cwd, lines):
    enc = cwd.replace("/", "-"); d = os.path.join(h, ".claude", "projects", enc); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "sess-b3.jsonl")
    with open(p, "w") as f:
        for at, role, content in lines:
            f.write(json.dumps({"type": role, "timestamp": at, "sessionId": "sess-b3", "cwd": cwd, "message": {"role": role, "content": content}}) + "\n")
    return p


def codex_rollout(h, cwd, lines):
    d = os.path.join(h, "codex-home", "sessions", "2026", "09", "30"); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "rollout-2026-09-30T22-00-00-abc.jsonl")
    with open(p, "w") as f:
        f.write(json.dumps({"timestamp": "2026-09-30T22:00:00Z", "type": "session_meta", "payload": {"cwd": cwd, "id": "abc"}}) + "\n")
        for at, role, text in lines:
            f.write(json.dumps({"timestamp": at, "type": "response_item", "payload": {"type": "message", "role": role, "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}}) + "\n")
    return p


def main():
    h = home(); r = repo(); cwd = os.getcwd()
    cl_dir = tempfile.mkdtemp(); cl = fake_engine(cl_dir, "claude"); cx_dir = tempfile.mkdtemp(); cx = fake_engine(cx_dir, "codex")
    engines = {"claude-r2d2": {"bin": cl, "cred": "claude-r2d2.env"}, "claude-l": {"bin": cl, "cred": "claude-l.env"}, "codex": {"bin": cx, "cred": None}}
    post = Poster(); sup = S.Supervisor(home=h, engines=engines, poster=post, repo=r, codex_home=os.path.join(h, "codex-home"))
    mem = os.path.join(r, "factory", "manager-memory")

    # a Claude transcript with a fact that is nowhere in the memory folder, plus a long tool result and a thinking block
    big = "x" * 9000
    claude_transcript(h, cwd, [
        ("2026-09-30T21:00:00Z", "user", "the vendor for the iOS device farm is Kobiton, decided today"),
        ("2026-09-30T21:00:05Z", "assistant", [{"type": "thinking", "thinking": "private reasoning that must not appear"}, {"type": "text", "text": "Noted: Kobiton for the device farm."}]),
        ("2026-09-30T21:01:00Z", "assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "cat big.log"}}]),
        ("2026-09-30T21:01:02Z", "user", [{"type": "tool_result", "content": big}]),
        ("2026-09-30T21:02:00Z", "assistant", [{"type": "text", "text": "Log reviewed, nothing to act on."}]),
    ])
    # 1. a claude turn first (so the ledger has a claude family turn), then switch to codex: the transition read is inline
    event(h, "warm up"); assert sup.run_once() is True
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "codex", "model": "gpt-6-astra"}))
    event(h, "which vendor did we pick for the device farm?"); assert sup.run_once() is True
    stdin = calls(cx_dir)[-1]["stdin"]
    i_mem = stdin.find("[memory]"); i_tr = stdin.find("[transition]")
    assert i_mem >= 0 and i_tr > i_mem, "transition block must follow the memory lines"
    assert "engine family switched from claude to codex" in stdin, stdin[i_tr:i_tr + 200]
    assert "Kobiton" in stdin and "Noted: Kobiton for the device farm." in stdin, "the Claude-only fact must be inline, verbatim"
    assert "Transcript window:" in stdin, "the window line must be present"
    assert "private reasoning that must not appear" not in stdin, "thinking blocks are dropped"
    assert "tool Bash(" in stdin and "more chars omitted" in stdin and big not in stdin, "tool call kept, long tool result cut at the cap with a marker"
    ledger = [json.loads(l) for l in open(os.path.join(mem, "LEDGER.jsonl")) if l.strip()]
    tr = ledger[-1].get("transition"); assert tr and tr["from"] == "claude" and tr["to"] == "codex" and tr["entries_kept"] >= 4 and tr["est_tokens"] > 0, ledger[-1]
    assert any(f.startswith("transition/claude-to-codex-") for f in os.listdir(mem) and os.listdir(os.path.join(mem, "transition"))), "window written to the record"
    print("1 ok: claude -> codex carries the Claude-only fact inline, thinking dropped, tool result capped, ledger has transition")

    # 2. codex says something only in its rollout; switch back to claude: the Codex-only fact is inline
    codex_rollout(h, cwd, [
        ("2026-09-30T22:05:00Z", "user", "any change to the device farm plan?"),
        ("2026-09-30T22:05:10Z", "assistant", "Yes: the founder said the Kobiton contract starts on October 15, budget code DF-7."),
    ])
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "claude-l", "model": "claude-fable-5-1"}))
    event(h, "when does the device farm contract start?"); assert sup.run_once() is True
    stdin = calls(cl_dir)[-1]["stdin"]
    assert "engine family switched from codex to claude" in stdin and "budget code DF-7" in stdin, "the Codex-only fact must be inline for Claude"
    print("2 ok: codex -> claude carries the Codex-only fact inline")

    # 3. same-family switch: no transition block
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-fable-5-1"}))
    event(h, "still there?"); assert sup.run_once() is True
    assert "[transition]" not in calls(cl_dir)[-1]["stdin"], "claude-l -> claude-r2d2 is the same family"
    print("3 ok: same-family switch has no transition block")

    # 4. the window respects max_tokens: newest kept, oldest dropped, meta says so
    open(os.path.join(h, "config.json"), "w").write(json.dumps({"transition": {"max_tokens": 1500, "tool_result_max_chars": 4000, "enabled": True}}))
    many = [(f"2026-09-30T21:{m:02d}:00Z", "user", f"filler note number {m} " + "y" * 300) for m in range(10, 40)]
    many += [("2026-09-30T21:50:00Z", "user", "the newest fact is code NEWEST-42")]
    claude_transcript(h, cwd, many)
    sup2 = S.Supervisor(home=h, engines=engines, poster=post, repo=r, codex_home=os.path.join(h, "codex-home"))
    event(h, "warm"); assert sup2.run_once() is True  # claude-r2d2 turn
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "codex", "model": "gpt-6-astra"}))
    event(h, "what is the newest fact?"); assert sup2.run_once() is True
    stdin = calls(cx_dir)[-1]["stdin"]
    assert "NEWEST-42" in stdin and "filler note number 10 " not in stdin, "newest kept, oldest dropped"
    assert "earlier entries not included" in stdin, "the window line must say what was cut"
    ledger = [json.loads(l) for l in open(os.path.join(mem, "LEDGER.jsonl")) if l.strip()]
    assert ledger[-1]["transition"]["entries_kept"] < ledger[-1]["transition"]["entries_total"] and ledger[-1]["transition"]["est_tokens"] <= 1800, ledger[-1]["transition"]
    print("4 ok: max_tokens respected, meta correct")

    # 5. flatteners against the recorded real fixtures
    fx = os.path.join(ROOT, "tests", "manager", "fixtures", "transcripts")
    cfx = [f for f in os.listdir(fx) if "claude" in f and f.endswith(".jsonl")][0]; xfx = [f for f in os.listdir(fx) if "codex" in f and f.endswith(".jsonl")][0]
    ce = Tr.flatten_claude(os.path.join(fx, cfx)); xe = Tr.flatten_codex(os.path.join(fx, xfx))
    assert ce and xe and all(set(e) >= {"at", "role", "kind", "text"} for e in ce + xe), "flatteners must parse the recorded real files"
    assert any(e["kind"] == "text" and e["role"] == "assistant" for e in ce) and any(e["kind"] == "text" and e["role"] == "assistant" for e in xe)
    txt, meta = Tr.window(Tr.render(ce), 500); assert meta["est_tokens"] <= 600 and "Transcript window:" in txt
    print("5 ok: recorded fixtures parse; window meta")
    print("b3 acceptance OK")


if __name__ == "__main__":
    main()
