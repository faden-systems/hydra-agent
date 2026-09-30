"""The hydra CLI: say queues, status renders from fixtures, attach takes and releases the lock, engine/pause/resume."""
import json
import os
import subprocess
import sys

from conftest import MANAGER, S, engine_acc, fake_engine, queue_event

HYDRA = os.path.join(MANAGER, "hydra")


def run(home, *args, **kw):
    return subprocess.run([sys.executable, HYDRA, *args], env={**os.environ, "HYDRA_HOME": home},
                          capture_output=True, text=True, timeout=60, **kw)


def test_say_queues_a_cli_event(home):
    r = run(home, "say", "console hello")
    assert r.returncode == 0, r.stderr
    ev = S.read_jsonl(os.path.join(home, "inbox", "events.jsonl"))[-1]
    assert ev["source"] == "cli"
    assert ev["payload"] == {"text": "console hello", "user": "founder-console", "instructs": True,
                             "channel": "C_DEV", "thread_ts": None}
    assert "queued" in r.stdout


def test_say_prints_the_reply_when_a_loop_is_alive(home):
    S.write_text(os.path.join(home, "logs", "supervisor.pid"), f"{os.getpid()}\n")
    src = ("import os, sys, time, json; h=sys.argv[1]\n"
           "while True:\n"
           "  evs=[json.loads(l) for l in open(os.path.join(h,'inbox','events.jsonl')) if l.strip()] if os.path.exists(os.path.join(h,'inbox','events.jsonl')) else []\n"
           "  if evs:\n"
           "    os.makedirs(os.path.join(h,'inbox','replies'), exist_ok=True); open(os.path.join(h,'inbox','replies',evs[-1]['id']+'.txt'),'w').write('the reply\\n'); break\n"
           "  time.sleep(0.2)\n")
    responder = subprocess.Popen([sys.executable, "-c", src, home])
    try:
        r = run(home, "say", "ping")
    finally:
        responder.wait(timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "the reply"


def test_status_renders_from_fixtures(home):
    open(os.path.join(home, "state.json"), "w").write(json.dumps({"tracks": {"t1": "building"}}))
    S.append_jsonl(os.path.join(home, "logs", "turns.jsonl"), {"n": 3, "at": 1000.0, "engine": "codex", "events": ["a", "b"], "duration_s": 12.0})
    queue_event(home, "x")
    r = run(home, "status")
    assert r.returncode == 0
    assert "engine: claude-r2d2" in r.stdout and "on codex (2 events" in r.stdout and "queue: 1 pending" in r.stdout
    assert "t1: building" in r.stdout


def test_engine_pause_resume_logs_tail(home):
    assert run(home, "engine").stdout.strip() == "engine: claude-r2d2 (claude-fable-5-1)", "a legacy file shows the default model"
    assert run(home, "engine", "codex").returncode == 0 and engine_acc(home) == "codex"
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "codex", "model": "gpt-6-astra"}
    assert run(home, "engine", "bogus").returncode == 2
    assert run(home, "pause", "maintenance").returncode == 0 and open(os.path.join(home, "PAUSE")).read().strip() == "maintenance"
    assert "resumed" in run(home, "resume").stdout and not os.path.exists(os.path.join(home, "PAUSE"))
    assert "not paused" in run(home, "resume").stdout
    S.append_jsonl(os.path.join(home, "logs", "turns.jsonl"), {"n": 1, "at": 1000.0, "engine": "claude-l", "events": ["a"], "duration_s": 3.0, "error": "boom"})
    out = run(home, "logs", "5").stdout
    assert "turn 1 claude-l events=1" in out and "error=boom" in out
    S.append_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"), {"ts": "1.0", "user": "U1", "text": "hello there"})
    assert "U1: hello there" in run(home, "tail", "C_DEV").stdout
    assert run(home, "tail", "C_NOPE").returncode == 1
    assert run(home).returncode == 2 and run(home, "--help").returncode == 0


def test_engine_acc_model_round_trip_and_rejection(home):
    r = run(home, "engine", "acc=claude-l", "model=sonnet5")
    assert r.returncode == 0 and r.stdout.strip() == "engine: claude-l (claude-sonnet-5)", r.stdout + r.stderr
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": "claude-sonnet-5"}
    assert run(home, "engine").stdout.strip() == "engine: claude-l (claude-sonnet-5)"
    r = run(home, "engine", "acc=codex", "model=fable5.1")
    assert r.returncode == 2 and "claude family" in r.stderr and "gpt6" in r.stderr
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": "claude-sonnet-5"}, "rejected: nothing changes"
    r = run(home, "engine", "model=zzz")
    assert r.returncode == 2 and "unknown model" in r.stderr
    assert run(home, "engine", "model=haiku4.5").returncode == 0
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": "claude-haiku-4-5-20251001"}
    assert run(home, "engine", "acc=codex", "model=sol").stdout.strip() == "engine: codex (gpt-5.6-sol)"
    assert "engine: codex (gpt-5.6-sol)" in run(home, "status").stdout


def test_attach_uses_the_configured_model(home, tmp_path):
    d = tmp_path / "att2"; d.mkdir()
    script = str(d / "claude")
    open(script, "w").write("#!/usr/bin/env python3\nimport sys, json\n"
                            f"open({str(d / 'calls.jsonl')!r}, 'a').write(json.dumps({{'argv': sys.argv[1:]}}) + '\\n')\n")
    os.chmod(script, 0o755)
    open(os.path.join(home, "config.json"), "w").write(json.dumps({"engines": {"claude-r2d2": {"bin": script}, "claude-l": {"bin": script}}}))
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "claude-l", "model": "claude-opus-5"}))
    assert run(home, "attach").returncode == 0
    assert json.loads(open(d / "calls.jsonl").read().strip())["argv"] == ["--resume", "sess-test", "--model", "claude-opus-5"]


def test_attach_takes_and_releases_the_lock(home, tmp_path):
    d = tmp_path / "att"; d.mkdir()
    script = str(d / "claude")
    open(script, "w").write("#!/usr/bin/env python3\nimport os, sys, json\n"
                            f"open({str(d / 'calls.jsonl')!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'writer': open({os.path.join(home, 'WRITER')!r}).read(), 'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'), 'cfg': os.environ.get('CLAUDE_CONFIG_DIR')}}) + '\\n')\n")
    os.chmod(script, 0o755)
    open(os.path.join(home, "config.json"), "w").write(json.dumps({"engines": {"claude-r2d2": {"bin": script}, "claude-l": {"bin": script}}}))
    r = run(home, "attach")
    assert r.returncode == 0, r.stderr
    call = json.loads(open(d / "calls.jsonl").read().strip())
    assert call["argv"] == ["--resume", "sess-test", "--model", "claude-fable-5-1"]
    assert call["writer"].split()[1] .endswith("@console") and call["token"] == "fake-claude-r2d2"
    assert call["cfg"] == os.path.join(home, ".claude")
    assert not os.path.exists(os.path.join(home, "WRITER"))
    posts = S.read_jsonl(os.path.join(home, "logs", "posts.jsonl"))
    assert posts[-1]["channel"] == "C_DEV" and posts[-1]["text"].count("\n") == 1
    assert "console session by" in r.stdout


def test_attach_refuses_while_held(home, tmp_path):
    with S.acquire_writer(home, "supervisor"):
        r = run(home, "attach")
    assert r.returncode == 1 and "cannot attach" in r.stderr
