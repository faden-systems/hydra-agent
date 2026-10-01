"""The transition read at a family switch (loops/b3.md section 3): switch detection from the ledger (family, not
account), `since` from the incoming family's last turn, the header and ledger field, the saved window, config
defaults, the disabled flag and a missing transcript."""
import datetime as dt
import json
import os
import re
import time

from conftest import S, calls, engines, fake_engine, queue_event  # noqa: F401
import transcript as Tr  # noqa: E402

T0 = dt.datetime(2026, 10, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


class Clock:
    """Every call advances one second from T0: ledger times are monotonic and known."""

    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        self.now = self.now + dt.timedelta(seconds=1)
        return self.now

    def set(self, seconds):
        self.now = T0 + dt.timedelta(seconds=seconds)


def ts(seconds):
    return (T0 + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_ledger(mem, rows):
    os.makedirs(mem, exist_ok=True)
    with open(os.path.join(mem, "LEDGER.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def claude_transcript(home, lines, sid="sess-test", cwd=None):
    d = os.path.join(home, ".claude", "projects", (cwd or home).replace("/", "-"))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{sid}.jsonl")
    with open(p, "w") as f:
        for at, role, content in lines:
            f.write(json.dumps({"type": role, "timestamp": at, "sessionId": sid, "message": {"role": role, "content": content}}) + "\n")
    return p


def codex_rollout(codex_home, cwd, lines, name="rollout-2026-10-01T12-00-00-abc.jsonl"):
    d = os.path.join(codex_home, "sessions", "2026", "10", "01"); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(json.dumps({"timestamp": ts(0), "type": "session_meta", "payload": {"cwd": cwd, "id": "abc"}}) + "\n")
        for at, role, text in lines:
            f.write(json.dumps({"timestamp": at, "type": "response_item", "payload": {"type": "message", "role": role,
                                "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}}) + "\n")
    return p


def make_sup(home, poster, tmp_path, clock=None, config=None):
    dcl = str(tmp_path / "cl"); os.makedirs(dcl, exist_ok=True); dcx = str(tmp_path / "cx"); os.makedirs(dcx, exist_ok=True)
    cl = fake_engine(dcl, "ok"); cx = fake_engine(dcx, "ok", name="codex")
    codex_home = str(tmp_path / "codex-home"); os.makedirs(codex_home, exist_ok=True)
    sup = S.Supervisor(home=home, engines=engines(cl, cl, cx), poster=poster, codex_home=codex_home,
                       clock=clock or Clock(), config=config)
    return sup, dcl, dcx


def set_engine(home, acc, model="claude-fable-5-1"):
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": acc, "model": model}))


def transition_lines(stdin):
    return [l for l in stdin.splitlines() if l.startswith("[transition]")]


# ----------------------------------------------------------------------------------------------- pure pieces

def test_switch_is_detected_by_family_from_the_ledger_not_by_account(home, poster, tmp_path):
    sup, _, _ = make_sup(home, poster, tmp_path)
    assert sup.switch_for("codex") is None, "no ledger yet: nothing to switch from"
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "claude-r2d2", "model": "m", "files_written": []},
                                  {"turn": 2, "at": ts(2), "engine": "codex", "model": "m", "files_written": []},
                                  {"turn": 3, "at": ts(3), "engine": "claude-l", "model": "m", "files_written": []}])
    assert sup.switch_for("claude-r2d2") is None, "claude-l -> claude-r2d2 is the same family"
    assert sup.switch_for("claude-l") is None
    assert sup.switch_for("codex") == ("claude", "codex")
    assert sup.family_last_at("codex") == ts(2) and sup.family_last_at("claude") == ts(3)
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "claude-r2d2", "model": "m", "files_written": []}])
    assert sup.family_last_at("codex") is None, "a family that never ran reads from the start of the transcript"


def test_config_defaults_and_overrides(home, poster, tmp_path):
    sup, _, _ = make_sup(home, poster, tmp_path)
    assert sup.transition_config() == {"max_tokens": 100000, "tool_result_max_chars": 4000, "enabled": True}
    assert S.TRANSITION_DEFAULTS == {"max_tokens": 100000, "tool_result_max_chars": 4000, "enabled": True}
    open(os.path.join(home, "config.json"), "w").write(json.dumps({"transition": {"max_tokens": 2000, "enabled": False}}))
    sup2, _, _ = make_sup(home, poster, tmp_path)
    assert sup2.transition_config() == {"max_tokens": 2000, "tool_result_max_chars": 4000, "enabled": False}
    sup3, _, _ = make_sup(home, poster, tmp_path, config={"transition": {"tool_result_max_chars": 10}})
    assert sup3.transition_config()["tool_result_max_chars"] == 10 and sup3.transition_config()["max_tokens"] == 100000


def test_header_text_is_exact():
    assert S.TRANSITION_HEADER.format(a="claude", b="codex", at="X") == (
        "[transition] engine family switched from claude to codex at X. Below is the other engine's transcript since the "
        "last switch, flattened, nothing summarized. Read it fully before acting. Then write to MEMORY.md anything in it "
        "that must survive the next switch.")


def test_claude_md_carries_the_transition_rule():
    text = open(os.path.join(os.path.dirname(S.__file__), "CLAUDE.md")).read()
    assert "After a `[transition]` block: read it entirely before acting." in text
    assert "dated and tagged with the engine that originally said it" in text


# ----------------------------------------------------------------------------------------------- turns

def test_claude_to_codex_switch_carries_the_transcript_inline_and_in_the_ledger(home, poster, tmp_path):
    clock = Clock()
    sup, dcl, dcx = make_sup(home, poster, tmp_path, clock=clock)
    queue_event(home, "warm up"); assert sup.run_once() is True
    rows = S.read_ledger(sup.memory_dir)
    assert rows[-1]["engine"] == "claude-r2d2" and "transition" not in rows[-1] and rows[-1]["at"] == ts(2)
    big = "z" * 9000
    claude_transcript(home, [
        (ts(30), "user", "the vendor is Kobiton, decided today"),
        (ts(35), "assistant", [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "Noted: Kobiton."}]),
        (ts(40), "assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "cat big.log"}}]),
        (ts(42), "user", [{"type": "tool_result", "content": big}]),
    ])
    clock.set(60)
    set_engine(home, "codex", "gpt-6-astra")
    queue_event(home, "which vendor?"); assert sup.run_once() is True
    stdin = calls(dcx)[-1]["stdin"]
    memory = [l for l in stdin.splitlines() if l.startswith("[memory]")]
    assert len(memory) == 3 and stdin.index("[memory]") < stdin.index("[transition]") < stdin.index("# MANAGER-HANDOFF.md") < stdin.index("Manager turn.")
    head = transition_lines(stdin)[0]
    assert head == S.TRANSITION_HEADER.format(a="claude", b="codex", at=ts(61))
    after = stdin[stdin.index("[transition]"):]
    assert after.splitlines()[1].startswith("Transcript window: 12:00Z to 12:00Z, 4 of 4 entries; 0 earlier entries not included.")
    assert "12:00Z user: the vendor is Kobiton, decided today" in after and "Noted: Kobiton." in after
    assert "private" not in after and 'tool Bash({"command": "cat big.log"})' in after and big not in after and "more chars omitted]" in after
    assert calls(dcx)[-1]["env"]["CODEX_HOME"] == sup.codex_home
    rows = S.read_ledger(sup.memory_dir)
    tr = rows[-1]["transition"]
    assert tr == {"from": "claude", "to": "codex", "source_path": os.path.join(home, ".claude", "projects", home.replace("/", "-"), "sess-test.jsonl"),
                  "since": None, "first_at": ts(30), "last_at": ts(42), "entries_kept": 4, "entries_total": 4, "est_tokens": tr["est_tokens"]}
    assert tr["est_tokens"] > 1000
    saved = os.path.join(sup.memory_dir, "transition", f"claude-to-codex-{ts(61)}.md")
    assert os.path.isfile(saved) and open(saved).read() == after.split("\n\n# MANAGER-HANDOFF.md")[0] + "\n"
    assert not any(p.startswith("transition/") for p in rows[-1]["files_written"]), "the record is the supervisor's, not the engine's"
    assert rows[-1]["at"] == ts(63) and open(sup.handoff_path()).read().startswith(f"updated_at: {ts(62)}\nengine: codex\n")


def test_since_is_the_incoming_familys_last_turn_and_reverse_direction_reads_the_rollout(home, poster, tmp_path):
    clock = Clock()
    sup, dcl, dcx = make_sup(home, poster, tmp_path, clock=clock)
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "claude-r2d2", "model": "m", "files_written": []},
                                  {"turn": 2, "at": ts(50), "engine": "codex", "model": "m", "files_written": []},
                                  {"turn": 3, "at": ts(100), "engine": "claude-l", "model": "m", "files_written": []}])
    claude_transcript(home, [(ts(10), "user", "OLD: before codex last ran"), (ts(50), "user", "AT: the moment codex last ran"),
                             (ts(120), "user", "NEW: after codex last ran")])
    clock.set(200); set_engine(home, "codex", "gpt-6-astra")
    queue_event(home, "q"); assert sup.run_once() is True
    stdin = calls(dcx)[-1]["stdin"]
    assert "NEW: after" in stdin and "AT: the moment" in stdin and "OLD: before" not in stdin
    tr = S.read_ledger(sup.memory_dir)[-1]["transition"]
    assert tr["since"] == ts(50) and tr["entries_total"] == 2 and tr["entries_kept"] == 2 and tr["first_at"] == ts(50)
    # back to claude: the rollout since claude's last turn (turn 3 at ts(100)); the account differs but the family is the same
    codex_rollout(sup.codex_home, home, [(ts(90), "user", "stale question"), (ts(150), "assistant", "the contract starts October 15, code DF-7")])
    clock.set(300); set_engine(home, "claude-r2d2")
    queue_event(home, "when?"); assert sup.run_once() is True
    stdin = calls(dcl)[-1]["stdin"]
    assert transition_lines(stdin)[0].startswith("[transition] engine family switched from codex to claude at " + ts(301))
    assert "code DF-7" in stdin and "stale question" not in stdin
    tr = S.read_ledger(sup.memory_dir)[-1]["transition"]
    assert tr["from"] == "codex" and tr["to"] == "claude" and tr["since"] == ts(100) and tr["source_path"].endswith("rollout-2026-10-01T12-00-00-abc.jsonl")
    assert os.path.isfile(os.path.join(sup.memory_dir, "transition", f"codex-to-claude-{ts(301)}.md"))
    # same family again: no block, no field
    set_engine(home, "claude-l")
    queue_event(home, "still there?"); assert sup.run_once() is True
    assert "[transition]" not in calls(dcl)[-1]["stdin"] and "transition" not in S.read_ledger(sup.memory_dir)[-1]


def test_window_budget_from_config(home, poster, tmp_path):
    clock = Clock()
    open(os.path.join(home, "config.json"), "w").write(json.dumps({"transition": {"max_tokens": 300}}))
    sup, dcl, dcx = make_sup(home, poster, tmp_path, clock=clock)
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "claude-r2d2", "model": "m", "files_written": []}])
    claude_transcript(home, [(ts(10 + m), "user", f"filler {m} " + "y" * 200) for m in range(10)] + [(ts(30), "user", "NEWEST-42")])
    clock.set(100); set_engine(home, "codex", "gpt-6-astra")
    queue_event(home, "q"); assert sup.run_once() is True
    stdin = calls(dcx)[-1]["stdin"]
    assert "NEWEST-42" in stdin and "filler 0 " not in stdin
    tr = S.read_ledger(sup.memory_dir)[-1]["transition"]
    assert tr["entries_total"] == 11 and tr["entries_kept"] < 11 and tr["est_tokens"] <= 300 and tr["last_at"] == ts(30)
    assert f"{11 - tr['entries_kept']} earlier entries not included." in stdin


def test_disabled_flag_skips_the_read(home, poster, tmp_path):
    open(os.path.join(home, "config.json"), "w").write(json.dumps({"transition": {"enabled": False}}))
    sup, dcl, dcx = make_sup(home, poster, tmp_path)
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "claude-r2d2", "model": "m", "files_written": []}])
    claude_transcript(home, [(ts(10), "user", "Kobiton")])
    set_engine(home, "codex", "gpt-6-astra")
    queue_event(home, "q"); assert sup.run_once() is True
    stdin = calls(dcx)[-1]["stdin"]
    assert "[transition]" not in stdin and "Kobiton" not in stdin and stdin.count("[memory]") == 3
    assert "transition" not in S.read_ledger(sup.memory_dir)[-1]
    assert not os.path.isdir(os.path.join(sup.memory_dir, "transition"))


def test_missing_transcript_is_one_line_and_the_turn_proceeds(home, poster, tmp_path):
    clock = Clock()
    sup, dcl, dcx = make_sup(home, poster, tmp_path, clock=clock)
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "claude-r2d2", "model": "m", "files_written": []}])
    set_engine(home, "codex", "gpt-6-astra")
    queue_event(home, "q"); assert sup.run_once() is True
    stdin = calls(dcx)[-1]["stdin"]
    lines = transition_lines(stdin)
    assert len(lines) == 1 and lines[0].startswith(f"[transition] engine family switched from claude to codex at {ts(1)}: ")
    assert f"no claude transcript found for {home}" in lines[0] and "Transcript window:" not in stdin
    assert stdin.index("[memory]") < stdin.index("[transition]") < stdin.index("Manager turn.")
    tr = S.read_ledger(sup.memory_dir)[-1]["transition"]
    assert tr["from"] == "claude" and tr["to"] == "codex" and tr["source_path"] is None and tr["entries_total"] == 0 and tr["est_tokens"] == 0
    assert not os.path.isdir(os.path.join(sup.memory_dir, "transition"))
    # the other direction with an empty codex home
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "codex", "model": "m", "files_written": []}])
    set_engine(home, "claude-l")
    queue_event(home, "q2"); assert sup.run_once() is True
    lines = transition_lines(calls(dcl)[-1]["stdin"])
    assert len(lines) == 1 and "from codex to claude" in lines[0] and f"no codex transcript found for {home}" in lines[0] and sup.codex_home in lines[0]


def test_a_rollout_from_another_cwd_is_never_the_transition_read(home, poster, tmp_path):
    clock = Clock()
    sup, dcl, dcx = make_sup(home, poster, tmp_path, clock=clock)
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "codex", "model": "m", "files_written": []}])
    codex_rollout(sup.codex_home, "/somewhere/else", [(ts(5), "assistant", "UNRELATED-PROJECT-FACT")], name="rollout-2026-10-01T12-00-00-zzz.jsonl")
    set_engine(home, "claude-l")
    queue_event(home, "anything new?"); assert sup.run_once() is True
    stdin = calls(dcl)[-1]["stdin"]
    lines = transition_lines(stdin)
    assert "UNRELATED-PROJECT-FACT" not in stdin and len(lines) == 1 and f"no codex transcript found for {home}" in lines[0]
    tr = S.read_ledger(sup.memory_dir)[-1]["transition"]
    assert tr["source_path"] is None and tr["entries_total"] == 0 and not os.path.isdir(os.path.join(sup.memory_dir, "transition"))
    # the same rollout for this cwd is read; the other project's is still ignored
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "codex", "model": "m", "files_written": []}])
    codex_rollout(sup.codex_home, home, [(ts(6), "assistant", "THIS-PROJECT-FACT")], name="rollout-2026-10-01T11-00-00-abc.jsonl")
    queue_event(home, "again?"); assert sup.run_once() is True
    stdin = calls(dcl)[-1]["stdin"]
    assert "THIS-PROJECT-FACT" in stdin and "UNRELATED-PROJECT-FACT" not in stdin
    assert S.read_ledger(sup.memory_dir)[-1]["transition"]["source_path"].endswith("rollout-2026-10-01T11-00-00-abc.jsonl")


def test_switch_after_a_failed_attempt_reads_for_the_engine_that_ran(home, poster, tmp_path):
    """claude-r2d2 and claude-l both out of quota: the codex attempt is the switch, computed for codex."""
    dq = str(tmp_path / "q"); os.makedirs(dq); dcx = str(tmp_path / "cx"); os.makedirs(dcx)
    quota = fake_engine(dq, "quota"); cx = fake_engine(dcx, "ok", name="codex")
    clock = Clock()
    sup = S.Supervisor(home=home, engines=engines(quota, quota, cx), poster=poster, codex_home=str(tmp_path / "ch"), clock=clock)
    write_ledger(sup.memory_dir, [{"turn": 1, "at": ts(1), "engine": "claude-l", "model": "m", "files_written": []}])
    claude_transcript(home, [(ts(10), "user", "Kobiton")])
    queue_event(home, "q"); assert sup.run_once() is True
    stdin = calls(dcx)[-1]["stdin"]
    assert "from claude to codex" in stdin and "Kobiton" in stdin
    assert S.read_ledger(sup.memory_dir)[-1]["transition"]["to"] == "codex"
    assert len(os.listdir(os.path.join(sup.memory_dir, "transition"))) == 1, "only the attempt that ran leaves a record"


def test_rollout_hint_from_codex_output_is_recorded_and_preferred(home, poster, tmp_path):
    dcx = str(tmp_path / "cx"); os.makedirs(dcx)
    ch = str(tmp_path / "ch")
    hinted = codex_rollout(ch, "/elsewhere", [(ts(5), "assistant", "HINTED fact")], name="rollout-2026-10-01T12-00-00-hint.jsonl")
    codex_rollout(ch, home, [(ts(6), "assistant", "OTHER fact")], name="rollout-2026-10-01T13-00-00-other.jsonl")
    p = os.path.join(dcx, "codex")
    open(p, "w").write("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
                       f"print('session saved to {hinted}', file=sys.stderr)\nprint('REPLY: ok')\nprint('---HANDOFF---')\nprint('tracks: t1')\n")
    os.chmod(p, 0o755)
    dcl = str(tmp_path / "cl"); os.makedirs(dcl); cl = fake_engine(dcl, "ok")
    sup = S.Supervisor(home=home, engines=engines(cl, cl, p), poster=poster, codex_home=ch, clock=Clock())
    set_engine(home, "codex", "gpt-6-astra")
    queue_event(home, "one"); assert sup.run_once() is True
    assert open(os.path.join(home, "logs", "codex-rollout")).read().strip() == hinted
    set_engine(home, "claude-r2d2")
    queue_event(home, "two"); assert sup.run_once() is True
    stdin = calls(dcl)[-1]["stdin"]
    assert "HINTED fact" in stdin and "OTHER fact" not in stdin
    assert S.read_ledger(sup.memory_dir)[-1]["transition"]["source_path"] == hinted


def test_clock_drives_ledger_and_handoff_times_and_defaults_to_now(home, poster, tmp_path):
    sup, dcl, _ = make_sup(home, poster, tmp_path, clock=Clock())
    queue_event(home, "x"); assert sup.run_once() is True
    assert S.read_ledger(sup.memory_dir)[-1]["at"] == ts(2) and open(sup.handoff_path()).read().startswith(f"updated_at: {ts(1)}\n")
    sup2 = S.Supervisor(home=home, engines=engines(fake_engine(dcl, "ok")), poster=poster)
    assert sup2.clock is None and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", sup2.now()) and abs(time.time() - Tr.parse_ts(sup2.now()).timestamp()) < 5
    assert S.Supervisor(home=home, engines={}, poster=poster, clock=lambda: 1800000000.0).now() == "2027-01-15T08:00:00Z"
