"""loops/b14.md: the session-in-use repair (requirements 1-2) and the rule-4 safety valve (3-5). Fake engines in
temporary homes, a controlled clock, a 3 s engine timeout; the fake Claude CLI creates its transcript on every
start and refuses `--session-id` on an id whose transcript exists, as the real CLI does. Fixtures: the sanitized
rows of both incidents (spec appendix)."""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import S, queue_event

FABLE, ASTRA, OPUS = "claude-fable-5-1", "gpt-6-astra", "claude-opus-5-5"
OLD_ID, SID = "79ef53fc-ccf9-4a02-a47f-9560830630d5", "8a4e6f56-1164-4a91-b014-728c29e353dd"
# the sanitized rows of both incidents (loops/b14.md appendix)
ROWS_OCT14 = [
    {"at": "2026-10-14T17:49:23Z", "engine": "claude-r2d2", "model": FABLE, "rc": 124, "classification": "other", "reason": "timeout after 1800s", "success": False},
    {"at": "2026-10-14T17:54:25Z", "engine": "claude-r2d2", "model": FABLE, "rc": 1, "classification": "other", "reason": "Error: Session ID ccfd78e6-d585-4715-a07e-dd1b829ed236 is already in use.", "success": False},
    {"at": "2026-10-14T17:54:26Z", "engine": "claude-l", "model": FABLE, "rc": 1, "classification": "other", "reason": "Error: Session ID ccfd78e6-d585-4715-a07e-dd1b829ed236 is already in use.", "success": False},
    {"at": "2026-10-14T18:07:26Z", "engine": "codex", "model": ASTRA, "rc": 0, "classification": None, "reason": "", "success": True},
]
ROWS_OCT16 = [
    {"at": "2026-10-16T19:30:26Z", "engine": "claude-l", "model": FABLE, "rc": 1, "classification": "other", "reason": f"Error: Session ID {SID} is already in use.", "success": False},
    {"at": "2026-10-16T19:35:35Z", "engine": "claude-l", "model": FABLE, "rc": 1, "classification": "other", "reason": f"Error: Session ID {SID} is already in use.", "success": False},
    {"at": "2026-10-16T19:40:44Z", "engine": "claude-l", "model": FABLE, "rc": 1, "classification": "other", "reason": f"Error: Session ID {SID} is already in use.", "success": False},
]
T0 = dt.datetime(2026, 10, 16, 0, 0, tzinfo=dt.timezone.utc)
MIN = dt.timedelta(minutes=1)

FAKE = r'''#!/usr/bin/env python3
import sys, os, json, time
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]
acc = {'fake-claude-r2d2': 'claude-r2d2', 'fake-claude-l': 'claude-l'}.get(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''), 'codex')
if '%(kind)s' == 'codex':
    model = argv[argv.index('-m') + 1] if '-m' in argv else ''
    table = json.loads((D / 'table.json').read_text())
    b = table.get('codex|' + model, 'ok')
    open(D / 'calls.jsonl', 'a').write(json.dumps({'acc': 'codex', 'model': model, 'argv': argv, 'resume': False}) + '\n')
    if b != 'ok':
        print({'usage_limit': 'usage limit reached', 'credits': 'out of credits'}[b], file=sys.stderr); sys.exit(1)
    Path(argv[argv.index('-o') + 1]).write_text('REPLY: ok\n---HANDOFF---\ntracks: t\nwaiting on: n\nlast decision: n\nnext action: n\nopen question: n\n')
    sys.exit(0)
model = argv[argv.index('--model') + 1]
resume = '--resume' in argv
sid = argv[argv.index('--resume') + 1] if resume else argv[argv.index('--session-id') + 1]
table = json.loads((D / 'table.json').read_text())
b = table.get(acc + '|' + model, 'ok')
project = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects' / os.getcwd().replace('/', '-')
transcript = project / (sid + '.jsonl')
existed = transcript.exists()
msg = sys.stdin.read()
open(D / 'calls.jsonl', 'a').write(json.dumps({'acc': acc, 'model': model, 'argv': argv, 'resume': resume, 'sid': sid, 'existed': existed}) + '\n')
in_use = 'Error: Session ID ' + sid + ' is already in use.'
def refuse(t):
    print(t, file=sys.stderr); sys.exit(1)
if b == 'stuck' or (b == 'session_in_use' and not resume) or (not resume and existed and b not in ('credits', 'other')):
    refuse(in_use)
project.mkdir(parents=True, exist_ok=True)
open(transcript, 'a').write(json.dumps({'type': 'user', 'sessionId': sid}) + '\n')
if b == 'timeout':
    time.sleep(30)
if b == 'credits':
    refuse("You're out of usage credits. Switch to another model to continue.")
if b == 'other':
    refuse('a plain runtime crash')
print('REPLY: ok\n---HANDOFF---\ntracks: t\nwaiting on: n\nlast decision: n\nnext action: n\nopen question: n')
'''


class Fakes:
    def __init__(self, d, table=None):
        self.d = d
        self.table(table or {})
        for kind in ("claude", "codex"):
            p = d / kind
            p.write_text(FAKE % {"dir": str(d), "kind": kind})
            p.chmod(0o755)

    def table(self, t):
        (self.d / "table.json").write_text(json.dumps(t))

    def engines(self):
        return {"claude-r2d2": {"bin": str(self.d / "claude"), "cred": "claude-r2d2.env"},
                "claude-l": {"bin": str(self.d / "claude"), "cred": "claude-l.env"},
                "codex": {"bin": str(self.d / "codex"), "cred": None}}

    def calls(self):
        p = self.d / "calls.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def invoked(self):
        return [(c["acc"], c["model"]) for c in self.calls()]

    def forms(self):
        return [(c["acc"], "resume" if c["resume"] else "session-id") for c in self.calls() if c["acc"] != "codex"]


class Poster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += dt.timedelta(seconds=1)
        return self.now


def setup(home, tmp_path, table=None, engine=None, started=False):
    with open(os.path.join(home, "engine"), "w") as f:
        f.write(json.dumps(engine or {"acc": "claude-r2d2", "model": FABLE}) + "\n")
    with open(os.path.join(home, "config.json"), "w") as f:
        f.write(json.dumps({"dev_channel": "C_DEV"}))
    with open(os.path.join(home, "session-id"), "w") as f:
        f.write(SID + "\n")
    with open(os.path.join(home, "rollover-intent.json"), "w") as f:
        f.write(json.dumps({"old_id": OLD_ID, "new_id": SID, "started": started}) + "\n")
    (tmp_path / "eng").mkdir(exist_ok=True)
    fk = Fakes(tmp_path / "eng", table)
    sup = S.Supervisor(home=home, engines=fk.engines(), poster=Poster(), buildlog_poster=Poster(), clock=Clock(), engine_timeout=3)
    return fk, sup


def turn(sup, home, minutes, text="go"):
    sup.clock.now = T0 + minutes * MIN
    retry = Path(home, "logs", "retry-after")
    if retry.exists():
        retry.unlink()
    eid = queue_event(home, text)
    assert sup.run_once() is True
    return eid


def fault(home):
    p = Path(home, "logs", "fault.json")
    return json.loads(p.read_text()) if p.exists() else {}


def intent(home):
    return json.loads(Path(home, "rollover-intent.json").read_text())


def outbox(home, needle):
    return [it for it in S.read_outbox(home) if needle in (it.get("text") or "")]


def engine_file(home):
    return json.loads(Path(home, "engine").read_text())


# ---- requirements 1 and 2: the repair, replayed from both incidents

def test_the_session_in_use_regex_matches_both_incidents_and_nothing_else():
    for row in ROWS_OCT14[1:3] + ROWS_OCT16:
        assert S.SESSION_IN_USE_RE.search(row["reason"]), row
    assert not S.SESSION_IN_USE_RE.search(ROWS_OCT14[0]["reason"]) and not S.SESSION_IN_USE_RE.search("usage limit reached")


def test_oct15_timeout_then_resume_recovers_on_the_next_turn(home, tmp_path):
    """Turn 1766's shape: the first start on the rolled-over id times out after the CLI created the transcript; the
    next turn resumes it (requirement 1) and marks the session started; the timed-out request is never replayed."""
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "timeout"})
    eid = turn(sup, home, 0)
    assert fk.forms() == [("claude-r2d2", "session-id")]
    rows = S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl"))
    assert rows[-1]["rc"] == 124 and rows[-1]["reason"].startswith("timeout after") == ROWS_OCT14[0]["reason"].startswith("timeout after")
    assert intent(home)["started"] is False and eid in S.handled_ids(home)
    assert S.find_claude_transcript(os.path.join(home, ".claude"), home, SID)
    fk.table({})
    eid2 = turn(sup, home, 5)
    assert fk.forms()[1:] == [("claude-r2d2", "resume")] and eid2 in S.handled_ids(home)
    assert intent(home)["started"] is True and fault(home).get("episode_id") is None


def test_oct14_in_use_without_a_transcript_retries_resume_in_the_same_turn(home, tmp_path):
    """Row 1110's shape: `--session-id` refused "already in use"; the same turn retries once with `--resume`
    (requirement 2); one attempt row, success, retried_resume true."""
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "session_in_use"})
    eid = turn(sup, home, 0)
    assert fk.forms() == [("claude-r2d2", "session-id"), ("claude-r2d2", "resume")] and fk.calls()[0]["existed"] is False
    rows = S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl"))
    assert len(rows) == 1 and rows[0]["success"] is True and rows[0]["retried_resume"] is True
    assert eid in S.handled_ids(home) and intent(home)["started"] is True and not outbox(home, "engine fault")


def test_a_refused_resume_is_a_fault_with_no_second_retry(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "stuck"}, started=True)
    eid = turn(sup, home, 0)
    assert fk.forms() == [("claude-r2d2", "resume")] and eid not in S.handled_ids(home)
    rows = S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl"))
    assert len(rows) == 1 and not rows[0]["success"] and "retried_resume" not in rows[0]
    assert fault(home)["count"] == 1 and fault(home)["step"] == {"tier": 1, "acc": "claude-r2d2", "model": FABLE}


def test_session_resumable_marks_the_intent_only_when_the_transcript_exists(home, tmp_path):
    fk, sup = setup(home, tmp_path)
    assert sup._session_resumable(SID) is False and intent(home)["started"] is False
    p = Path(home, ".claude", "projects", os.path.abspath(home).replace("/", "-"), f"{SID}.jsonl")
    p.parent.mkdir(parents=True)
    p.write_text("{}\n")
    assert sup._session_resumable(SID) is True and intent(home)["started"] is True
    assert sup._session_resumable("another-id") is True, "an id the intent does not name resumes as before"


# ---- requirements 3 and 4: the valve

def _escalate(home, tmp_path, engine=None):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "stuck"}, engine=engine)
    for m in (0, 5, 10):
        turn(sup, home, m)
    assert fault(home)["count"] == 3 and not fault(home).get("escalated") and engine_file(home)["acc"] == "claude-r2d2"
    turn(sup, home, 35)
    return fk, sup


def test_three_faults_over_thirty_minutes_escalate_to_the_other_account(home, tmp_path):
    fk, sup = _escalate(home, tmp_path)
    assert fk.invoked()[-2:] == [("claude-r2d2", FABLE), ("claude-l", FABLE)]
    assert engine_file(home) == {"acc": "claude-l", "model": FABLE}
    f = fault(home)
    assert f["escalated"] is True and f["count"] == 4 and f["episode_id"]
    esc = outbox(home, "engine fault escalated: ")
    assert len(esc) == 1 and esc[0]["text"] == f"engine fault escalated: tier 1 claude-r2d2 ({FABLE}): other: Error: Session ID {SID} is already in use."
    assert esc[0]["id"] == f"fault-{f['episode_id']}-escalated" and esc[0]["thread_ts"] is None
    assert len(outbox(home, "fallback started")) == 1
    due = S.parse_iso(f["probe_after"]) - sup.clock.now
    assert dt.timedelta(seconds=3590) <= due <= dt.timedelta(seconds=3600)


@pytest.mark.parametrize("minutes,expect_escalated", [((0, 5, 10), False), ((0, 60), False), ((0, 5, 10, 35), True)])
def test_the_threshold_needs_count_three_and_thirty_minutes(home, tmp_path, minutes, expect_escalated):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "stuck"})
    for m in minutes:
        turn(sup, home, m)
    assert bool(fault(home).get("escalated")) is expect_escalated
    assert (engine_file(home)["acc"] == "claude-l") is expect_escalated


def test_the_return_at_the_hourly_probe_closes_the_episode(home, tmp_path):
    fk, sup = _escalate(home, tmp_path, engine={"acc": "claude-r2d2", "model": FABLE, "mode": "per-turn"})
    n = len(fk.calls())
    turn(sup, home, 60)
    assert fk.invoked()[n:] == [("claude-l", FABLE)], "inside the hour the faulted step is not called"
    fk.table({})
    n = len(fk.calls())
    turn(sup, home, 96)
    assert fk.invoked()[n:] == [("claude-r2d2", FABLE)] and fk.forms()[-1] == ("claude-r2d2", "resume")
    assert engine_file(home) == {"acc": "claude-r2d2", "model": FABLE, "mode": "per-turn"}, "the mode key is kept"
    assert fault(home)["episode_id"] is None and len(outbox(home, "fallback ended")) == 1


def test_a_faulting_probe_keeps_the_effective_pair_and_reschedules(home, tmp_path):
    fk, sup = _escalate(home, tmp_path)
    n = len(fk.calls())
    turn(sup, home, 96)
    assert fk.invoked()[n:] == [("claude-r2d2", FABLE), ("claude-l", FABLE)]
    f = fault(home)
    assert f["escalated"] and f["count"] == 5 and S.parse_iso(f["probe_after"]) > sup.clock.now + dt.timedelta(seconds=3500)
    assert len(outbox(home, "engine fault escalated: ")) == 1


def test_escalation_walks_to_astra_and_the_floor_never_below(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-l|{FABLE}": "stuck", f"claude-r2d2|{FABLE}": "credits", f"codex|{ASTRA}": "usage_limit"},
                    engine={"acc": "claude-l", "model": FABLE})
    for m in (0, 5, 10):
        turn(sup, home, m)
    n = len(fk.calls())
    turn(sup, home, 35)
    assert fk.invoked()[n:] == [("claude-l", FABLE), ("claude-r2d2", FABLE), ("codex", ASTRA), ("claude-r2d2", OPUS)]
    assert engine_file(home) == {"acc": "claude-r2d2", "model": OPUS}
    assert all(a.get("model") in (FABLE, ASTRA, OPUS) for a in S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl")))


def test_an_explicit_selection_closes_an_escalated_episode(home, tmp_path):
    fk, sup = _escalate(home, tmp_path)
    S.set_engine(home, "claude-l", FABLE, fk.engines())
    assert fault(home)["episode_id"] is None and fault(home)["closed_by"] == "explicit selection"


# ---- requirement 5: status and the digest event

def test_status_and_digest_name_a_long_episode_from_the_file(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "stuck"})
    turn(sup, home, 0)
    turn(sup, home, 29)
    assert not [l for l in S.status_text(home).splitlines() if l.startswith("fault episode:")]
    turn(sup, home, 35)  # count 3 at 35 min: escalated, and the line says so
    f = fault(home)
    lines = [l for l in S.status_text(home).splitlines() if l.startswith("fault episode:")]
    assert lines == [f"fault episode: tier 1 claude-r2d2 ({FABLE}) since {S._status_pdt(f['since'])}, 3 faults over 35 min, escalated at {S._status_pdt(f['escalated_at'])}"]
    sup.add_timer_event(now=1_800_000_000, text="timer: daily digest", digest=True)
    ev = [e for e in S.pending_events(home) if (e.get("payload") or {}).get("digest")]
    assert ev and ev[-1]["payload"]["text"].splitlines() == ["timer: daily digest", lines[0]]


def test_the_oct16_episode_line_from_the_real_timeline(home, tmp_path):
    """logs/fault.json as the 2026-10-16 outage left it (since 00:00:16Z, faults every five minutes until 20:06Z):
    the line names the step, the start and the span; the real file lacked count/last_at, which reads as nothing."""
    fk, sup = setup(home, tmp_path)
    real = {"episode_id": "f791c1b2-82a7-4bb1-a954-f7c3b8a465d5", "since": "2026-10-16T00:00:16Z", "turn": 1773,
            "text": f"engine fault: tier 1 claude-l ({FABLE}): other: Error: Session ID {SID} is already in use.", "queued": True}
    Path(home, "logs", "fault.json").write_text(json.dumps(real))
    assert S._fault_episode_line(home) is None, "a pre-b14 episode without last_at shows nothing rather than a wrong number"
    real.update({"last_at": "2026-10-16T20:06:31Z", "count": 235, "step": {"tier": 1, "acc": "claude-l", "model": FABLE}})
    Path(home, "logs", "fault.json").write_text(json.dumps(real))
    line = S._fault_episode_line(home)
    assert line == f"fault episode: tier 1 claude-l ({FABLE}) since {S._status_pdt(real['since'])}, 235 faults over 1206 min"
    assert line in S.status_text(home)
