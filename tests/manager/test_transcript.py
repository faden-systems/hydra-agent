"""The flatteners (loops/b3.md section 2): the recorded real files (counts by kind match the file, known lines
verbatim, long tool results cut at the cap, reasoning dropped), both Codex tool-call shapes, `since`, `render`,
`window` (cut at an entry boundary, newest kept, meta) and the transcript finders."""
import json
import os
import time

from conftest import ROOT  # conftest puts manager/ on sys.path
import transcript as Tr  # noqa: E402

FX = os.path.join(ROOT, "tests", "manager", "fixtures", "transcripts")
CLAUDE_FX = os.path.join(FX, "claude-session.jsonl")
CODEX_FX = os.path.join(FX, "codex-rollout.jsonl")


def raw(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def by_kind(entries, kind):
    return [e for e in entries if e["kind"] == kind]


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


# ----------------------------------------------------------------------------------------------- the recorded files

def test_claude_fixture_counts_match_the_file():
    entries = Tr.flatten_claude(CLAUDE_FX)
    assert entries and all(set(e) == {"at", "role", "kind", "text"} for e in entries)
    assert {e["kind"] for e in entries} <= {"text", "tool_call", "tool_result"}
    rows = raw(CLAUDE_FX)
    blocks = [(o["type"], b) for o in rows if o.get("type") in ("user", "assistant")
              for b in ((o.get("message") or {}).get("content") or []) if isinstance(b, dict)]
    n_tool_use = sum(1 for _, b in blocks if b["type"] == "tool_use")
    n_tool_result = sum(1 for _, b in blocks if b["type"] == "tool_result")
    n_text_blocks = sum(1 for _, b in blocks if b["type"] == "text")
    n_str = sum(1 for o in rows if o.get("type") in ("user", "assistant") and isinstance((o.get("message") or {}).get("content"), str))
    assert n_tool_use > 0 and len(by_kind(entries, "tool_call")) == n_tool_use
    assert n_tool_result > 0 and len(by_kind(entries, "tool_result")) == n_tool_result
    assert len(by_kind(entries, "text")) == n_text_blocks + n_str, "every text block and every string message is one entry"
    assert all(e["role"] == "assistant" for e in by_kind(entries, "tool_call"))
    assert all(e["text"].startswith("tool ") and "(" in e["text"] for e in by_kind(entries, "tool_call"))
    # nothing else sneaks in: the harness's own records (attachment, queue-operation, cost-state, ...) are not entries
    assert len(entries) == n_tool_use + n_tool_result + n_text_blocks + n_str
    assert sum(1 for o in rows if o.get("type") not in ("user", "assistant")) > 0
    # file order is kept: timestamps never go backwards
    ats = [e["at"] for e in entries if e["at"]]
    assert ats == sorted(ats)


def test_claude_fixture_known_lines_verbatim_and_thinking_dropped():
    entries = Tr.flatten_claude(CLAUDE_FX)
    texts = [e["text"] for e in entries]
    assert "remember the word pelican" in texts, "a known founder line, whole"
    assert "What word did I ask you to remember at the start?" in texts
    first_reply = next(b["text"] for o in raw(CLAUDE_FX) if o.get("type") == "assistant"
                       for b in o["message"]["content"] if b.get("type") == "text")
    assert first_reply in texts, "a known manager reply, whole"
    assert not any("signature" in e["text"] and "thinking" in e["text"] for e in entries)
    assert not any(e["kind"] == "text" and e["role"] == "assistant" and e["text"] == "" for e in entries)


def test_claude_fixture_long_tool_result_cut_at_cap_with_marker():
    rows = raw(CLAUDE_FX)
    longest = max(len(b["content"]) for o in rows if o.get("type") == "user"
                  for b in ((o.get("message") or {}).get("content") or []) if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), str))
    assert longest > 4000, "the recorded file has a tool result longer than the default cap"
    results = by_kind(Tr.flatten_claude(CLAUDE_FX), "tool_result")
    cut = [e for e in results if "more chars omitted]" in e["text"]]
    assert cut and all(len(e["text"]) <= 4000 + 60 for e in cut)
    assert any(e["text"].endswith(f"[... {longest - 4000} more chars omitted]") for e in cut), "N is the exact overflow"
    uncut = by_kind(Tr.flatten_claude(CLAUDE_FX, tool_result_max_chars=longest), "tool_result")
    assert not any("more chars omitted]" in e["text"] for e in uncut)
    assert max(len(e["text"]) for e in uncut) == longest


def test_codex_fixture_counts_match_the_file_and_reasoning_dropped():
    entries = Tr.flatten_codex(CODEX_FX)
    assert entries and all(set(e) == {"at", "role", "kind", "text"} for e in entries)
    rows = raw(CODEX_FX)
    p = lambda o: (o.get("payload") or {})  # noqa: E731
    n_calls = sum(1 for o in rows if p(o).get("type") in ("function_call", "custom_tool_call"))
    n_outs = sum(1 for o in rows if p(o).get("type") in ("function_call_output", "custom_tool_call_output"))
    n_msgs = sum(1 for o in rows if p(o).get("type") == "message" and p(o).get("role") in ("user", "assistant"))
    assert n_calls > 0 and len(by_kind(entries, "tool_call")) == n_calls, "every tool call item is one tool_call entry"
    assert n_outs > 0 and len(by_kind(entries, "tool_result")) == n_outs, "every output item is one tool_result entry"
    assert len(by_kind(entries, "text")) == n_msgs
    assert any(p(o).get("type") == "reasoning" for o in rows), "the recorded rollout holds reasoning items"
    assert not any("encrypted_content" in e["text"] or e["kind"] == "reasoning" for e in entries)
    assert not any("<skills_instructions>" in e["text"] for e in entries), "the harness's developer messages are not conversation"
    texts = [e["text"] for e in entries]
    first_reply = next(b["text"] for o in rows if p(o).get("type") == "message" and p(o).get("role") == "assistant" for b in p(o)["content"])
    assert first_reply in texts, "a known manager reply, whole"
    founder = next(b["text"] for o in rows if p(o).get("type") == "message" and p(o).get("role") == "user"
                   for b in p(o)["content"] if "# MANAGER-HANDOFF.md" in b["text"])
    assert founder in texts, "a known user message, whole"
    calls = by_kind(entries, "tool_call")
    assert all(e["role"] == "assistant" and e["text"].startswith("tool exec(") for e in calls)
    assert any("more chars omitted]" in e["text"] for e in by_kind(entries, "tool_result"))


def test_fixtures_hold_no_token_shaped_strings():
    import re
    for path in (CLAUDE_FX, CODEX_FX):
        text = open(path, encoding="utf-8").read()
        assert not re.search(r"xoxb-[0-9]|xapp-[0-9]|sk-ant-oat|ghp_[A-Za-z0-9]{20}", text)


# ----------------------------------------------------------------------------------------------- synthetic lines

def test_codex_both_tool_call_shapes(tmp_path):
    path = write_jsonl(str(tmp_path / "rollout-x.jsonl"), [
        {"timestamp": "2026-10-01T10:00:00.000Z", "type": "session_meta", "payload": {"cwd": "/x", "id": "s"}},
        {"timestamp": "2026-10-01T10:00:01.000Z", "type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "harness"}]}},
        {"timestamp": "2026-10-01T10:00:02.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "plain string"}},
        {"timestamp": "2026-10-01T10:00:03.000Z", "type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "secret plan"}], "encrypted_content": "zzz"}},
        {"timestamp": "2026-10-01T10:00:04.000Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "{\"cmd\": \"ls\"}", "call_id": "c1"}},
        {"timestamp": "2026-10-01T10:00:05.000Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "a.txt\nb.txt"}},
        {"timestamp": "2026-10-01T10:00:06.000Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "text(1)", "call_id": "c2"}},
        {"timestamp": "2026-10-01T10:00:07.000Z", "type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "c2", "output": [{"type": "input_text", "text": "one"}, {"type": "input_text", "text": "two"}]}},
        {"timestamp": "2026-10-01T10:00:08.000Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": "2026-10-01T10:00:09.000Z", "type": "event_msg", "payload": {"type": "token_count"}},
    ])
    entries = Tr.flatten_codex(path)
    assert [(e["role"], e["kind"], e["text"]) for e in entries] == [
        ("user", "text", "plain string"),
        ("assistant", "tool_call", 'tool shell({"cmd": "ls"})'),
        ("tool", "tool_result", "a.txt\nb.txt"),
        ("assistant", "tool_call", "tool exec(text(1))"),
        ("tool", "tool_result", "one\ntwo"),
        ("assistant", "text", "done"),
    ]
    assert entries[0]["at"] == "2026-10-01T10:00:02.000Z"
    assert not any("secret plan" in e["text"] or "harness" in e["text"] for e in entries)


def test_claude_blocks_thinking_dropped_summary_kept_and_tool_result_cap(tmp_path):
    big = "x" * 9000
    path = write_jsonl(str(tmp_path / "s.jsonl"), [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-01T10:00:00Z", "content": "hello"},
        {"type": "user", "timestamp": "2026-10-01T10:00:01Z", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:02Z", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "private", "signature": "sig"}, {"type": "text", "text": "hi there"}]}},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:03Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cat big.log"}}]}},
        {"type": "user", "timestamp": "2026-10-01T10:00:04Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": big}]}},
        {"type": "user", "timestamp": "2026-10-01T10:00:05Z", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "line a"}, {"type": "text", "text": "line b"}]}]}},
        {"type": "summary", "summary": "earlier: the founder chose vendor X", "leafUuid": "u"},
        {"type": "assistant", "timestamp": "2026-10-01T10:00:06Z", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "first"}, {"type": "text", "text": "second"}]}},
    ])
    entries = Tr.flatten_claude(path)
    kinds = [(e["role"], e["kind"]) for e in entries]
    assert kinds == [("user", "text"), ("assistant", "text"), ("assistant", "tool_call"), ("tool", "tool_result"),
                     ("tool", "tool_result"), ("summary", "text"), ("assistant", "text")]
    assert entries[1]["text"] == "hi there" and "private" not in json.dumps(entries)
    assert entries[2]["text"] == 'tool Bash({"command": "cat big.log"})'
    assert entries[3]["text"] == big[:4000] + "\n[... 5000 more chars omitted]"
    assert entries[4]["text"] == "line a\nline b"
    assert entries[5]["text"] == "earlier: the founder chose vendor X" and entries[5]["at"] == ""
    assert entries[6]["text"] == "first\nsecond", "text blocks of one message stay one entry"
    assert Tr.flatten_claude(path, tool_result_max_chars=9000)[3]["text"] == big


def test_since_keeps_entries_at_or_after_the_moment_and_undated_ones(tmp_path):
    path = write_jsonl(str(tmp_path / "s.jsonl"), [
        {"type": "user", "timestamp": "2026-10-01T10:00:00.500Z", "message": {"role": "user", "content": "before"}},
        {"type": "user", "timestamp": "2026-10-01T10:00:01Z", "message": {"role": "user", "content": "at"}},
        {"type": "summary", "summary": "undated summary"},
        {"type": "user", "timestamp": "2026-10-01T10:00:01.250Z", "message": {"role": "user", "content": "after"}},
    ])
    assert [e["text"] for e in Tr.flatten_claude(path, "2026-10-01T10:00:01Z")] == ["at", "undated summary", "after"]
    assert [e["text"] for e in Tr.flatten_claude(path, since_ts=None)] == ["before", "at", "undated summary", "after"]
    rollout = write_jsonl(str(tmp_path / "rollout-y.jsonl"), [
        {"timestamp": "2026-10-01T09:59:59Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "old"}},
        {"timestamp": "2026-10-01T10:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": "new"}},
    ])
    assert [e["text"] for e in Tr.flatten_codex(rollout, "2026-10-01T10:00:01Z")] == ["new"]
    assert Tr.parse_ts("2026-10-01T10:00:01Z") == Tr.parse_ts("2026-10-01T10:00:01.000+00:00")
    assert Tr.parse_ts("nonsense") is None and Tr.parse_ts("") is None


def test_render_format():
    entries = [{"at": "2026-10-01T12:03:09.100Z", "role": "user", "kind": "text", "text": "hi\nthere"},
               {"at": "2026-10-01T12:04:00Z", "role": "assistant", "kind": "tool_call", "text": 'tool Bash({"command": "ls"})'},
               {"at": "", "role": "summary", "kind": "text", "text": "compacted"}]
    assert Tr.render(entries) == '12:03Z user: hi\nthere\n\n12:04Z assistant: tool Bash({"command": "ls"})\n\n--:--Z summary: compacted'
    assert Tr.render([]) == ""


# ----------------------------------------------------------------------------------------------- window

def entries_n(n, size=300):
    return [{"at": f"2026-10-01T10:{m:02d}:00Z", "role": "user", "kind": "text", "text": f"note {m} " + "y" * size} for m in range(n)]


def test_window_keeps_newest_entries_within_budget_and_cuts_at_a_boundary():
    text = Tr.render(entries_n(30))
    out, meta = Tr.window(text, 1000)  # 3500 chars: about ten entries of ~320 chars
    assert out.startswith("Transcript window: ")
    body = out.split("\n\n", 1)[1]
    assert body.startswith("10:") and "note 29 " in out and "note 0 " not in out and "note 10 " not in out
    assert meta["entries_total"] == 30 and 5 < meta["entries_kept"] < 12 and meta["cut"] is True
    assert meta["entries_kept"] == len(body.split("\n\n")), "the cut is at an entry boundary: every kept entry is whole"
    assert meta["first_at"] == f"10:{30 - meta['entries_kept']:02d}Z" and meta["last_at"] == "10:29Z"
    assert meta["est_tokens"] <= 1000 and meta["est_tokens"] > 0
    line = out.splitlines()[0]
    assert line == (f"Transcript window: {meta['first_at']} to {meta['last_at']}, {meta['entries_kept']} of 30 entries; "
                    f"{30 - meta['entries_kept']} earlier entries not included.")
    assert out.count("\n\n") == meta["entries_kept"], "one blank line after the window line and between entries"


def test_window_without_a_cut_and_empty():
    text = Tr.render(entries_n(3, size=10))
    out, meta = Tr.window(text, 1000)
    assert meta == {"entries_total": 3, "entries_kept": 3, "first_at": "10:00Z", "last_at": "10:02Z",
                    "est_tokens": meta["est_tokens"], "cut": False}
    assert out.endswith(text) and out.splitlines()[0].endswith("3 of 3 entries; 0 earlier entries not included.")
    out, meta = Tr.window("", 100)
    assert meta["entries_total"] == 0 and meta["entries_kept"] == 0 and meta["cut"] is False
    assert out == "Transcript window: none to none, 0 of 0 entries; 0 earlier entries not included."


def test_window_entries_with_blank_lines_inside_stay_whole():
    es = [{"at": "2026-10-01T10:00:00Z", "role": "tool", "kind": "tool_result", "text": "para one\n\npara two\n\nuser: not a header\n"},
          {"at": "2026-10-01T10:01:00Z", "role": "assistant", "kind": "text", "text": "ok"}]
    out, meta = Tr.window(Tr.render(es), 100000)
    assert meta["entries_total"] == 2 and meta["entries_kept"] == 2 and "para one\n\npara two\n\nuser: not a header" in out
    assert Tr.split_entries(Tr.render(es)) == ["10:00Z tool: para one\n\npara two\n\nuser: not a header\n", "10:01Z assistant: ok"]
    assert "\n\n".join(Tr.split_entries(Tr.render(es))) == Tr.render(es), "split and join round-trip the rendered text"


def test_window_single_entry_over_budget_keeps_its_tail():
    es = [{"at": "2026-10-01T10:00:00Z", "role": "user", "kind": "text", "text": "old " * 500 + "END"}]
    out, meta = Tr.window(Tr.render(es), 100)
    assert meta["entries_kept"] == 1 and meta["entries_total"] == 1 and meta["cut"] is True
    assert out.endswith("END") and "chars of this entry omitted" in out and meta["est_tokens"] <= 130


def test_estimate_is_chars_over_3_5():
    assert Tr.estimate_tokens("x" * 350) == 100 and Tr.estimate_tokens("") == 0 and Tr.estimate_tokens("abcd") == 2


# ----------------------------------------------------------------------------------------------- finders

def test_find_claude_transcript_by_cwd_then_session_id_then_newest(tmp_path):
    cfg = str(tmp_path / "cfg"); home = "/srv/hydra/manager"
    proj = os.path.join(cfg, "projects", home.replace("/", "-"))
    other = os.path.join(cfg, "projects", "-elsewhere")
    assert Tr.find_claude_transcript(cfg, home, "sid") is None
    a = write_jsonl(os.path.join(other, "sid.jsonl"), [])
    assert Tr.find_claude_transcript(cfg, home, "sid") == a, "the session id wins over the project directory"
    b = write_jsonl(os.path.join(proj, "sid.jsonl"), [])
    assert Tr.find_claude_transcript(cfg, home, "sid") == b
    c = write_jsonl(os.path.join(proj, "zzz.jsonl"), []); os.utime(c, (time.time() + 100, time.time() + 100))
    assert Tr.find_claude_transcript(cfg, home, "") == c, "no session id: the newest file of the cwd's project"
    assert Tr.find_claude_transcript(cfg, home, "missing") == c


def test_find_codex_rollout_prefers_cwd_match_then_newest_then_hint(tmp_path):
    ch = str(tmp_path / "codex"); home = str(tmp_path / "home")
    assert Tr.find_codex_rollout(ch, home) is None
    meta = lambda cwd: {"timestamp": "2026-10-01T10:00:00Z", "type": "session_meta", "payload": {"cwd": cwd, "id": "x"}}  # noqa: E731
    a = write_jsonl(os.path.join(ch, "sessions", "2026", "09", "30", "rollout-2026-09-30T10-00-00-a.jsonl"), [meta("/other")])
    b = write_jsonl(os.path.join(ch, "sessions", "2026", "10", "01", "rollout-2026-10-01T10-00-00-b.jsonl"), [meta(home)])
    c = write_jsonl(os.path.join(ch, "sessions", "2026", "10", "01", "rollout-2026-10-01T11-00-00-c.jsonl"), [meta("/other")])
    now = time.time()
    for i, p in enumerate((a, b, c)):
        os.utime(p, (now + i, now + i))
    assert Tr.find_codex_rollout(ch, home) == b, "the latest rollout whose session_meta cwd is the manager's"
    assert Tr.find_codex_rollout(ch, "/nowhere") == c, "no cwd match: the latest rollout"
    assert Tr.find_codex_rollout(ch, home, hint=a) == a, "the file codex exec reported wins"
    assert Tr.find_codex_rollout(ch, home, hint="/gone.jsonl") == b
    assert Tr.rollout_cwd(b) == os.path.abspath(home)
