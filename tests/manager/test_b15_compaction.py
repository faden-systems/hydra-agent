"""loops/b15.md requirement 5: rollover verification by the new session's first request, the reply-shape failures of
the `[compaction]` turn, and turn timeouts told apart from account faults. Fake engines (a fake Claude CLI that appends
`assistant` records with real-shaped usage to its transcript as it works and answers with stream-json) and fake
posters, temporary homes, a controlled clock and a 3 s engine timeout; never a real engine, GitHub or Slack. The
fixtures of the 2026-10-18 rows (the spec's appendix) are copied here; no fixture files are added."""
import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from conftest import S

FABLE = "claude-fable-5-1"
OLD_ID, NEW_ID = "52164f18-f7ee-42cc-a694-f50832d61cf0", "a0023f84-0449-4fd4-974b-f286c67c1cd0"  # the 14:54Z rollover
THRESHOLD = 300000
FIRST_REQUEST = {"input_tokens": 4, "cache_creation_input_tokens": 15407, "cache_read_input_tokens": 14201}  # 29,612
FIRST_TOTAL = 29612
# the sanitized rows of 2026-10-18 (loops/b15.md appendix)
ROWS_TURNS = [
    {"n": 2134, "at": "2026-10-18T14:51:44Z", "engine": "claude-r2d2", "context_tokens": 305024, "duration_s": 142.3},
    {"n": 2135, "at": "2026-10-18T14:54:57Z", "engine": "claude-r2d2", "context_tokens": None, "duration_s": 0.0, "kind": "compaction"},
    {"n": 2136, "at": "2026-10-18T14:54:57Z", "engine": "claude-r2d2", "context_tokens": None, "duration_s": 1800.9},
    {"n": 2137, "at": "2026-10-18T15:29:58Z", "engine": "claude-r2d2", "context_tokens": 343387, "duration_s": 218.4},
]
ROWS_TIMEOUTS = [
    {"at": "2026-10-18T15:24:58Z", "engine": "claude-r2d2", "model": FABLE, "rc": 124, "classification": "other", "reason": "timeout after 1800s", "success": False},
    {"at": "2026-10-18T16:03:46Z", "engine": "claude-r2d2", "model": FABLE, "rc": 124, "classification": "other", "reason": "timeout after 1800s", "success": False},
    {"at": "2026-10-18T16:38:49Z", "engine": "claude-r2d2", "model": FABLE, "rc": 124, "classification": "other", "reason": "timeout after 1800s", "success": False},
    {"at": "2026-10-18T17:13:55Z", "engine": "claude-r2d2", "model": FABLE, "rc": 124, "classification": "other", "reason": "timeout after 1800s", "success": False},
]
HANDOFF_NAMES = ("tracks:", "waiting on:", "last decision:", "next action:", "open question:")
HANDOFF_MISS = "carried no valid five-line handoff; end it with"
MEMORY_MISS = "left MEMORY.md unchanged; write the durable facts"
T0 = dt.datetime(2026, 10, 18, 14, 50, 0, tzinfo=dt.timezone.utc)
MIN = dt.timedelta(minutes=1)

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import sys, os, json, time
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]
acc = {'fake-claude-r2d2': 'claude-r2d2', 'fake-claude-l': 'claude-l'}.get(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''), '?')
model = argv[argv.index('--model') + 1] if '--model' in argv else ''
resume = '--resume' in argv
sid = argv[argv.index('--resume') + 1] if resume else (argv[argv.index('--session-id') + 1] if '--session-id' in argv else '')
table = json.loads((D / 'table.json').read_text()) if (D / 'table.json').exists() else {}
cfg = Path(os.environ.get('CLAUDE_CONFIG_DIR') or (Path.home() / '.claude'))
project = cfg / 'projects' / os.getcwd().replace('/', '-')
transcript = project / (sid + '.jsonl')
HANDOFF = '---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none'
def entry(msg):
    if msg.startswith('[compaction]'):
        e = table.get('compact', 'ok')
    else:
        q = json.loads((D / 'queue.json').read_text()) if (D / 'queue.json').exists() else []
        if q:
            e = q.pop(0)
            (D / 'queue.json').write_text(json.dumps(q))
        else:
            e = table.get(acc + '|' + model, 'ok')
    return e if isinstance(e, dict) else {'behaviour': e}
def record(msg, e, persistent=False):
    with open(D / 'calls.jsonl', 'a') as f:
        f.write(json.dumps({'acc': acc, 'model': model, 'resume': resume, 'sid': sid, 'behaviour': e.get('behaviour'),
                            'stdin': msg, 'persistent_turn': persistent}) + '\n')
def usage_of(e):
    ctx = int(e.get('ctx', 50000))
    return {'input_tokens': 4, 'cache_creation_input_tokens': ctx - 4 - 1000, 'cache_read_input_tokens': 1000}
def append(u, text='working'):
    project.mkdir(parents=True, exist_ok=True)
    if not transcript.exists():
        with open(transcript, 'a') as f:
            f.write(json.dumps({'type': 'user', 'sessionId': sid, 'cwd': os.getcwd()}) + '\n')
    with open(transcript, 'a') as f:
        f.write(json.dumps({'type': 'assistant', 'sessionId': sid, 'message': {'role': 'assistant', 'usage': dict(u, output_tokens=5),
                                                                                'content': [{'type': 'text', 'text': text}]}}) + '\n')
def serve(msg, e):
    b = e.get('behaviour', 'ok')
    u = usage_of(e)
    if msg.startswith('[compaction]'):
        m = Path(os.environ['HYDRA_MEMORY_DIR']) / 'MEMORY.md'
        if b == 'fail':
            print('compaction engine failure', file=sys.stderr); sys.exit(1)
        if b in ('ok', 'no_handoff'):
            m.write_text(m.read_text() + '\n- note\n')
        append(u, 'compacted')
        return ('compacted' if b == 'no_handoff' else 'compacted\n' + HANDOFF), u
    if b == 'timeout_working':
        for i in range(int(e.get('records', 3))):
            append(u, 'working %%d' %% i)
        time.sleep(30)
    if b == 'timeout_silent':
        time.sleep(30)
    append(u, 'REPLY: ok')
    return 'REPLY: ok\n' + HANDOFF, u
if '--input-format' not in argv:
    msg = sys.stdin.read() if not sys.stdin.isatty() else ''
    e = entry(msg)
    record(msg, e)
    reply, u = serve(msg, e)
    print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'usage': dict(u, output_tokens=5), 'content': [{'type': 'text', 'text': reply}]}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': reply, 'session_id': sid, 'usage': dict(u, output_tokens=5)}), flush=True)
    sys.exit(0)
e0 = entry('<persistent start>')
record('<persistent start>', e0)
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    env = json.loads(line)
    content = env.get('message', {}).get('content')
    text = content if isinstance(content, str) else ''.join(c.get('text', '') for c in content or [])
    e = entry(text)
    record(text, e, persistent=True)
    reply, u = serve(text, e)
    print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'usage': dict(u, output_tokens=5), 'content': [{'type': 'text', 'text': reply}]}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': reply, 'session_id': sid, 'usage': dict(u, output_tokens=5)}), flush=True)
'''


class Poster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        self.now = self.now + dt.timedelta(seconds=1)
        return self.now


def assistant_record(sid, usage, text="working"):
    return {"type": "assistant", "sessionId": sid, "message": {"role": "assistant", "usage": dict(usage, output_tokens=5),
                                                                "content": [{"type": "text", "text": text}]}}


def transcript_path(h, sid):
    return Path(h, ".claude", "projects", os.path.abspath(h).replace("/", "-"), f"{sid}.jsonl")


def make_home(root, engine=None, sid=OLD_ID, transcript=True):
    """A running home: a started session whose transcript holds one small first request (1,110 tokens)."""
    h = str(root)
    for d in ("inbox", "inbox/files", "logs", "credentials", ".claude", "mirror"):
        os.makedirs(os.path.join(h, d), exist_ok=True)
    for name in ("claude-r2d2", "claude-l"):
        token = f"fake-{name}"
        Path(h, "credentials", f"{name}.env").write_text(f"CLAUDE_CODE_OAUTH_TOKEN={token}\n")
        Path(h, "credentials", f"{name}.env.identity.json").write_text(json.dumps(
            {"schema_version": 1, "label": name, "account_id": f"fixture-{name}",
             "token_sha256": hashlib.sha256(token.encode()).hexdigest(), "verified_at": "2026-10-04T20:00:00Z",
             "method": "private-window-and-usage-bar", "evidence": "https://example.test/evidence"}))
    Path(h, "engine").write_text(json.dumps(engine or {"acc": "claude-r2d2", "model": FABLE}) + "\n")
    Path(h, "session-id").write_text(sid + "\n")
    Path(h, "rollover-intent.json").write_text(json.dumps({"old_id": "none", "new_id": sid, "started": True}) + "\n")
    if transcript:
        tp = transcript_path(h, sid)
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(json.dumps({"type": "user", "sessionId": sid, "cwd": h}) + "\n"
                      + json.dumps(assistant_record(sid, {"input_tokens": 10, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000})) + "\n")
    Path(h, "config.json").write_text(json.dumps({"dev_channel": "C_DEV"}))
    return h


def make_fakes(root, table=None):
    d = Path(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / "table.json").write_text(json.dumps(table or {}))
    p = d / "claude"
    p.write_text(FAKE_CLAUDE % {"dir": str(d)})
    p.chmod(0o755)
    return d


def set_table(d, table):
    (d / "table.json").write_text(json.dumps(table))


def queue(d, *entries):
    (d / "queue.json").write_text(json.dumps(list(entries)))


def engines(d):
    return {"claude-r2d2": {"bin": str(d / "claude"), "cred": "claude-r2d2.env"},
            "claude-l": {"bin": str(d / "claude"), "cred": "claude-l.env"}}


def calls(d):
    p = d / "calls.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def compaction_calls(d):
    return [c for c in calls(d) if c["stdin"].startswith("[compaction]")]


def make(h, d, poster=None, buildlog=None):
    return S.Supervisor(home=h, engines=engines(d), poster=poster or Poster(), buildlog_poster=buildlog or Poster(),
                        clock=Clock(), engine_timeout=3)


def event(h, text, thread="1.0", ts=None):
    ev = S.new_event("slack", {"channel": "C_DEV", "thread_ts": thread, "ts": ts or f"{time.time():.6f}", "text": text, "instructs": True})
    S.append_event(h, ev)
    return ev["id"]


def tick(sup, h, at_minutes):
    sup.clock.now = T0 + at_minutes * MIN
    retry = Path(h, "logs", "retry-after")
    if retry.exists():
        retry.unlink()


def turn(sup, h, at_minutes, text="go"):
    tick(sup, h, at_minutes)
    eid = event(h, text)
    assert sup.run_once() is True, f"the turn at {at_minutes} min did not run"
    return eid


def attempts(h):
    return [a for a in S.read_jsonl(os.path.join(h, "logs", "attempts.jsonl")) if isinstance(a, dict)]


def turns(h):
    return [t for t in S.read_jsonl(os.path.join(h, "logs", "turns.jsonl")) if isinstance(t, dict)]


def session_id(h):
    return Path(h, "session-id").read_text().strip()


def fault(h):
    p = Path(h, "logs", "fault.json")
    return json.loads(p.read_text()) if p.exists() else {}


def cstate(h):
    return S.read_compaction_state(h)


def notices(h, needle):
    return [it for it in S.read_outbox(h) if needle in (it.get("text") or "")]


def status_lines(h, needle):
    return [l for l in S.status_text(h).splitlines() if l.startswith(needle)]


def compaction_line(h):
    lines = status_lines(h, "context:")
    assert len(lines) == 1, lines
    return lines[0]


def rollover(sup, h, d, at_minutes=0, ctx=305024):
    """A tokens compaction the way 14:54Z happened: a measured turn over the threshold, then the next tick's
    `[compaction]` turn replaces the session id. Returns the old id."""
    old = session_id(h)
    queue(d, {"behaviour": "ok", "ctx": ctx})
    turn(sup, h, at_minutes)
    assert (cstate(h).get("pending") or {}).get("reason") == "tokens"
    return old


def timeout_row(h):
    rows = [a for a in attempts(h) if a.get("rc") == 124]
    assert rows, "no rc 124 attempt row"
    return rows[-1]


@pytest.fixture
def hd(tmp_path):
    return make_home(tmp_path / "home"), make_fakes(tmp_path / "engine")


# ---------------------------------------------------------------------------- requirement 1: verification

def test_oct18_rollover_verifies_from_the_transcript_after_a_working_timeout(hd):
    """The 14:54Z replay (ROWS_TURNS): the first turn on the new id is a working timeout; the next turn settles the
    verification from the new transcript's first request (29,612), not from its own 343,387."""
    h, d = hd
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    old = rollover(sup, h, d, 0, ctx=ROWS_TURNS[0]["context_tokens"])
    queue(d, {"behaviour": "timeout_working", "ctx": FIRST_TOTAL, "records": 2})
    tick(sup, h, 5)
    eid = event(h, "first turn on the new id")
    assert sup.run_once() is True
    new = session_id(h)
    assert new != old and len(compaction_calls(d)) == 1
    assert cstate(h)["verify"]["new_id"] == new and cstate(h).get("failed_at") is None
    row = timeout_row(h)
    assert (row["classification"], row["timeout"], row["transcript_records"]) == ("timeout", "working", 2)
    assert eid in S.handled_ids(h) and not Path(h, "logs", "fault.json").exists()
    queue(d, {"behaviour": "ok", "ctx": ROWS_TURNS[3]["context_tokens"]})
    turn(sup, h, 40, "the first measured turn")
    last = cstate(h)["last"]
    assert cstate(h).get("verify") is None and last["ok"] is True and last["after_tokens"] == FIRST_TOTAL
    assert last["source"] == "transcript" and last["new_id"] == new and last["before_tokens"] == 305024
    assert cstate(h).get("failed_at") is None and not any("rollover failed" in t for _, _, t in buildlog.posted)
    assert len(compaction_calls(d)) == 1, "the settled verification did not make the old session's size due again"
    assert f"session start: {FIRST_TOTAL} tokens" in compaction_line(h)


def test_verify_fails_from_the_transcript_when_the_first_request_is_large(hd):
    h, d = hd
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    old = rollover(sup, h, d, 0)
    queue(d, {"behaviour": "ok", "ctx": 320000})
    turn(sup, h, 5)
    last = cstate(h)["last"]
    assert session_id(h) != old and last["ok"] is False and last["after_tokens"] == 320000 and last["source"] == "transcript"
    assert "still 320000" in last["error"] and cstate(h)["failed_at"]
    assert sum("rollover failed" in t for _, _, t in buildlog.posted) == 1
    assert sup.compaction_due() == (False, None)


def test_verification_stays_pending_without_an_assistant_record_and_settles_at_compaction_due(hd):
    h, d = hd
    sup = make(h, d)
    old = rollover(sup, h, d, 0)
    tick(sup, h, 2)
    assert sup.run_once() is True, "the compaction tick ran without events"
    new = session_id(h)
    assert new != old and cstate(h)["verify"]["new_id"] == new
    assert sup.compaction_due() == (False, None) and cstate(h)["last"]["ok"] is None
    tp = transcript_path(h, new)
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps({"type": "user", "sessionId": new}) + "\n")
    assert sup.compaction_due() == (False, None) and cstate(h).get("verify") is not None, "a user record settles nothing"
    with open(tp, "a") as f:
        f.write("not json at all\n")
        f.write(json.dumps(assistant_record(new, FIRST_REQUEST)) + "\n")
        f.write(json.dumps(assistant_record(new, {"input_tokens": 4, "cache_creation_input_tokens": 400000, "cache_read_input_tokens": 0})) + "\n")
    sup.compaction_due()
    last = cstate(h)["last"]
    assert cstate(h).get("verify") is None and last["ok"] is True and last["after_tokens"] == FIRST_TOTAL and last["source"] == "transcript"
    assert sup.verify_compaction(10) is None, "nothing left to verify"


def test_explicit_argument_still_settles_the_verification(hd):
    h, d = hd
    sup = make(h, d)
    state = cstate(h)
    state["verify"] = {"before_tokens": 500000, "at": "fixture", "reason": "tokens", "old_id": "a", "new_id": "b"}
    sup.save_compaction_state(state)
    rec = sup.verify_compaction()
    assert rec["ok"] is None and cstate(h).get("verify") is not None, "no transcript of b, no turn measurement: pending"
    rec = sup.verify_compaction(10)
    assert rec["ok"] is True and rec["source"] == "argument" and rec["old_id"] == "a" and cstate(h).get("verify") is None


def test_turn_measurement_settles_a_verification_only_without_a_transcript(hd):
    """Amendment 1: an engine that writes no transcript at all (the b11 fakes) is still verified by the turn's own
    measurement; a transcript that exists but has no assistant record keeps the verification pending."""
    h, d = hd
    sup = make(h, d)
    state = cstate(h)
    state["verify"] = {"before_tokens": 500000, "at": "fixture", "reason": "tokens", "old_id": "a", "new_id": "no-file"}
    sup.save_compaction_state(state)
    rec = sup.verify_compaction(turn_tokens=320000)
    assert rec["ok"] is False and rec["source"] == "turn" and rec["after_tokens"] == 320000 and cstate(h).get("verify") is None
    state = cstate(h)
    state["verify"] = {"before_tokens": 500000, "at": "fixture", "reason": "tokens", "old_id": "a", "new_id": "has-file"}
    state["failed_at"] = None
    sup.save_compaction_state(state)
    tp = transcript_path(h, "has-file")
    tp.write_text(json.dumps({"type": "user", "sessionId": "has-file"}) + "\n")
    rec = sup.verify_compaction(turn_tokens=320000)
    assert rec["ok"] is None and cstate(h).get("verify") is not None, "the file decides once it exists; no record yet: pending"
    with open(tp, "a") as f:
        f.write(json.dumps(assistant_record("has-file", FIRST_REQUEST)) + "\n")
    rec = sup.verify_compaction(turn_tokens=320000)
    assert rec["ok"] is True and rec["source"] == "transcript" and rec["after_tokens"] == FIRST_TOTAL


def test_large_measurement_after_the_verification_schedules_a_compaction_instead_of_failing_it(hd):
    """The new session started small (its transcript's first request is 29,612); the turn that then measures 343,387
    passes the verification by the file and schedules the next compaction with its own number (ROWS_TURNS 2137)."""
    h, d = hd
    sup = make(h, d)
    rollover(sup, h, d, 0)
    tick(sup, h, 2)
    assert sup.run_once() is True, "the compaction tick"
    new = session_id(h)
    tp = transcript_path(h, new)
    tp.write_text(json.dumps({"type": "user", "sessionId": new}) + "\n" + json.dumps(assistant_record(new, FIRST_REQUEST)) + "\n")
    queue(d, {"behaviour": "ok", "ctx": ROWS_TURNS[3]["context_tokens"]})
    turn(sup, h, 5)
    last, pend = cstate(h)["last"], cstate(h)["pending"]
    assert last["ok"] is True and last["after_tokens"] == FIRST_TOTAL and last["source"] == "transcript", "the first request decides"
    assert pend["reason"] == "tokens" and pend["value"] == 343387, "the large measurement schedules the next compaction"
    assert sup.compaction_due() == (True, "tokens") and cstate(h).get("failed_at") is None


# ---------------------------------------------------------------------------- requirement 2: shape failures

def test_first_handoff_miss_is_retried_with_the_sentence_and_no_backoff(tmp_path):
    """The 17:37Z replay: `compacted` alone with MEMORY.md written keeps the old id (the baseline error text), arms
    no back-off, keeps COMPACT, posts nothing; the retry carries the handoff sentence and rolls over."""
    h, d = make_home(tmp_path / "home"), make_fakes(tmp_path / "engine", {"compact": "no_handoff"})
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    sup.ensure_memory_layout()
    handoff = Path(sup.memory_dir, "MANAGER-HANDOFF.md")
    before = handoff.read_bytes() if handoff.exists() else None
    Path(h, "COMPACT").write_text("founder\n")
    old = session_id(h)
    tick(sup, h, 0)
    assert sup.run_once() is True
    last = cstate(h)["last"]
    assert session_id(h) == old and last["ok"] is False and "five-line" in last["error"]
    assert cstate(h).get("failed_at") is None and cstate(h)["shape_failures"] == 1 and last["shape_failures"] == 1
    assert Path(h, "COMPACT").exists() and not buildlog.posted and not notices(h, "rollover failed")
    assert (handoff.read_bytes() if handoff.exists() else None) == before
    assert sup.compaction_due() == (True, "forced")
    set_table(d, {"compact": "ok"})
    tick(sup, h, 1)
    assert sup.run_once() is True
    cc = compaction_calls(d)
    assert len(cc) == 2 and HANDOFF_MISS in cc[1]["stdin"] and HANDOFF_MISS not in cc[0]["stdin"]
    assert cc[1]["stdin"].startswith(S.COMPACTION_MESSAGE)
    assert session_id(h) != old and not Path(h, "COMPACT").exists() and not cstate(h).get("shape_failures")
    assert cstate(h).get("failed_at") is None and not buildlog.posted


def test_unchanged_memory_backs_off_on_the_second_miss_with_the_forced_file_kept_then_consumed(tmp_path):
    h, d = make_home(tmp_path / "home"), make_fakes(tmp_path / "engine", {"compact": "unchanged"})
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    old = session_id(h)
    Path(h, "COMPACT").write_text("founder\n")
    tick(sup, h, 0)
    sup.run_once()
    last = cstate(h)["last"]
    assert session_id(h) == old and "did not change MEMORY.md" in last["error"]
    assert cstate(h).get("failed_at") is None and cstate(h)["shape_failures"] == 1 and Path(h, "COMPACT").exists() and not buildlog.posted
    tick(sup, h, 1)
    sup.run_once()
    cc = compaction_calls(d)
    assert len(cc) == 2 and MEMORY_MISS in cc[1]["stdin"] and HANDOFF_MISS not in cc[1]["stdin"]
    assert session_id(h) == old and cstate(h)["failed_at"] and not Path(h, "COMPACT").exists()
    assert sum("rollover failed" in t for _, _, t in buildlog.posted) == 1
    assert sup.compaction_due() == (False, None), "the back-off holds"


def test_engine_failure_of_the_compaction_turn_backs_off_at_once(tmp_path):
    h, d = make_home(tmp_path / "home"), make_fakes(tmp_path / "engine", {"compact": "fail"})
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    Path(h, "COMPACT").write_text("x\n")
    tick(sup, h, 0)
    sup.run_once()
    assert session_id(h) == OLD_ID and cstate(h)["failed_at"] and not Path(h, "COMPACT").exists()
    assert len(buildlog.posted) == 1 and not cstate(h).get("shape_failures"), "rc 1 is not a shape failure"


def test_compaction_message_names_the_handoff_block_and_a_compliant_reply_rolls_over(hd):
    h, d = hd
    sup = make(h, d)
    Path(h, "COMPACT").write_text("x\n")
    tick(sup, h, 0)
    sup.run_once()
    cc = compaction_calls(d)
    assert len(cc) == 1
    for needle in ("[compaction]", "MEMORY.md", "compacted", "---HANDOFF---") + HANDOFF_NAMES:
        assert needle in cc[0]["stdin"], needle
    assert cc[0]["stdin"] == S.COMPACTION_MESSAGE and session_id(h) != OLD_ID


# ---------------------------------------------------------------------------- requirement 3: turn timeouts

def test_working_timeout_row_line_and_untouched_fault_file(tmp_path):
    h = make_home(tmp_path / "home")
    d = make_fakes(tmp_path / "engine", {f"claude-r2d2|{FABLE}": {"behaviour": "timeout_working", "records": 3}})
    sup = make(h, d)
    eid = turn(sup, h, 0)
    row = timeout_row(h)
    assert (row["rc"], row["classification"], row["timeout"], row["transcript_records"], row["success"]) == (124, "timeout", "working", 3, False)
    assert eid in S.handled_ids(h), "b8: the uncertain disposition stays"
    assert not Path(h, "logs", "fault.json").exists() and not notices(h, "engine fault")
    n = turns(h)[-1]["n"]
    line = notices(h, "manager turn timeout: ")
    assert len(line) == 1 and line[0]["thread_ts"] is None and line[0]["channel"] == "C_DEV" and line[0]["id"] == f"timeout-{n}"
    assert line[0]["text"].startswith(f"manager turn timeout: turn {n} on claude-r2d2 ({FABLE}) ran ")
    assert " s while working (3 replies); its events were answered as unavailable" in line[0]["text"]
    assert turns(h)[-1]["timeout"] == "working" and turns(h)[-1]["transcript_records"] == 3
    assert json.loads(Path(h, "engine").read_text()) == {"acc": "claude-r2d2", "model": FABLE} and not S.read_engine_runtime(h).get("active")
    set_table(d, {})
    eid2 = turn(sup, h, 5)
    assert eid2 in S.handled_ids(h) and calls(d)[-1]["acc"] == "claude-r2d2" and len(notices(h, "manager turn timeout: ")) == 1


def test_oct18_four_working_timeouts_never_escalate_or_move(tmp_path):
    """The 15:24Z-17:13Z replay (ROWS_TIMEOUTS): four working timeouts over two hours, no fault file, no move."""
    h = make_home(tmp_path / "home")
    d = make_fakes(tmp_path / "engine", {f"claude-r2d2|{FABLE}": {"behaviour": "timeout_working", "records": 1}})
    sup = make(h, d)
    for m in (0, 35, 70, 105):
        turn(sup, h, m)
        assert not Path(h, "logs", "fault.json").exists()
    rows = [a for a in attempts(h) if a.get("rc") == 124]
    assert len(rows) == len(ROWS_TIMEOUTS) and all(r["classification"] == "timeout" and r["timeout"] == "working" for r in rows)
    assert json.loads(Path(h, "engine").read_text()) == {"acc": "claude-r2d2", "model": FABLE}
    assert not notices(h, "engine fault") and not notices(h, "fallback")
    lines = notices(h, "manager turn timeout: ")
    assert len(lines) == 4 and len({l["id"] for l in lines}) == 4
    assert all(c["acc"] == "claude-r2d2" for c in calls(d))


def test_silent_timeout_is_a_fault_counted_under_b14(tmp_path):
    h = make_home(tmp_path / "home")
    d = make_fakes(tmp_path / "engine", {f"claude-r2d2|{FABLE}": "timeout_silent"})
    sup = make(h, d)
    for m in (0, 5, 10):
        turn(sup, h, m)
    row = timeout_row(h)
    assert (row["classification"], row["timeout"], row["transcript_records"]) == ("other", "silent", 0), "the b14 row shape (amendment 1)"
    f = fault(h)
    assert f["count"] == 3 and f["text"].endswith("timeout") and not f.get("escalated")
    assert len(notices(h, "engine fault: ")) == 1 and not notices(h, "manager turn timeout: ")
    assert turns(h)[-1]["timeout"] == "silent"
    turn(sup, h, 35)
    assert fault(h)["escalated"] is True and fault(h)["count"] == 4, "the valve opened on the fourth silent timeout"
    assert len(notices(h, "engine fault escalated: ")) == 1


def test_persistent_mode_working_timeout_is_classified_the_same(tmp_path):
    h = make_home(tmp_path / "home", engine={"acc": "claude-r2d2", "model": FABLE, "mode": "persistent"})
    d = make_fakes(tmp_path / "engine")
    queue(d, "ok", {"behaviour": "timeout_working", "records": 2})
    sup = make(h, d)
    try:
        eid = turn(sup, h, 0)
        row = timeout_row(h)
        assert (row["classification"], row["timeout"], row["transcript_records"]) == ("timeout", "working", 2)
        assert eid in S.handled_ids(h) and not Path(h, "logs", "fault.json").exists() and len(notices(h, "manager turn timeout: ")) == 1
        assert not sup.persistent_status().get("pid"), "the child was retired"
    finally:
        sup._retire_persistent("planned", "test end")


def test_timeout_notice_survives_a_crash_before_the_enqueue(tmp_path, monkeypatch):
    h = make_home(tmp_path / "home")
    d = make_fakes(tmp_path / "engine", {f"claude-r2d2|{FABLE}": {"behaviour": "timeout_working", "records": 1}})
    sup = make(h, d)
    original = S._queue_notice_once
    reached = []

    class Crash(BaseException):
        pass

    def crash_queue(home_arg, channel, text, note_id):
        if not str(note_id).startswith("timeout-"):
            return original(home_arg, channel, text, note_id)
        assert turns(h)[-1]["timeout"] == "working" and note_id == f"timeout-{turns(h)[-1]['n']}", "the row is durable first"
        reached.append(note_id)
        raise Crash()
    monkeypatch.setattr(S, "_queue_notice_once", crash_queue)
    tick(sup, h, 0)
    event(h, "crash")
    with pytest.raises(Crash):
        sup.run_once()
    monkeypatch.setattr(S, "_queue_notice_once", original)
    assert len(reached) == 1 and not notices(h, "manager turn timeout: ")
    restarted = make(h, d)
    line = notices(h, "manager turn timeout: ")
    assert len(line) == 1 and line[0]["id"] == reached[0]
    set_table(d, {})
    eid = turn(restarted, h, 5)
    assert eid in S.handled_ids(h) and len(notices(h, "manager turn timeout: ")) == 1


# ---------------------------------------------------------------------------- the status lines

def test_status_shows_the_session_start_and_the_last_turn_timeout_for_a_day(tmp_path):
    h, d = make_home(tmp_path / "home"), make_fakes(tmp_path / "engine")
    sup = make(h, d)
    assert "session start: 1110 tokens" in compaction_line(h), compaction_line(h)
    assert not status_lines(h, "last turn timeout: ")
    h2 = make_home(tmp_path / "home2", transcript=False)
    make(h2, d)
    assert "session start:" not in compaction_line(h2), "no transcript: no segment (amendment 1)"
    h3 = make_home(tmp_path / "home3")
    d3 = make_fakes(tmp_path / "engine3", {f"claude-r2d2|{FABLE}": {"behaviour": "timeout_working", "records": 3}})
    sup3 = make(h3, d3)
    turn(sup3, h3, 0)
    text = S.status_text(h3).splitlines()
    ctx = [i for i, l in enumerate(text) if l.startswith("context:")]
    assert ctx and text[ctx[0] + 1].startswith("last turn timeout: ") and "working (3 replies)" in text[ctx[0] + 1]
    sup3.clock.now = T0 + dt.timedelta(hours=25)
    assert not status_lines(h3, "last turn timeout: "), "gone after 24 h"
    _ = sup


# ---------------------------------------------------------------------------- PR #84 review round 1

def test_an_engine_failure_ends_the_shape_streak_so_the_next_miss_is_retried_again(tmp_path):
    """Round 1: a shape miss, then an engine failure of the compaction turn (rc 1, backs off as today), then a shape
    miss after the back-off: the second miss is the first of a new streak (retried once, no back-off), never the
    `second consecutive` one."""
    h, d = make_home(tmp_path / "home"), make_fakes(tmp_path / "engine", {"compact": "no_handoff"})
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    old = session_id(h)
    Path(h, "COMPACT").write_text("founder\n")
    tick(sup, h, 0)
    sup.run_once()
    assert cstate(h)["shape_failures"] == 1 and cstate(h).get("failed_at") is None
    set_table(d, {"compact": "fail"})
    tick(sup, h, 1)
    sup.run_once()
    assert session_id(h) == old and cstate(h)["failed_at"] and not cstate(h).get("shape_failures") and not cstate(h).get("shape_miss")
    assert not Path(h, "COMPACT").exists() and len(buildlog.posted) == 1
    failed_before = cstate(h)["failed_at"]
    set_table(d, {"compact": "no_handoff"})
    Path(h, "COMPACT").write_text("founder\n")
    tick(sup, h, 6 * 60 + 5)  # past the back-off
    assert sup.compaction_due() == (True, "forced")
    sup.run_once()
    assert session_id(h) == old and cstate(h)["shape_failures"] == 1 and cstate(h)["failed_at"] == failed_before, "failed_at left alone"
    assert Path(h, "COMPACT").exists() and len(buildlog.posted) == 1, "the first miss of the new streak: no back-off, no line"
    assert sup.compaction_due() == (True, "forced"), "no new back-off"
    set_table(d, {"compact": "ok"})
    tick(sup, h, 6 * 60 + 6)
    sup.run_once()
    cc = compaction_calls(d)
    assert HANDOFF_MISS in cc[-1]["stdin"] and session_id(h) != old and not cstate(h).get("shape_failures")


def test_reconcile_queues_an_old_working_timeout_line_however_late_the_restart(tmp_path):
    """Round 1: the row is the durable record and the receipts deduplicate forever, so a row older than any
    cutoff whose enqueue was lost still gets its one line at the next start."""
    h, d = make_home(tmp_path / "home"), make_fakes(tmp_path / "engine")
    row = {"n": 7, "at": (T0 - dt.timedelta(days=30)).timestamp(), "engine": "claude-r2d2", "model": FABLE,
           "events": ["e1"], "duration_s": 1800.4, "error": "claude-r2d2: timeout after 1800s",
           "timeout": "working", "transcript_records": 2, "timeout_at": (T0 - dt.timedelta(days=30)).isoformat()}
    S.append_jsonl(os.path.join(h, "logs", "turns.jsonl"), row)
    make(h, d)
    line = notices(h, "manager turn timeout: ")
    assert len(line) == 1 and line[0]["id"] == "timeout-7" and line[0]["text"].startswith(f"manager turn timeout: turn 7 on claude-r2d2 ({FABLE}) ran 1800 s while working (2 replies)")
    make(h, d)
    assert len(notices(h, "manager turn timeout: ")) == 1, "exactly once"


def test_working_timeout_line_names_the_step_that_timed_out_not_the_configured_pair(tmp_path, monkeypatch):
    """Round 1: during a fault episode's probe or a fallback the step that ran out of time is not the configured
    pair; the turn row and the line name the step (`timeout_engine`/`timeout_model`), the row's `engine` stays the
    configured pair as in every failed turn."""
    h, d = make_home(tmp_path / "home"), make_fakes(tmp_path / "engine")
    sup = make(h, d)

    def probe_timed_out(message, notes):
        exc = S.EngineFault([("claude-l", "timeout after 3s")], uncertain=True)
        exc.reasons, exc.step, exc.counted = [f"tier 1 claude-l ({FABLE}): timeout"], None, True
        exc.timeout_info = {"timeout": "working", "transcript_records": 2, "acc": "claude-l", "model": FABLE}
        raise exc
    monkeypatch.setattr(sup, "run_engines", probe_timed_out)
    eid = turn(sup, h, 0)
    row = turns(h)[-1]
    assert (row["engine"], row["timeout_engine"], row["timeout_model"], row["timeout"]) == ("claude-r2d2", "claude-l", FABLE, "working")
    line = notices(h, "manager turn timeout: ")
    assert len(line) == 1 and line[0]["text"].startswith(f"manager turn timeout: turn {row['n']} on claude-l ({FABLE}) ran ")
    assert eid in S.handled_ids(h) and not Path(h, "logs", "fault.json").exists()
    # the real path records the step too
    d2 = make_fakes(tmp_path / "engine2", {f"claude-r2d2|{FABLE}": {"behaviour": "timeout_working", "records": 1}})
    h2 = make_home(tmp_path / "home2")
    turn(make(h2, d2), h2, 0)
    assert (turns(h2)[-1]["timeout_engine"], turns(h2)[-1]["timeout_model"]) == ("claude-r2d2", FABLE)
