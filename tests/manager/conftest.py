"""Shared fixtures for the manager tests: a temporary HYDRA_HOME, recording fake engines, a fake poster."""
import json
import os
import stat
import sys
import tempfile
import time
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANAGER = os.path.join(ROOT, "manager")
sys.path.insert(0, MANAGER)

# Python 3.12's venv does not drop a .gitignore into the venv (3.13 does); the exit script's fence lists untracked
# files, so backfill that behaviour for the project venv the exit script creates at the repo root.
_venv = os.path.join(ROOT, ".venv")
if os.path.isdir(_venv) and not os.path.exists(os.path.join(_venv, ".gitignore")):
    with open(os.path.join(_venv, ".gitignore"), "w") as _f:
        _f.write("*\n")

import supervisor as S  # noqa: E402
import bridge as B  # noqa: E402


def make_home(root=None):
    h = root or tempfile.mkdtemp(prefix="hydra-home-")
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror"):
        os.makedirs(os.path.join(h, d), exist_ok=True)
    for name in ("claude-r2d2", "claude-l"):
        with open(os.path.join(h, "credentials", f"{name}.env"), "w") as f:
            f.write(f"CLAUDE_CODE_OAUTH_TOKEN=fake-{name}\n")
    with open(os.path.join(h, "engine"), "w") as f:
        f.write("claude-r2d2\n")
    with open(os.path.join(h, "session-id"), "w") as f:
        f.write("sess-test\n")
    return h


def fake_engine(dir_, behaviour="ok", name="claude", reply="REPLY: handled {n} events"):
    """A recording fake engine: appends argv/env/stdin to <dir>/calls.jsonl, then behaves.
    behaviour: ok | quota | crash | nohandoff | long | json"""
    p = os.path.join(dir_, name)
    bodies = {
        "quota": "print('usage limit reached for this account', file=sys.stderr); sys.exit(1)\n",
        "crash": "print('segfault-ish failure', file=sys.stderr); sys.exit(2)\n",
        "ok": f"print({reply!r}.format(n=msg.count('source:')))\nprint('---HANDOFF---')\n"
              "print('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')\n",
        "nohandoff": "print('just a reply, no handoff')\n",
        "long": "print('\\n'.join(f'line {i}' for i in range(60)))\nprint('---HANDOFF---')\nprint('tracks: long')\n",
        "json": "print(json.dumps({'result': 'json reply\\n---HANDOFF---\\ntracks: j1', 'usage': {'input_tokens': 5, 'output_tokens': 7}, 'session_id': 'sess-json'}))\n",
    }
    with open(p, "w") as f:
        f.write("#!/usr/bin/env python3\nimport sys, os, json\nmsg=sys.stdin.read()\n"
                f"open({dir_!r} + '/calls.jsonl','a').write(json.dumps({{'argv': sys.argv[1:], "
                "'env': {k: v for k, v in os.environ.items() if k.startswith('CLAUDE') or k.startswith('CODEX')}, "
                "'stdin': msg}) + '\\n')\n" + bodies[behaviour])
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def calls(dir_):
    path = os.path.join(dir_, "calls.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


class FakePoster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))

    @property
    def texts(self):
        return " ".join(t for _, _, t in self.posted)


def queue_event(h, text, source="slack", sender="U_FOUNDER", ts=None, instructs=True, channel="C_DEV", thread="1.0"):
    ev = {"id": ts or str(uuid.uuid4()), "source": source, "at": time.time(),
          "payload": {"channel": channel, "thread_ts": thread, "user": sender, "text": text, "instructs": instructs}}
    S.append_event(h, ev)
    return ev["id"]


def engines(bin_r2d2, bin_l=None, bin_codex=None):
    e = {"claude-r2d2": {"bin": bin_r2d2, "cred": "claude-r2d2.env"},
         "claude-l": {"bin": bin_l or bin_r2d2, "cred": "claude-l.env"}}
    if bin_codex:
        e["codex"] = {"bin": bin_codex, "cred": None}
    return e


@pytest.fixture
def home(tmp_path):
    return make_home(str(tmp_path / "home"))


@pytest.fixture
def poster():
    return FakePoster()


@pytest.fixture
def ok_engine(tmp_path):
    d = str(tmp_path / "ok")
    os.makedirs(d)
    return fake_engine(d, "ok")


@pytest.fixture
def quota_engine(tmp_path):
    d = str(tmp_path / "quota")
    os.makedirs(d)
    return fake_engine(d, "quota")
