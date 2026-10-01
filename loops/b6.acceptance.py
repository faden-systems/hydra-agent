#!/usr/bin/env python3
"""Exit-owned acceptance for b6: heartbeat reaction swap, direct posts via `hydra post` through the bridge path,
and the `hydra update` privilege split. Interfaces: Supervisor(..., reactor=) and config.json
reactions.heartbeat_seconds; Bridge.drain_outbox(); the `hydra post` and `hydra update` CLI with HYDRA_FAKE_UID.
Tightened after the spec review round (hydra-agent #27): strict alternation, a failing reactor, `-` top-level posts,
every git call recorded with the user it ran as, a remote that moved after the clone, AGENTS.md, an untouched app
in the unprivileged run, and the exact root continuation command."""
import datetime as dt, json, os, re, shutil, subprocess, sys, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "manager"))
import supervisor as S  # noqa: E402
import bridge as B  # noqa: E402

T0 = dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=dt.timezone.utc); NOW = [T0]
def clock():
    NOW[0] = NOW[0] + dt.timedelta(seconds=1); return NOW[0]

NAMES = ("eyes", "hourglass_flowing_sand")


class Recorder:
    def __init__(self, fail_remove=False): self.calls = []; self.fail_remove = fail_remove
    def add(self, channel, ts, name): self.calls.append(("add", ts, name, time.monotonic()))
    def remove(self, channel, ts, name):
        self.calls.append(("remove", ts, name, time.monotonic()))
        if self.fail_remove: raise RuntimeError("no_reaction")


class Poster:
    def __init__(self, ts=None): self.posted = []; self.t = []; self.ts = ts
    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text)); self.t.append(time.monotonic())
        return {"ok": True, "ts": self.ts} if self.ts else None


def slow_engine(dir_, seconds):
    p = os.path.join(dir_, "claude")
    open(p, "w").write("#!/usr/bin/env python3\nimport sys, time\nmsg=sys.stdin.read()\n"
                       f"time.sleep({seconds})\nprint('REPLY: ok')\nprint('---HANDOFF---')\nprint('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n")
    os.chmod(p, 0o755); return p


def home(hb):
    h = tempfile.mkdtemp()
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror"): os.makedirs(os.path.join(h, d))
    for n in ("claude-r2d2", "claude-l"): open(os.path.join(h, "credentials", f"{n}.env"), "w").write(f"CLAUDE_CODE_OAUTH_TOKEN=fake-{n}\n")
    open(os.path.join(h, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-fable-5-1"}))
    open(os.path.join(h, "session-id"), "w").write("sess-b6\n")
    open(os.path.join(h, "budgets.json"), "w").write(json.dumps({"turns_per_hour": 100, "claude_turns_per_day": {"claude-r2d2": 100, "claude-l": 100}}))
    open(os.path.join(h, "config.json"), "w").write(json.dumps({"reactions": {"heartbeat_seconds": hb}}))
    return h


def event(h, ts):
    ev = {"id": ts, "source": "slack", "at": time.time(), "payload": {"channel": "C_DEV", "thread_ts": "1.0", "ts": ts, "user": "U_FOUNDER", "text": "work", "instructs": True}}
    open(os.path.join(h, "inbox", "events.jsonl"), "a").write(json.dumps(ev) + "\n")


def engines(b): return {"claude-r2d2": {"bin": b, "cred": "claude-r2d2.env"}, "claude-l": {"bin": b, "cred": "claude-l.env"}}


def check_alternation(seq, lo, hi):
    """seq: [(action, name)] for one message. add eyes, then (remove current, add other) pairs, then remove current."""
    assert seq and seq[0] == ("add", "eyes"), seq
    rest = seq[1:]
    assert rest and rest[-1][0] == "remove", ("the last action is a remove", seq)
    swaps = rest[:-1]
    assert len(swaps) % 2 == 0, ("swaps come in remove/add pairs", seq)
    current, n = "eyes", 0
    for k in range(0, len(swaps), 2):
        r, a = swaps[k], swaps[k + 1]
        assert r == ("remove", current), ("a swap removes the reaction that is standing", seq)
        assert a[0] == "add" and a[1] in NAMES and a[1] != current, ("a swap adds the other name", seq)
        current = a[1]; n += 1
    assert lo <= n <= hi, (f"expected {lo}..{hi} swaps", seq)
    assert rest[-1] == ("remove", current), ("the final removal targets the active reaction", seq)
    return n


def git_lines(log):
    """[(user, args)] for every git invocation the fake git recorded."""
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^git\[user=([^\]]*)\] (.*)$", log, re.M)]


def main():
    # 1. heartbeat: eyes -> hourglass -> eyes -> ..., strictly alternating, about three swaps for 1.7 s at 0.5 s
    h = home(0.5); rec = Recorder(); post = Poster(); sup = S.Supervisor(home=h, engines=engines(slow_engine(tempfile.mkdtemp(), 1.7)), poster=post, clock=clock, reactor=rec)
    event(h, "10.1"); assert sup.run_once() is True
    seq = [(c[0], c[2]) for c in rec.calls if c[1] == "10.1"]
    n = check_alternation(seq, 2, 4)
    assert rec.calls[-1][3] >= post.t[-1], "the final remove comes after delivery"
    assert not [c for c in rec.calls if c[3] > post.t[-1] and c[0] == "add"], "no adds after delivery"
    print(f"1 ok: heartbeat alternates ({n} swaps) and ends clean")
    # 1b. a reactor whose remove raises: the turn and the delivery still complete, nothing is added after delivery
    hb = home(0.5); recb = Recorder(fail_remove=True); postb = Poster(); supb = S.Supervisor(home=hb, engines=engines(slow_engine(tempfile.mkdtemp(), 1.2)), poster=postb, clock=clock, reactor=recb)
    event(hb, "11.1"); assert supb.run_once() is True and postb.posted, "a failing swap never blocks the turn"
    assert not [c for c in recb.calls if c[3] > postb.t[-1] and c[0] == "add"], "no adds after delivery when removes fail"
    assert any(c[0] == "remove" and c[3] >= postb.t[-1] for c in recb.calls), "delivery still tries to clear the reaction"
    print("1b ok: failing swaps are tolerated")
    # 2. heartbeat disabled: only eyes add/remove
    h2 = home(0); rec2 = Recorder(); post2 = Poster(); sup2 = S.Supervisor(home=h2, engines=engines(slow_engine(tempfile.mkdtemp(), 1.2)), poster=post2, clock=clock, reactor=rec2)
    event(h2, "20.1"); assert sup2.run_once() is True
    seq2 = [(c[0], c[2]) for c in rec2.calls if c[1] == "20.1"]; assert seq2 == [("add", "eyes"), ("remove", "eyes")], seq2
    print("2 ok: heartbeat 0 = plain b5")
    # 3. hydra post: outbox -> bridge path -> poster, join recorded, mirror carries thread_ts; replies arrive
    h3 = home(0); post3 = Poster(); br = B.Bridge(home=h3, allowlist={"U_FOUNDER": {"instructs": True}}, poster=post3, token_env={}); br.bot_id = "U_BOT"
    hydra = os.path.join(ROOT, "manager", "hydra")
    r = subprocess.run([sys.executable, hydra, "post", "C_DEV", "77.1", "hello from inside a turn"], env={**os.environ, "HYDRA_HOME": h3}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.path.exists(os.path.join(h3, "inbox", "outbox.jsonl")), "hydra post writes to the outbox"
    n3 = br.drain_outbox(); assert n3 == 1 and post3.posted[-1] == ("C_DEV", "77.1", "hello from inside a turn"), post3.posted
    th = json.load(open(os.path.join(h3, "threads.json"))); assert "77.1" in th.get("C_DEV", {}), "a direct post joins the thread"
    mirror = [json.loads(l) for l in open(os.path.join(h3, "mirror", "C_DEV.jsonl")) if l.strip()]
    assert mirror and mirror[-1].get("thread_ts") == "77.1" and "hello from inside a turn" in mirror[-1]["text"], mirror[-1]
    br.handle_message({"channel": "C_DEV", "ts": "77.2", "thread_ts": "77.1", "user": "U_FOUNDER", "text": "reply to the direct post"})
    q = [json.loads(l) for l in open(os.path.join(h3, "inbox", "events.jsonl")) if l.strip()]; assert any(e["id"] == "77.2" for e in q), "replies to a direct post reach the manager"
    assert br.drain_outbox() == 0, "the outbox is drained once"
    # 3b. `-` posts top level: the ts the poster returns becomes the joined thread and the mirrored thread_ts
    post3b = Poster(ts="88.1"); br.poster = post3b
    r = subprocess.run([sys.executable, hydra, "post", "C_DEV", "-", "a new top-level post"], env={**os.environ, "HYDRA_HOME": h3}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert br.drain_outbox() == 1 and post3b.posted[-1][0] == "C_DEV" and not post3b.posted[-1][1] and post3b.posted[-1][2] == "a new top-level post", post3b.posted
    th = json.load(open(os.path.join(h3, "threads.json"))); assert "88.1" in th.get("C_DEV", {}), ("a top-level post joins the thread it starts", th)
    mirror = [json.loads(l) for l in open(os.path.join(h3, "mirror", "C_DEV.jsonl")) if l.strip()]
    assert mirror[-1].get("thread_ts") == "88.1" and "a new top-level post" in mirror[-1]["text"], mirror[-1]
    print("3 ok: hydra post goes through the bridge path, joins, mirrors; replies arrive; `-` starts a joined thread")
    # 4. hydra update privilege split with recording fakes: sudo, systemctl and git (git logs the user it ran as, then
    #    runs the real git); the remote moves after the clone so the run must really fetch and merge
    real_git = shutil.which("git"); assert real_git, "git on PATH"
    fakebin = tempfile.mkdtemp(); calls = os.path.join(fakebin, "calls.log")
    open(os.path.join(fakebin, "sudo"), "w").write(
        "#!/usr/bin/env python3\nimport os, sys, subprocess\n"
        f"open({calls!r},'a').write('sudo ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "a = sys.argv[1:]\nuser = a[a.index('-u') + 1] if '-u' in a else 'root'\n"
        "env = dict(os.environ, FAKE_SUDO_USER=user)\n"
        "i = next((k for k, x in enumerate(a) if x == 'git'), None)\n"
        "sys.exit(subprocess.call(a[i:], env=env) if i is not None else 0)\n")
    open(os.path.join(fakebin, "git"), "w").write(
        "#!/usr/bin/env python3\nimport os, sys\n"
        f"open({calls!r},'a').write('git[user=' + os.environ.get('FAKE_SUDO_USER', '-') + '] ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        f"os.execv({real_git!r}, [{real_git!r}] + sys.argv[1:])\n")
    open(os.path.join(fakebin, "systemctl"), "w").write(
        f"#!/usr/bin/env python3\nimport sys\nopen({calls!r},'a').write('systemctl ' + ' '.join(sys.argv[1:]) + '\\n')\nsys.exit(0)\n")
    for tool in ("sudo", "git", "systemctl"): os.chmod(os.path.join(fakebin, tool), 0o755)
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    def commit_version(src, v):
        open(os.path.join(src, "manager", "CLAUDE.md"), "w").write(f"{v}\n")
        subprocess.run([real_git, "-C", src, "add", "-A"], check=True); subprocess.run([real_git, "-C", src, "commit", "-qm", v], check=True, env=genv)
        subprocess.run([real_git, "-C", src, "push", "-q", "-u", "origin", "HEAD:main"], check=True)
    bare = tempfile.mkdtemp(); subprocess.run([real_git, "init", "-q", "--bare", bare], check=True)
    src = tempfile.mkdtemp(); subprocess.run([real_git, "clone", "-q", bare, src], check=True); os.makedirs(os.path.join(src, "manager"))
    open(os.path.join(src, "manager", "supervisor.py"), "w").write("# stand-in: hydra update refuses a manager/ without supervisor.py\n")
    commit_version(src, "v1")
    clone = tempfile.mkdtemp(); subprocess.run([real_git, "clone", "-q", "-b", "main", bare, clone], check=True)
    commit_version(src, "v2")  # the remote is now ahead of the clone
    app = tempfile.mkdtemp(); hu = tempfile.mkdtemp(); os.makedirs(os.path.join(hu, "inbox"))
    env = {**os.environ, "HYDRA_HOME": hu, "HYDRA_REPO": clone, "HYDRA_APP": app, "PATH": fakebin + os.pathsep + os.environ["PATH"], "HYDRA_FAKE_UID": "0"}
    r4 = subprocess.run([sys.executable, hydra, "update"], env=env, capture_output=True, text=True, timeout=120)
    assert r4.returncode == 0, r4.stdout + r4.stderr
    log = open(calls).read(); g = git_lines(log)
    assert len(g) >= 2 and all(u == "hydra" for u, _ in g), ("as root, every git step runs via sudo -u hydra", log)
    assert any(a.split()[:1] == ["-C"] or "fetch" in a for _, a in g) and any("merge" in a or "pull" in a for _, a in g), ("fetch and merge/pull are among the git steps", log)
    assert "systemctl restart" in log, log
    assert open(os.path.join(app, "CLAUDE.md")).read() == "v2\n", "the root run installs the newer remote commit"
    agents = os.path.join(hu, "AGENTS.md"); assert os.path.islink(agents) and open(agents).read() == "v2\n", "AGENTS.md links to the deployed rules"
    # 4b. unprivileged: the git steps run directly as this user, nothing is copied or restarted, the root command is printed
    commit_version(src, "v3")
    open(calls, "w").close(); app2 = tempfile.mkdtemp(); open(os.path.join(app2, "sentinel"), "w").write("untouched\n")
    env.update({"HYDRA_FAKE_UID": "1001", "HYDRA_APP": app2})
    r5 = subprocess.run([sys.executable, hydra, "update"], env=env, capture_output=True, text=True, timeout=120)
    assert r5.returncode == 0, r5.stdout + r5.stderr
    log = open(calls).read(); g = git_lines(log)
    assert len(g) >= 2 and all(u == "-" for u, _ in g), ("as hydra, the git steps run directly, no sudo", log)
    assert "sudo" not in log.replace("[user=-]", "") and "systemctl" not in log, ("as hydra, no elevation and no restart attempted", log)
    assert subprocess.run([real_git, "-C", clone, "show", "HEAD:manager/CLAUDE.md"], capture_output=True, text=True).stdout == "v3\n", "the clone was updated by the unprivileged run"
    assert sorted(os.listdir(app2)) == ["sentinel"] and open(os.path.join(app2, "sentinel")).read() == "untouched\n", "the unprivileged run leaves app/ alone"
    assert re.search(r"sudo(\s+-n)?\s+hydra\s+update", r5.stdout), ("as hydra, print the exact root command for the rest", r5.stdout)
    print("4 ok: hydra update runs git as hydra under root, installs the newer commit; unprivileged it updates the clone and prints the root step")
    print("b6 acceptance OK")


if __name__ == "__main__":
    main()
