#!/usr/bin/env python3
"""Exit-owned acceptance for b6: heartbeat reaction swap, direct posts via `hydra post` through the bridge path,
and the `hydra update` privilege split. Interfaces: Supervisor(..., reactor=) and config.json
reactions.heartbeat_seconds; Bridge.drain_outbox(); the `hydra post` and `hydra update` CLI with HYDRA_FAKE_UID."""
import datetime as dt, json, os, subprocess, sys, tempfile, time, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "manager"))
import supervisor as S  # noqa: E402
import bridge as B  # noqa: E402

T0 = dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=dt.timezone.utc); NOW = [T0]
def clock():
    NOW[0] = NOW[0] + dt.timedelta(seconds=1); return NOW[0]


class Recorder:
    def __init__(self): self.calls = []
    def add(self, channel, ts, name): self.calls.append(("add", ts, name, time.monotonic()))
    def remove(self, channel, ts, name): self.calls.append(("remove", ts, name, time.monotonic()))


class Poster:
    def __init__(self): self.posted = []; self.t = []
    def __call__(self, channel, thread_ts, text): self.posted.append((channel, thread_ts, text)); self.t.append(time.monotonic())


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


def main():
    # 1. heartbeat: eyes -> hourglass -> eyes -> removed, in order, with a 0.5 s interval and a 1.7 s engine
    h = home(0.5); rec = Recorder(); post = Poster(); sup = S.Supervisor(home=h, engines=engines(slow_engine(tempfile.mkdtemp(), 1.7)), poster=post, clock=clock, reactor=rec)
    event(h, "10.1"); assert sup.run_once() is True
    seq = [(c[0], c[2]) for c in rec.calls if c[1] == "10.1"]
    assert seq[0] == ("add", "eyes"), seq
    assert ("remove", "eyes") in seq and ("add", "hourglass_flowing_sand") in seq and ("remove", "hourglass_flowing_sand") in seq, ("the reaction must alternate", seq)
    i_hg = seq.index(("add", "hourglass_flowing_sand")); assert seq[i_hg - 1] == ("remove", "eyes") and i_hg >= 1
    assert seq.count(("add", "eyes")) >= 2, ("eyes must come back after the hourglass", seq)
    assert seq[-1][0] == "remove" and rec.calls[-1][3] >= post.t[-1], "the last action is a remove, after delivery"
    names_after = [c for c in rec.calls if c[3] > post.t[-1] and c[0] == "add"]; assert not names_after, "no adds after delivery"
    print("1 ok: heartbeat alternates and ends clean")
    # 2. heartbeat disabled: only eyes add/remove
    h2 = home(0); rec2 = Recorder(); post2 = Poster(); sup2 = S.Supervisor(home=h2, engines=engines(slow_engine(tempfile.mkdtemp(), 1.2)), poster=post2, clock=clock, reactor=rec2)
    event(h2, "20.1"); assert sup2.run_once() is True
    seq2 = [(c[0], c[2]) for c in rec2.calls if c[1] == "20.1"]; assert seq2 == [("add", "eyes"), ("remove", "eyes")], seq2
    print("2 ok: heartbeat 0 = plain b5")
    # 3. hydra post: outbox -> bridge path -> poster, join recorded, mirror carries thread_ts
    h3 = home(0); post3 = Poster(); br = B.Bridge(home=h3, allowlist={"U_FOUNDER": {"instructs": True}}, poster=post3, token_env={}); br.bot_id = "U_BOT"
    r = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "post", "C_DEV", "77.1", "hello from inside a turn"], env={**os.environ, "HYDRA_HOME": h3}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.path.exists(os.path.join(h3, "inbox", "outbox.jsonl")), "hydra post writes to the outbox"
    n = br.drain_outbox(); assert n == 1 and post3.posted[-1] == ("C_DEV", "77.1", "hello from inside a turn"), post3.posted
    th = json.load(open(os.path.join(h3, "threads.json"))); assert "77.1" in th.get("C_DEV", {}), "a direct post joins the thread"
    mirror = [json.loads(l) for l in open(os.path.join(h3, "mirror", "C_DEV.jsonl")) if l.strip()]
    assert mirror and mirror[-1].get("thread_ts") == "77.1" and "hello from inside a turn" in mirror[-1]["text"], mirror[-1]
    br.handle_message({"channel": "C_DEV", "ts": "77.2", "thread_ts": "77.1", "user": "U_FOUNDER", "text": "reply to the direct post"})
    q = [json.loads(l) for l in open(os.path.join(h3, "inbox", "events.jsonl")) if l.strip()]; assert any(e["id"] == "77.2" for e in q), "replies to a direct post reach the manager"
    print("3 ok: hydra post goes through the bridge path, joins, mirrors; replies arrive")
    # 4. hydra update privilege split with recording fakes
    fakebin = tempfile.mkdtemp()
    for tool in ("sudo", "systemctl"):
        open(os.path.join(fakebin, tool), "w").write("#!/usr/bin/env python3\nimport sys, subprocess\nopen(%r,'a').write(%r + ' ' + ' '.join(sys.argv[1:])+'\\n')\n%s" % (os.path.join(fakebin, "calls.log"), tool, "sys.exit(subprocess.call(sys.argv[sys.argv.index('git'):]) if 'git' in sys.argv else 0)\n" if tool == "sudo" else "sys.exit(0)\n"))
        os.chmod(os.path.join(fakebin, tool), 0o755)
    bare = tempfile.mkdtemp(); subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    src = tempfile.mkdtemp(); subprocess.run(["git", "clone", "-q", bare, src], check=True); os.makedirs(os.path.join(src, "manager")); open(os.path.join(src, "manager", "CLAUDE.md"), "w").write("v1\n")
    open(os.path.join(src, "manager", "supervisor.py"), "w").write("# stand-in: hydra update refuses a manager/ without supervisor.py\n")
    genv = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", src, "add", "-A"], check=True); subprocess.run(["git", "-C", src, "commit", "-qm", "v1"], check=True, env=genv); subprocess.run(["git", "-C", src, "push", "-q", "-u", "origin", "HEAD:main"], check=True)
    clone = tempfile.mkdtemp(); subprocess.run(["git", "clone", "-q", "-b", "main", bare, clone], check=True); app = tempfile.mkdtemp(); hu = tempfile.mkdtemp(); os.makedirs(os.path.join(hu, "inbox"))
    env = {**os.environ, "HYDRA_HOME": hu, "HYDRA_REPO": clone, "HYDRA_APP": app, "PATH": fakebin + os.pathsep + os.environ["PATH"], "HYDRA_FAKE_UID": "0"}
    r4 = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "update"], env=env, capture_output=True, text=True, timeout=120)
    assert r4.returncode == 0, r4.stdout + r4.stderr
    log = open(os.path.join(fakebin, "calls.log")).read()
    assert "sudo -u hydra" in log and "git" in log, ("as root, git must run via sudo -u hydra", log)
    assert "systemctl restart" in log, log
    assert open(os.path.join(app, "CLAUDE.md")).read() == "v1\n"
    open(os.path.join(fakebin, "calls.log"), "w").close()
    env["HYDRA_FAKE_UID"] = "1001"
    r5 = subprocess.run([sys.executable, os.path.join(ROOT, "manager", "hydra"), "update"], env=env, capture_output=True, text=True, timeout=120)
    assert r5.returncode == 0, r5.stdout + r5.stderr
    log = open(os.path.join(fakebin, "calls.log")).read()
    assert "sudo -u hydra" not in log and "systemctl restart" not in log, ("as hydra, no elevation attempted", log)
    assert "sudo" in r5.stdout and "hydra update" in r5.stdout, ("as hydra, print the root command for the rest", r5.stdout)
    print("4 ok: hydra update runs git as hydra under root and prints the root step when unprivileged")
    print("b6 acceptance OK")


if __name__ == "__main__":
    main()
