"""Shared fixtures for the b11 tests (loops/b11.md requirement 7): recording fake engines that speak the
supervisor's stream-json (per-turn and persistent), a fake Codex, a fake `gh`, a fake `systemctl`, a controlled
clock and fake posters. Never a real engine, GitHub or Slack."""
import datetime as dt
import hashlib
import http.server
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from conftest import S, make_home

FADEN_REPO, HYDRA_REPO = "faden-systems/faden", "faden-systems/hydra-agent"

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import sys, os, json
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]
sid = argv[argv.index('--session-id') + 1] if '--session-id' in argv else (argv[argv.index('--resume') + 1] if '--resume' in argv else '')
PERSISTENT = '--input-format' in argv


def record(msg):
    with open(D / 'calls.jsonl', 'a') as f:
        f.write(json.dumps({'argv': argv, 'stdin': msg, 'kind': 'compaction' if '[compaction]' in msg else 'turn',
                            'persistent': PERSISTENT, 'pid': os.getpid()}) + '\n')


def answer(msg):
    if '[compaction]' in msg:
        if (D / 'fail_compact').exists():
            return None
        m = Path(os.environ['HYDRA_MEMORY_DIR']) / 'MEMORY.md'
        m.write_text(m.read_text() + '\nfixture memory flushed ' + sid + '\n')
        reply = 'compacted\n---HANDOFF---\ntracks: t1\nwaiting on: none\nlast decision: flush\nnext action: resume\nopen question: none'
        ctx = 20000
    else:
        reply = 'REPLY: ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none'
        ctx = int((D / ('context_after' if sid != 'sess-test' and (D / 'context_after').exists() else 'context')).read_text())
    usage = {'input_tokens': 100, 'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': ctx - 1100, 'output_tokens': 50}
    return [json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'usage': usage, 'content': [{'type': 'text', 'text': reply}]}}),
            json.dumps({'type': 'result', 'subtype': 'success', 'result': reply, 'session_id': sid,
                        'usage': {'input_tokens': 300, 'cache_creation_input_tokens': 3000, 'cache_read_input_tokens': (ctx - 1100) * 3, 'output_tokens': 150}})]


if not PERSISTENT:
    msg = sys.stdin.read() if not sys.stdin.isatty() else ''
    record(msg)
    out = answer(msg)
    if out is None:
        print('usage limit reached', file=sys.stderr); sys.exit(1)
    print('\n'.join(out))
else:
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        env = json.loads(line)
        content = env.get('message', {}).get('content')
        msg = content if isinstance(content, str) else ''.join(c.get('text', '') for c in content or [])
        record(msg)
        out = answer(msg)
        if out is None:
            print('usage limit reached', file=sys.stderr); sys.exit(1)
        print('\n'.join(out), flush=True)
'''

FAKE_CODEX = r'''#!/usr/bin/env python3
import sys, json
from pathlib import Path
D = Path(%(dir)r)
msg = sys.stdin.read() if not sys.stdin.isatty() else ''
argv = sys.argv[1:]
with open(D / 'calls.jsonl', 'a') as f:
    f.write(json.dumps({'argv': argv, 'stdin': msg, 'kind': 'codex'}) + '\n')
out = argv[argv.index('-o') + 1]
Path(out).write_text('REPLY: codex ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none\n')
print('codex done')
'''

FAKE_GH = r'''#!/usr/bin/env python3
import sys, json
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]


def norm(p):
    base, _, q = p.partition('?')
    params = sorted(kv for kv in q.split('&') if kv and not kv.startswith(('since=', 'per_page=')))
    return base + ('?' + '&'.join(params) if params else '')


with open(D / 'gh-calls.log', 'a') as f:
    f.write(json.dumps(argv) + '\n')
if argv[:1] != ['api'] or len(argv) < 2 or any(a in ('--jq', '--paginate', '-q') for a in argv):
    print('fake gh: unsupported invocation', argv, file=sys.stderr); sys.exit(2)
path = norm(argv[1])
routes = json.loads((D / 'routes.json').read_text())
fails = json.loads((D / 'fails.json').read_text()) if (D / 'fails.json').exists() else []
if any(path.startswith(norm(f)) for f in fails):
    print('fake gh: HTTP 503', file=sys.stderr); sys.exit(1)
if path not in routes:
    print('fake gh: no route for ' + path, file=sys.stderr); sys.exit(1)
print(json.dumps(routes[path]))
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import sys
from pathlib import Path
state = Path(%(state)r).read_text().strip()
print(state)
sys.exit(0 if state == 'active' else 3)
'''


def home():
    h = make_home()
    Path(h, "config.json").write_text("{}")
    return h


def fakes(context=940000, context_after=90000, fail_compact=False):
    d = Path(tempfile.mkdtemp(prefix="b11t-engine-"))
    (d / "context").write_text(str(context))
    (d / "context_after").write_text(str(context_after))
    if fail_compact:
        (d / "fail_compact").write_text("1")
    for name, src in (("claude", FAKE_CLAUDE), ("codex", FAKE_CODEX)):
        p = d / name
        p.write_text(src % {"dir": str(d)})
        p.chmod(0o755)
    return d


def engines(d):
    return {"claude-r2d2": {"bin": str(d / "claude"), "cred": "claude-r2d2.env"},
            "claude-l": {"bin": str(d / "claude"), "cred": "claude-l.env"},
            "codex": {"bin": str(d / "codex"), "cred": None}}


def calls(d):
    p = d / "calls.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def kinds(d):
    return [c["kind"] for c in calls(d)]


class Poster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))

    @property
    def texts(self):
        return [t for _, _, t in self.posted]


class Clock:
    def __init__(self, start=dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc)):
        self.now = start

    def __call__(self):
        self.now = self.now + dt.timedelta(seconds=1)
        return self.now

    def advance(self, seconds):
        self.now = self.now + dt.timedelta(seconds=seconds)

    def set(self, *args):
        self.now = dt.datetime(*args, tzinfo=dt.timezone.utc)


def make(h, d, clock=None, blog=None, poster=None, sleep=None):
    return S.Supervisor(home=h, engines=engines(d), poster=poster or Poster(), buildlog_poster=blog or Poster(),
                        clock=clock or Clock(), sleep=sleep)


def config(h, **cfg):
    Path(h, "config.json").write_text(json.dumps({"compaction": cfg} if cfg else {}))


def event(h, text, thread="1.0", source="slack", ts=None, instructs=True):
    ev = {"id": ts or f"ev-{os.urandom(6).hex()}", "source": source, "at": 1.0,
          "payload": {"channel": "C_DEV", "thread_ts": thread, "user": "U_FOUNDER", "text": text, "instructs": instructs}}
    S.append_event(h, ev)
    return ev["id"]


def turns(h):
    return S.read_jsonl(os.path.join(h, "logs", "turns.jsonl"))


def cstate(h):
    return S.read_compaction_state(h)


def session_id(h):
    return Path(h, "session-id").read_text().strip()


def status_line(h, now=None):
    return S.compaction_status_line(h, now=now)


def transcript_dir(h):
    d = Path(h, ".claude", "projects", str(h).replace("/", "-"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def sha(seed):
    return hashlib.sha1(seed.encode()).hexdigest()


def pr(number, head, updated="2026-10-08T00:00:00Z", labels=(), comments=0, review_comments=0, login="someone",
       reviews=(), checks=()):
    return {"number": number, "head": {"sha": head}, "updated_at": updated, "labels": [{"name": l} for l in labels],
            "comments": comments, "review_comments": review_comments, "user": {"login": login},
            "reviews": [{"state": r} for r in reviews], "checks": list(checks)}


class FakeGitHub:
    def __init__(self):
        self.d = Path(tempfile.mkdtemp(prefix="b11t-gh-"))
        self.routes = {}
        self.bin = self.d / "gh"
        self.bin.write_text(FAKE_GH % {"dir": str(self.d)})
        self.bin.chmod(0o755)
        self.set("user", {"login": "hydra-manager"})
        self.fail([])

    @staticmethod
    def norm(p):
        base, _, q = p.partition("?")
        params = sorted(kv for kv in q.split("&") if kv and not kv.startswith(("since=", "per_page=")))
        return base + ("?" + "&".join(params) if params else "")

    def set(self, path, data):
        self.routes[self.norm(path)] = data
        (self.d / "routes.json").write_text(json.dumps(self.routes))

    def repo(self, repo, head, prs=(), issues=()):
        self.set(f"repos/{repo}/commits/HEAD", {"sha": head})
        self.set(f"repos/{repo}/pulls?state=open&per_page=100",
                 [{k: v for k, v in p.items() if k not in ("reviews", "checks", "comments", "review_comments")} for p in prs])
        for p in prs:
            self.set(f"repos/{repo}/pulls/{p['number']}", {k: v for k, v in p.items() if k not in ("reviews", "checks")})
            self.set(f"repos/{repo}/pulls/{p['number']}/reviews", p["reviews"])
            self.set(f"repos/{repo}/commits/{p['head']['sha']}/check-runs",
                     {"total_count": len(p["checks"]), "check_runs": [{"conclusion": c} for c in p["checks"]]})
        self.set(f"repos/{repo}/issues?state=open&since=2000-01-01T00:00:00Z&per_page=100", list(issues))

    def fail(self, prefixes):
        (self.d / "fails.json").write_text(json.dumps(list(prefixes)))

    def calls(self):
        p = self.d / "gh-calls.log"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def issue_sinces(self, repo):
        out = []
        for argv in self.calls():
            path = argv[1] if len(argv) > 1 else ""
            if path.startswith(f"repos/{repo}/issues?"):
                m = re.search(r"since=([^&]+)", path)
                out.append(m.group(1) if m else None)
        return out


class Dogfood:
    def __init__(self):
        self.d = Path(tempfile.mkdtemp(prefix="b11t-dog-"))
        self.state = self.d / "unit-state"
        self.state.write_text("active\n")
        self.systemctl = self.d / "systemctl"
        self.systemctl.write_text(FAKE_SYSTEMCTL % {"state": str(self.state)})
        self.systemctl.chmod(0o755)
        self.status = self.d / "health-status"
        self.status.write_text("200")
        status_file = self.status

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code = int(status_file.read_text().strip() or "200")
                body = b'{"ok": true}' if code == 200 else b'{"ok": false}'
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/health"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.checkout = self.d / "checkout"
        self.checkout.mkdir()
        self.git("init", "-q")
        self.commit("one")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.checkout), "-c", "user.name=b11", "-c", "user.email=b11@example.test", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, msg):
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def close(self):
        self.server.shutdown()


def coder_dir(root, name, pid, state="running"):
    d = Path(root, name)
    d.mkdir()
    Path(d, "launcher.json").write_text(json.dumps({"pid": pid, "HEAD": sha(name)}))
    Path(d, "status.json").write_text(json.dumps({"state": state, "exit_code": None}))
    return d


def checks_log(h):
    return S.read_jsonl(os.path.join(h, "logs", "timer-checks.jsonl"))


def snapshot(h):
    return json.loads(Path(h, "logs", "timer-snapshot.json").read_text())


class Ticker:
    """One timer tick per call, each in a fresh every_s slot; returns the new timer event or None."""
    def __init__(self, sup, h, clock, every_s=60):
        self.sup, self.h, self.clock, self.every_s, self.n = sup, h, clock, every_s, 0

    def __call__(self):
        self.clock.advance(self.every_s)
        before = len(S.pending_events(self.h))
        rec = self.sup.timer_tick()
        self.n += 1
        new = S.pending_events(self.h)[before:]
        assert len(new) == (1 if rec["woke"] else 0), (rec, new)
        return rec, (new[0] if new else None)


def timer_setup(every_s=60, start=None, dogfood=True, repos=True):
    h, d = home(), fakes(context=100000)
    Path(h, "logs", "persistence.json").write_text(json.dumps({"status": "no_repo"}))
    gh, dog = FakeGitHub(), Dogfood()
    coder = Path(tempfile.mkdtemp(prefix="b11t-coder-"))
    timer = {"every_s": every_s, "gh_bin": str(gh.bin), "coder_logs_glob": str(coder / "*-logs")}
    if dogfood:
        timer["dogfood"] = {"unit": "fitflow-dogfood", "health_url": dog.url, "checkout": str(dog.checkout), "systemctl_bin": str(dog.systemctl)}
    if not repos:
        timer["github_repos"] = []
    Path(h, "config.json").write_text(json.dumps({"timer": timer}))
    gh.repo(FADEN_REPO, head=sha("f1"), prs=[pr(261, sha("p1"), labels=("loop",), reviews=("APPROVED",), checks=("success",))])
    gh.repo(HYDRA_REPO, head=sha("h1"))
    clock = Clock(start or dt.datetime(2026, 10, 8, 15, 0, 0, tzinfo=dt.timezone.utc))
    sup = make(h, d, clock=clock)
    return h, d, gh, dog, coder, clock, sup, Ticker(sup, h, clock, every_s)
