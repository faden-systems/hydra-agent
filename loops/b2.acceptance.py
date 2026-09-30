#!/usr/bin/env python3
"""Exit-owned acceptance for b2: shared memory across engines (ledger, snapshot, preamble, header, mirror, commit)
and the engine command with acc= and model=. Interfaces: Supervisor(home, engines, poster, repo) as in b1;
supervisor.parse_engine_command(text) -> {"acc", "model"} or raises supervisor.BadEngine; the engine file is JSON."""
import json, os, subprocess, sys, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "manager"))
import supervisor as S  # noqa: E402


def fake_engine(dir_, name="claude", memory_line=None):
    """Recording fake: logs argv/env/stdin; replies; writes a handoff; optionally writes a memory note file."""
    p = os.path.join(dir_, name)
    mem = ""
    if memory_line:
        mem = (f"import os\nd=os.environ.get('CLAUDE_CONFIG_DIR')\n"
               f"if d:\n    import glob\n    cwd=os.getcwd()\n    enc=cwd.replace('/','-')\n    md=os.path.join(d,'projects',enc,'memory'); os.makedirs(md,exist_ok=True); open(os.path.join(md,'MEMORY.md'),'a').write({memory_line!r}+'\\n')\n")
    open(p, "w").write("#!/usr/bin/env python3\nimport sys, os, json\nmsg=sys.stdin.read()\n"
                       f"open({dir_!r}+'/calls.jsonl','a').write(json.dumps({{'argv': sys.argv[1:], 'env': {{k: v for k, v in os.environ.items() if k.startswith('CLAUDE') or k.startswith('CODEX')}}, 'stdin': msg}})+'\\n')\n"
                       + mem +
                       "print('REPLY: ok ' + str(msg.count('source:')))\nprint('---HANDOFF---')\nprint('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n")
    os.chmod(p, 0o755); return p


def calls(dir_): return [json.loads(l) for l in open(os.path.join(dir_, "calls.jsonl")) if l.strip()]


class Poster:
    def __init__(self): self.posted = []
    def __call__(self, channel, thread_ts, text): self.posted.append((channel, thread_ts, text))


def home():
    h = tempfile.mkdtemp()
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror"): os.makedirs(os.path.join(h, d))
    for n in ("claude-r2d2", "claude-l"): open(os.path.join(h, "credentials", f"{n}.env"), "w").write(f"CLAUDE_CODE_OAUTH_TOKEN=fake-{n}\n")
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-fable-5-1"}))
    open(os.path.join(h, "session-id"), "w").write("sess-b2\n")
    open(os.path.join(h, "budgets.json"), "w").write(json.dumps({"turns_per_hour": 100, "claude_turns_per_day": {"claude-r2d2": 100, "claude-l": 100}}))
    return h


def repo():
    bare = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    r = tempfile.mkdtemp(); subprocess.run(["git", "clone", "-q", bare, r], check=True)
    os.makedirs(os.path.join(r, "factory")); open(os.path.join(r, "factory", "state.json"), "w").write("{}")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", r, "add", "-A"], check=True); subprocess.run(["git", "-C", r, "commit", "-qm", "init"], check=True, env=env); subprocess.run(["git", "-C", r, "push", "-q", "-u", "origin", "HEAD"], check=True)
    os.environ.update({k: v for k, v in env.items() if k.startswith("GIT_")})
    return r, bare


def event(h, text, ts=None):
    ev = {"id": ts or str(uuid.uuid4()), "source": "slack", "at": time.time(), "payload": {"channel": "C_DEV", "thread_ts": "1.0", "user": "U_FOUNDER", "text": text, "instructs": True}}
    open(os.path.join(h, "inbox", "events.jsonl"), "a").write(json.dumps(ev) + "\n"); return ev["id"]


def main():
    h = home(); r, bare = repo()
    cl_dir = tempfile.mkdtemp(); cl = fake_engine(cl_dir, "claude", memory_line="the founder prefers Hetzner-sized boxes")
    cx_dir = tempfile.mkdtemp(); cx = fake_engine(cx_dir, "codex")
    engines = {"claude-r2d2": {"bin": cl, "cred": "claude-r2d2.env"}, "claude-l": {"bin": cl, "cred": "claude-l.env"}, "codex": {"bin": cx, "cred": None}}
    post = Poster(); sup = S.Supervisor(home=h, engines=engines, poster=post, repo=r)
    mem = os.path.join(r, "factory", "manager-memory")

    # turn 1 on claude: ledger, snapshot, handoff header, mirror of own reply, commit
    event(h, "first"); assert sup.run_once() is True
    c = calls(cl_dir)[-1]; assert "--model" in c["argv"] and c["argv"][c["argv"].index("--model") + 1] == "claude-fable-5-1", c["argv"]
    ledger = [json.loads(l) for l in open(os.path.join(mem, "LEDGER.jsonl")) if l.strip()]
    assert ledger and ledger[-1]["engine"] == "claude-r2d2" and ledger[-1]["model"] == "claude-fable-5-1" and "turn" in ledger[-1] and "at" in ledger[-1], ledger
    assert os.path.exists(os.path.join(mem, "claude", "MEMORY.md")) and "Hetzner" in open(os.path.join(mem, "claude", "MEMORY.md")).read(), "Claude memory must be snapshotted"
    hand = open(os.path.join(mem, "MANAGER-HANDOFF.md")).read()
    assert hand.startswith("updated_at:") and "engine: claude-r2d2" in hand.splitlines()[1], hand[:120]
    assert os.path.islink(os.path.join(h, "MANAGER-HANDOFF.md")) or os.path.exists(os.path.join(h, "MANAGER-HANDOFF.md")), "old path must still resolve"
    mirror = open(os.path.join(h, "mirror", "C_DEV.jsonl")).read(); assert "REPLY: ok" in mirror, "the manager's own reply must be mirrored"
    files = subprocess.run(["git", "--git-dir", bare, "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    assert "factory/manager-memory/LEDGER.jsonl" in files and "factory/manager-memory/claude/MEMORY.md" in files, files
    print("1 ok: claude turn: ledger, snapshot, header, own-reply mirror, committed")

    # turn 2 on claude again: preamble says no other-engine changes
    event(h, "second"); assert sup.run_once() is True
    pre = calls(cl_dir)[-1]["stdin"]
    assert "[memory]" in pre and ("none" in pre.lower()), pre[:400]
    assert "codex/NOTES.md" not in pre
    print("2 ok: same-engine preamble says none")

    # switch to codex: preamble lists claude's changed files; codex gets -m with the family default
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "codex", "model": "gpt-6-astra"}))
    event(h, "third"); assert sup.run_once() is True
    c = calls(cx_dir)[-1]; pre = c["stdin"]
    assert "-m" in c["argv"] and c["argv"][c["argv"].index("-m") + 1] == "gpt-6-astra", c["argv"]
    assert "claude/MEMORY.md" in pre and "MANAGER-HANDOFF.md" in pre and "claude-r2d2" in pre, ("preamble must list the other engine's files", pre[:600])
    ledger = [json.loads(l) for l in open(os.path.join(mem, "LEDGER.jsonl")) if l.strip()]
    assert ledger[-1]["engine"] == "codex" and ledger[-1]["model"] == "gpt-6-astra"
    assert "engine: codex" in open(os.path.join(mem, "MANAGER-HANDOFF.md")).read().splitlines()[1]
    print("3 ok: switch to codex: preamble lists claude's files, model on argv, header updated")

    # back to claude-l: preamble lists codex's handoff (and NOTES if written); model carried over
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "claude-l", "model": "claude-fable-5-1"}))
    event(h, "fourth"); assert sup.run_once() is True
    c = calls(cl_dir)[-1]; pre = c["stdin"]
    assert "MANAGER-HANDOFF.md" in pre and "codex" in pre, pre[:600]
    assert c["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == "fake-claude-l"
    print("4 ok: back to claude-l: preamble lists codex's changes")

    # engine command parsing: aliases, family check, legacy form, unknown
    assert S.parse_engine_command("acc=claude-l model=sonnet5") == {"acc": "claude-l", "model": "claude-sonnet-5"}
    assert S.parse_engine_command("acc=codex") == {"acc": "codex", "model": "gpt-6-astra"}
    assert S.parse_engine_command("acc=codex model=sol") == {"acc": "codex", "model": "gpt-5.6-sol"}
    assert S.parse_engine_command("claude-r2d2") == {"acc": "claude-r2d2", "model": "claude-fable-5-1"}
    assert S.parse_engine_command("acc=claude-l model=claude-opus-5") == {"acc": "claude-l", "model": "claude-opus-5"}
    for bad in ("acc=claude-l model=sol", "acc=codex model=fable5.1", "acc=nope", "acc=claude-l model=zzz"):
        try:
            S.parse_engine_command(bad); raise AssertionError(f"must reject {bad!r}")
        except S.BadEngine as e:
            assert str(e), "rejection must say why"
    print("5 ok: engine command parsing")

    # legacy engine file upgrade + CLI round trip
    open(os.path.join(h, "engine"), "w").write("claude-r2d2\n")
    r2 = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "engine"], env={**os.environ, "HYDRA_HOME": h}, capture_output=True, text=True, timeout=60)
    assert r2.returncode == 0 and "claude-r2d2" in r2.stdout and "claude-fable-5-1" in r2.stdout, r2.stdout + r2.stderr
    r3 = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "engine", "acc=claude-l", "model=sonnet5"], env={**os.environ, "HYDRA_HOME": h}, capture_output=True, text=True, timeout=60)
    assert r3.returncode == 0, r3.stdout + r3.stderr
    assert json.load(open(os.path.join(h, "engine"))) == {"acc": "claude-l", "model": "claude-sonnet-5"}
    r4 = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "engine", "acc=codex", "model=fable5.1"], env={**os.environ, "HYDRA_HOME": h}, capture_output=True, text=True, timeout=60)
    assert r4.returncode != 0 and json.load(open(os.path.join(h, "engine"))) == {"acc": "claude-l", "model": "claude-sonnet-5"}, "a rejected command must change nothing"
    event(h, "fifth"); assert sup.run_once() is True
    c = calls(cl_dir)[-1]; assert c["argv"][c["argv"].index("--model") + 1] == "claude-sonnet-5", c["argv"]
    print("6 ok: legacy upgrade, CLI round trip, model reaches argv")
    print("b2 acceptance OK")


if __name__ == "__main__":
    main()
