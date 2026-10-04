"""The compaction/rollover policy (loops/b4.md, amended by loops/b7.md requirement 13): context tokens recorded
from the engine's first-request usage; triggers by tokens and by bytes; a `[compaction]` turn writes durable memory
on the same session, then, only once it produces a valid five-line handoff and actually changes MEMORY.md's bytes,
the session id is atomically replaced with a new UUID (never `/compact`); the failure path (one buildlog post, the
old id kept, a back-off); quiet hours and the 25% override in the configured timezone; the Codex periodic flush;
`hydra compact` and `@manager compact`; the status line.

Changed for b7 (documented in docs/notes/b7.md "Changed assertions"): the engine fixture performs a memory write
and a session-id rollover instead of `/compact`; assertions read `sup.compaction_state()['last']` (old_id/new_id/
ok) instead of a ledger "compaction" value, since a failed rollover is recorded in logs/attempts.jsonl rather than
the family-turn ledger; turns.jsonl records `context_tokens`, not just `input_tokens`; offline fixtures set
`compaction.rollover_enabled: true` since automatic rollover now defaults held/disabled."""
import datetime as dt
import json
import os
import subprocess
import sys

from conftest import B, MANAGER, S, calls, engines, queue_event

HYDRA = os.path.join(MANAGER, "hydra")


class Clock:
    def __init__(self, start=dt.datetime(2026, 10, 2, 14, 0, 0, tzinfo=dt.timezone.utc)):
        self.now = start

    def __call__(self):
        self.now = self.now + dt.timedelta(seconds=1)
        return self.now

    def set(self, *args):
        self.now = dt.datetime(*args, tzinfo=dt.timezone.utc)


class Poster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))


def fake_claude(dir_, tokens=None, after_tokens=40000, fail_compact=False, name="claude", json_out=False,
                fail_message="usage limit reached"):
    """Records argv/stdin; reports `tokens` input tokens until a successful rollover, then `after_tokens` (the
    new session is told apart from the original `sess-test` fixture id by `--session-id` not matching it). The
    `[compaction]` turn writes MEMORY.md and the five-line handoff; it fails (exit 1, `fail_message` on stderr)
    when `fail_compact`."""
    p = os.path.join(dir_, name)
    lines = ["#!/usr/bin/env python3", "import sys, os, json",
             "msg = sys.stdin.read() if not sys.stdin.isatty() else ''",
             f"D = {dir_!r}",
             "open(D + '/calls.jsonl', 'a').write(json.dumps({'argv': sys.argv[1:], 'stdin': msg}) + '\\n')",
             "assert '/compact' not in sys.argv, 'obsolete compaction invocation'",
             "if '[compaction]' in msg:",
             f"    if {fail_compact!r}:",
             f"        print({fail_message!r}, file=sys.stderr); sys.exit(1)",
             "    from pathlib import Path",
             "    m = Path(os.environ['HYDRA_MEMORY_DIR']) / 'MEMORY.md'",
             "    m.write_text(m.read_text() + '\\nfixture memory flushed\\n')",
             "    print('compacted\\n---HANDOFF---\\ntracks: t1\\nwaiting on: none\\nlast decision: flush\\n"
             "next action: resume\\nopen question: none')",
             "    sys.exit(0)",
             "if '--session-id' in sys.argv and sys.argv[sys.argv.index('--session-id') + 1] != 'sess-test':",
             "    open(D + '/compacted', 'w').write('1')",
             f"n = {after_tokens} if os.path.exists(D + '/compacted') else {tokens!r}"]
    if json_out:
        lines += ["usage = {'input_tokens': n - 10, 'cache_read_input_tokens': 10, 'output_tokens': 7} if n else {}",
                  "print(json.dumps({'result': 'REPLY: ok\\n---HANDOFF---\\ntracks: t1', 'usage': usage}))"]
    else:
        lines += ["print('REPLY: ok')",
                  "if n: print('[usage] input_tokens=' + str(n))",
                  "print('---HANDOFF---')",
                  "print('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')"]
    open(p, "w").write("\n".join(lines) + "\n")
    os.chmod(p, 0o755)
    return p


def config(home, **over):
    cfg = {"threshold_tokens": 300000, "max_bytes": 50000000, "codex_every_turns": 25, "quiet_hours": [2, 5],
           "quiet_hours_tz": "UTC", "rollover_enabled": True}
    cfg.update(over)
    open(os.path.join(home, "config.json"), "w").write(json.dumps({"compaction": cfg}))


def make(home, tmp_path, tokens, clock=None, blog=None, poster=None, **fake):
    d = tmp_path / "cl"; d.mkdir(exist_ok=True)
    eng = fake_claude(str(d), tokens=tokens, **fake)
    sup = S.Supervisor(home=home, engines=engines(eng), poster=poster or Poster(), clock=clock or Clock(),
                       buildlog_poster=blog or Poster())
    return sup, str(d)


def turns(home):
    return S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))


def compact_calls(d):
    """Attempts of the `[compaction]` memory-writing turn (never a literal `/compact`, requirement 13)."""
    return [c for c in calls(d) if "[compaction]" in c["stdin"]]


# ----------------------------------------------------------------------------------------------- usage parsing

def test_usage_from_text_and_json_output(home, tmp_path):
    """Changed for b7 (requirement 3, loops/b7.md): usage now also carries `context_tokens`, the first request's
    usage alone; a single legacy `[usage]` line or a non-stream JSON result still parse, documented in
    docs/notes/b7.md "Changed assertions"."""
    text = "REPLY: ok\n[usage] input_tokens=1234 output_tokens=5\n---HANDOFF---\ntracks: x\n"
    out, usage, is_error, sid = S.Supervisor._parse_claude_output(text)
    assert "[usage]" not in out and out.startswith("REPLY: ok") and "---HANDOFF---" in out
    assert usage == {"tokens": 1239, "input_tokens": 1234, "context_tokens": 1234}
    js = json.dumps({"result": "r", "usage": {"input_tokens": 5, "cache_creation_input_tokens": 20,
                                                 "cache_read_input_tokens": 100, "output_tokens": 7}, "session_id": "s1"})
    out, usage, is_error, sid = S.Supervisor._parse_claude_output(js)
    assert out == "r" and usage == {"tokens": 132, "input_tokens": 125, "context_tokens": None} and sid == "s1"
    assert S.Supervisor._parse_claude_output("plain")[1] is None


def test_input_tokens_recorded_per_turn(home, tmp_path):
    config(home)
    sup, d = make(home, tmp_path, 1000, json_out=True)
    queue_event(home, "x")
    assert sup.run_once() is True
    assert turns(home)[-1]["input_tokens"] == 1000 and turns(home)[-1]["tokens"] == 1007
    assert sup.compaction_pending() is False


def test_config_defaults_and_overrides(home, poster):
    sup = S.Supervisor(home=home, engines={}, poster=poster)
    cfg = sup.compaction_config()
    assert cfg["threshold_tokens"] == 300000 and cfg["max_bytes"] == 50 * 1000 * 1000 and cfg["codex_every_turns"] == 25
    assert cfg["quiet_hours"] == [2, 5] and cfg["quiet_hours_tz"] == "local"
    config(home, threshold_tokens="200", quiet_hours=[22, 6], quiet_hours_tz="Europe/Berlin", max_bytes=None)
    sup = S.Supervisor(home=home, engines={}, poster=poster)
    cfg = sup.compaction_config()
    assert cfg["threshold_tokens"] == 200 and cfg["quiet_hours"] == [22, 6] and cfg["quiet_hours_tz"] == "Europe/Berlin"
    assert cfg["max_bytes"] == 50 * 1000 * 1000


# ----------------------------------------------------------------------------------------------- triggers and the run

def test_over_threshold_schedules_defers_then_runs_in_quiet_hours(home, tmp_path):
    """Changed for b7 (requirement 13, loops/b7.md): the `[compaction]` turn is followed by an atomic session-id
    rollover, not a separate `claude -p --resume ... /compact` call, so the real turn after it is `cs[i+1]`, not
    `cs[i+2]`, and it starts the new id with `--session-id` (never `--resume`). Outcome fields (before/after
    tokens, old/new id, ok) now live in `sup.compaction_state()['last']`, not a ledger "compaction" value."""
    config(home)
    clock = Clock(); blog = Poster()
    sup, d = make(home, tmp_path, 350000, clock=clock, blog=blog)
    queue_event(home, "one"); assert sup.run_once() is True
    assert turns(home)[-1]["context_tokens"] == 350000 and sup.compaction_pending() is True
    queue_event(home, "two"); assert sup.run_once() is True
    assert not compact_calls(d), "under 25% over the threshold and outside quiet hours: deferred"
    assert sup.compaction_pending() is True
    clock.set(2026, 10, 3, 3, 0, 0)
    old_sid = open(os.path.join(home, "session-id")).read().strip()
    queue_event(home, "three"); assert sup.run_once() is True
    cs = calls(d)
    i = next(i for i, c in enumerate(cs) if "[compaction]" in c["stdin"])
    assert cs[i]["stdin"].startswith("[compaction] Compact: write everything from this session that must survive into MEMORY.md")
    assert "update MANAGER-HANDOFF.md, then reply only `compacted`" in cs[i]["stdin"]
    assert "[memory]" not in cs[i]["stdin"], "the compaction turn is the bare instruction"
    argv = cs[i]["argv"]
    assert argv[argv.index("--resume") + 1] == old_sid and "--model" in argv
    assert "[compaction]" not in cs[i + 1]["stdin"] and "three" in cs[i + 1]["stdin"], "then the real turn"
    new_sid = open(os.path.join(home, "session-id")).read().strip()
    assert new_sid != old_sid, "rollover must select a new session id"
    real_argv = cs[i + 1]["argv"]
    assert real_argv[real_argv.index("--session-id") + 1] == new_sid, "the fresh session starts with --session-id"
    # the turn after rollover reports the reduced count: verified and recorded
    rec = sup.compaction_state()["last"]
    assert rec["before_tokens"] == 350000 and rec["after_tokens"] == 40000 and rec["ok"] is True and rec["at"]
    assert rec["reason"] == "tokens" and rec["old_id"] == old_sid and rec["new_id"] == new_sid
    assert turns(home)[-1]["context_tokens"] == 40000 and sup.compaction_pending() is False
    assert blog.posted == []
    kinds = [t.get("kind") for t in turns(home)]
    assert kinds.count("compaction") == 1 and turns(home)[-1].get("kind") is None
    led = S.read_ledger(sup.memory_dir)
    assert any(e.get("kind") == "compaction" for e in led), "the compaction turn has its own ledger line"
    assert "last compaction:" in S.status_text(home) and "(350000 -> 40000)" in S.status_text(home)


def test_far_over_threshold_runs_immediately(home, tmp_path):
    """Changed for b7: no separate `/compact` call, so the real "two" turn is stdins[2], not stdins[3]."""
    config(home)
    sup, d = make(home, tmp_path, 740000)
    queue_event(home, "one"); assert sup.run_once() is True
    assert not compact_calls(d)
    queue_event(home, "two"); assert sup.run_once() is True
    assert len(compact_calls(d)) == 1
    stdins = [c["stdin"] for c in calls(d)]
    assert "[compaction]" in stdins[1] and "two" in stdins[2]


def test_exactly_25_percent_is_immediate(home, tmp_path):
    config(home)
    sup, d = make(home, tmp_path, 375000)
    for t in ("one", "two"):
        queue_event(home, t); assert sup.run_once() is True
    assert len(compact_calls(d)) == 1


def test_quiet_hours_in_the_configured_timezone(home, tmp_path):
    # 03:00 Berlin is 01:00 UTC in October (CEST): quiet in Berlin, not in UTC
    config(home, quiet_hours_tz="Europe/Berlin")
    clock = Clock(dt.datetime(2026, 10, 2, 1, 0, 0, tzinfo=dt.timezone.utc))
    sup, d = make(home, tmp_path, 350000, clock=clock)
    assert sup.quiet_hours_now() is True
    config(home, quiet_hours_tz="UTC")
    sup2, _ = make(home, tmp_path, 350000, clock=clock)
    assert sup2.quiet_hours_now() is False
    config(home, quiet_hours=[22, 5], quiet_hours_tz="UTC")
    sup3, _ = make(home, tmp_path, 350000, clock=Clock(dt.datetime(2026, 10, 2, 23, 0, 0, tzinfo=dt.timezone.utc)))
    assert sup3.quiet_hours_now() is True, "a window across midnight"
    config(home, quiet_hours_tz="Not/AZone")
    sup4, _ = make(home, tmp_path, 350000, clock=clock)
    assert sup4.quiet_hours_now() in (True, False), "an unknown zone falls back to local time without raising"


def test_session_file_over_max_bytes_schedules(home, tmp_path):
    """Changed for b7: the outcome is read from `compaction_state()['last']`; growth is tracked against the
    *new* session's transcript after rollover, since the old one is left byte-for-byte untouched."""
    config(home, max_bytes=1000)
    sd = os.path.join(home, ".claude", "projects", home.replace("/", "-")); os.makedirs(sd)
    open(os.path.join(sd, "sess-test.jsonl"), "w").write("x" * 5000)
    sup, d = make(home, tmp_path, 1000)
    queue_event(home, "one"); assert sup.run_once() is True
    assert sup.compaction_pending() is True
    assert json.load(open(os.path.join(home, "logs", "compaction.json")))["pending"]["reason"] == "bytes"
    queue_event(home, "two"); assert sup.run_once() is True
    assert len(compact_calls(d)) == 1, "5000 over 1000 is more than 25% over: immediate"
    new_sid = open(os.path.join(home, "session-id")).read().strip()
    assert new_sid != "sess-test"
    open(os.path.join(sd, f"{new_sid}.jsonl"), "w").write("")
    queue_event(home, "three"); assert sup.run_once() is True
    assert sup.compaction_pending() is False, "the file cannot shrink: bytes re-trigger only after max_bytes more growth"
    rec = sup.compaction_state()["last"]
    assert rec["reason"] == "bytes" and rec["before_tokens"] == 1000 and rec["ok"] is True
    assert open(os.path.join(sd, "sess-test.jsonl")).read() == "x" * 5000, "the old transcript is untouched"
    open(os.path.join(sd, f"{new_sid}.jsonl"), "a").write("y" * 1500)
    queue_event(home, "four"); assert sup.run_once() is True
    assert sup.compaction_pending() is True


def test_no_usage_means_no_token_trigger(home, tmp_path):
    config(home)
    sup, d = make(home, tmp_path, None)
    queue_event(home, "one"); assert sup.run_once() is True
    assert "input_tokens" not in turns(home)[-1] and sup.compaction_pending() is False


# ----------------------------------------------------------------------------------------------- failure

def test_failed_compact_posts_once_keeps_the_session_and_backs_off(home, tmp_path):
    """Changed for b7: the memory-writing turn fails this time (`fail_compact`), so no rollover happens and no
    `kind=compaction` entry reaches turns.jsonl (requirement 13: a failed attempt belongs in attempts.jsonl, not
    the family-turn ledger) -- five queued events still produce five turns.jsonl rows, not six."""
    config(home)
    clock = Clock(); blog = Poster()
    sup, d = make(home, tmp_path, 740000, clock=clock, blog=blog, fail_compact=True)
    for i in range(5):
        queue_event(home, f"t{i}"); assert sup.run_once() is True
    assert open(os.path.join(home, "session-id")).read().strip() == "sess-test"
    assert len(compact_calls(d)) == 1, "after a failure the supervisor backs off instead of retrying every turn"
    assert len(blog.posted) == 1 and "rollover" in blog.posted[0][2].lower() and "sess-test" in blog.posted[0][2]
    rec = sup.compaction_state()["last"]
    assert rec["ok"] is False and rec.get("after_tokens") is None and rec["before_tokens"] == 740000 and rec["error"]
    assert sup.compaction_pending() is True, "still over the threshold"
    assert len(turns(home)) == 5 and sum(1 for t in turns(home) if t.get("kind") == "compaction") == 0
    assert "(740000 -> failed)" in S.status_text(home)
    # after the back-off it tries again, without a second post
    clock.now = clock.now + dt.timedelta(hours=7)
    queue_event(home, "later"); assert sup.run_once() is True
    assert len(compact_calls(d)) == 2 and len(blog.posted) == 1


def test_no_reduction_after_compact_is_a_failure(home, tmp_path):
    """The memory turn and rollover succeed; the next measured turn still shows context over the threshold, so
    verification alone marks it a failure (requirement 13: a successful memory-write alone cannot claim a
    reduction)."""
    config(home)
    blog = Poster()
    sup, d = make(home, tmp_path, 740000, blog=blog, after_tokens=600000)
    for i in range(3):
        queue_event(home, f"t{i}"); assert sup.run_once() is True
    rec = sup.compaction_state()["last"]
    assert rec["ok"] is False and rec["after_tokens"] == 600000 and rec["before_tokens"] == 740000
    assert rec["old_id"] == "sess-test" and rec["new_id"] and rec["new_id"] != "sess-test"
    assert len(blog.posted) == 1 and "600000" in blog.posted[0][2]


def test_failed_compaction_turn_skips_compact_and_posts(home, tmp_path):
    """Changed for b7: the `[compaction]` turn itself fails with a classifiable (quota-like) error; no rollover
    is attempted, the old session id is kept, and the failure is recorded in logs/attempts.jsonl (requirement
    11/12), not the family-turn ledger."""
    config(home)
    d = tmp_path / "cl"; d.mkdir()
    eng = fake_claude(str(d), tokens=740000, fail_compact=True, fail_message="usage limit reached")
    blog = Poster()
    sup = S.Supervisor(home=home, engines=engines(eng), poster=Poster(), clock=Clock(), buildlog_poster=blog)
    for i in range(3):
        queue_event(home, f"t{i}"); assert sup.run_once() is True
    assert len(compact_calls(str(d))) == 1 and len(blog.posted) == 1
    assert open(os.path.join(home, "session-id")).read().strip() == "sess-test"
    assert sup.compaction_state()["last"]["ok"] is False
    attempts = S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl"))
    failed = [a for a in attempts if a.get("success") is False]
    assert len(failed) == 1 and failed[0]["classification"] == "usage_limit"


# ----------------------------------------------------------------------------------------------- forcing

def test_forced_compaction_runs_on_the_next_tick_even_without_events(home, tmp_path):
    config(home)
    sup, d = make(home, tmp_path, 1000)
    queue_event(home, "warm"); assert sup.run_once() is True
    assert sup.run_once() is False
    S.write_text(os.path.join(home, "COMPACT"), "test\n")
    assert sup.compaction_pending() is True
    assert sup.run_once() is True
    assert len(compact_calls(d)) == 1 and not os.path.exists(os.path.join(home, "COMPACT"))
    assert sup.run_once() is False
    queue_event(home, "after"); assert sup.run_once() is True
    rec = sup.compaction_state()["last"]
    assert rec["reason"] == "forced" and rec["ok"] is True and rec["before_tokens"] == 1000


def test_hydra_compact_cli_schedules(home):
    r = subprocess.run([sys.executable, HYDRA, "compact"], env={**os.environ, "HYDRA_HOME": home},
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "scheduled" in r.stdout and os.path.exists(os.path.join(home, "COMPACT"))
    assert "compaction: pending" in subprocess.run([sys.executable, HYDRA, "status"], env={**os.environ, "HYDRA_HOME": home},
                                                   capture_output=True, text=True, timeout=60).stdout


def test_hydra_compact_waits_for_a_live_loop(home, tmp_path):
    S.write_text(os.path.join(home, "logs", "supervisor.pid"), f"{os.getpid()}\n")
    src = ("import os, sys, time, json; h=sys.argv[1]\n"
           "while not os.path.exists(os.path.join(h, 'COMPACT')): time.sleep(0.2)\n"
           "os.remove(os.path.join(h, 'COMPACT'))\n"
           "json.dump({'last': {'before_tokens': 5, 'after_tokens': None, 'at': 'T1', 'ok': False, 'error': 'nope'}}, open(os.path.join(h, 'logs', 'compaction.json'), 'w'))\n")
    helper = subprocess.Popen([sys.executable, "-c", src, home])
    try:
        r = subprocess.run([sys.executable, HYDRA, "compact"], env={**os.environ, "HYDRA_HOME": home, "HYDRA_COMPACT_TIMEOUT": "30"},
                           capture_output=True, text=True, timeout=60)
    finally:
        helper.wait(timeout=30)
    assert r.returncode == 1 and "failed" in r.stdout + r.stderr, r.stdout + r.stderr


def test_slack_compact_is_founder_only(home, poster):
    br = B.Bridge(home=home, allowlist={"U_FOUNDER": {"instructs": True}, "U_OPERATOR": {"instructs": False}},
                  poster=poster, token_env={}, bot_user_id="U_MANAGER")
    br.handle_message({"channel": "C_DEV", "ts": "1.1", "user": "U_OPERATOR", "text": "<@U_MANAGER> compact"})
    assert "not authorized" in poster.posted[-1][2] and not os.path.exists(os.path.join(home, "COMPACT"))
    br.handle_message({"channel": "C_DEV", "ts": "1.2", "user": "U_FOUNDER", "text": "<@U_MANAGER> compact"})
    assert open(os.path.join(home, "COMPACT")).read().strip() == "U_FOUNDER" and "scheduled" in poster.posted[-1][2]
    assert not any(e["id"] in ("1.1", "1.2") for e in S.read_jsonl(os.path.join(home, "inbox", "events.jsonl")))


# ----------------------------------------------------------------------------------------------- codex

def test_codex_periodic_flush(home, tmp_path):
    config(home, codex_every_turns=2)
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "codex", "model": "gpt-6-astra"}))
    dcl = tmp_path / "cl"; dcl.mkdir(); dcx = tmp_path / "cx"; dcx.mkdir()
    cl = fake_claude(str(dcl), tokens=1000); cx = fake_claude(str(dcx), name="codex")
    sup = S.Supervisor(home=home, engines=engines(cl, bin_codex=cx), poster=Poster(), clock=Clock(), buildlog_poster=Poster())
    for i in range(5):
        queue_event(home, f"t{i}"); assert sup.run_once() is True
    stdins = [c["stdin"] for c in calls(str(dcx))]
    flushed = [i for i, s in enumerate(stdins) if s.startswith("[flush]")]
    assert flushed == [1, 3], "the 2nd and 4th Codex turns carry the flush line, in front of everything"
    assert "MEMORY.md" in stdins[1] and "codex/NOTES.md" in stdins[1] and "[memory]" in stdins[1]
    assert not compact_calls(str(dcx)) and not calls(str(dcl)), "Codex is never compacted by the supervisor"


def test_codex_flush_off_when_zero(home, tmp_path):
    config(home, codex_every_turns=0)
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "codex", "model": "gpt-6-astra"}))
    dcx = tmp_path / "cx"; dcx.mkdir()
    cx = fake_claude(str(dcx), name="codex")
    sup = S.Supervisor(home=home, engines={"codex": {"bin": cx, "cred": None}}, poster=Poster())
    for i in range(3):
        queue_event(home, f"t{i}"); assert sup.run_once() is True
    assert not any("[flush]" in c["stdin"] for c in calls(str(dcx)))


def test_claude_md_carries_the_compaction_rule():
    text = open(os.path.join(MANAGER, "CLAUDE.md")).read()
    assert "When a turn begins with `[compaction]`, do exactly that" in text
    assert "reply `compacted`, nothing else" in text and "[flush]" in text
