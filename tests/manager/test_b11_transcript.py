"""loops/b11.md requirement 7 (f) and (k): the CLI's own compactions (`compact_boundary`) parsed by the pure
`cli_compactions_since`, the incremental transcript scan, the turn flag, the status precedence, the cap, the
unreadable-transcript diagnostic once per episode, a missing transcript never failing a turn."""
import json
from pathlib import Path

from b11_helpers import S, cstate, event, fakes, home, kinds, make, session_id, status_line, transcript_dir, turns

GOOD = {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-08T09:40:58.542Z", "content": "Conversation compacted"}
OTHER = {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}


def test_f_cli_compactions_since_parses_boundaries_skips_invalid_and_leaves_a_partial_line():
    lines = [json.dumps(OTHER), json.dumps(GOOD), "{not json", json.dumps(dict(GOOD, timestamp="2026-10-08T10:00:00Z")), json.dumps(OTHER)]
    data = ("\n".join(lines) + "\n").encode() + b'{"type": "system", "subtype": "compact_bou'
    recs, offset = S.cli_compactions_since(data, 0)
    assert [r["at"] for r in recs] == ["2026-10-08T09:40:58.542Z", "2026-10-08T10:00:00Z"]
    assert offset == len(("\n".join(lines) + "\n").encode()), "the offset stops before the partial trailing line"
    recs2, offset2 = S.cli_compactions_since(data, offset)
    assert recs2 == [] and offset2 == offset
    recs3, _ = S.cli_compactions_since(data + b'ndary", "timestamp": "T3"}\n', offset)
    assert [r["at"] for r in recs3] == ["T3"], "the completed line is found once its newline arrives"


def test_f_turn_after_a_boundary_is_flagged_and_status_shows_cli_until_a_supervisor_compaction():
    h, d = home(), fakes(context=100000, context_after=90000)
    sup = make(h, d)
    tpath = transcript_dir(h) / "sess-test.jsonl"
    tpath.write_text(json.dumps(OTHER) + "\n")
    event(h, "A"); sup.run_once()
    assert turns(h)[-1]["cli_compacted"] is False and not cstate(h).get("cli_compactions")
    assert cstate(h)["transcript_scan"] == {"path": str(tpath), "offset": tpath.stat().st_size}
    with tpath.open("a") as f:
        f.write(json.dumps(GOOD) + "\n")
    event(h, "B"); sup.run_once()
    assert turns(h)[-1]["cli_compacted"] is True
    assert cstate(h)["cli_compactions"] == [{"at": "2026-10-08T09:40:58.542Z", "turn": 2}]
    assert "last compaction: 2026-10-08T09:40:58.542Z (cli)" in status_line(h)
    (d / "context").write_text("940000")
    event(h, "C"); sup.run_once()
    event(h, "D"); sup.run_once()
    assert "compaction" in kinds(d) and session_id(h) != "sess-test"
    assert "(cli)" not in status_line(h) and "->" in status_line(h), "a later supervisor compaction takes precedence"


def test_k_transcript_edge_cases():
    h, d = home(), fakes(context=100000, context_after=90000)
    sup = make(h, d)
    tdir = transcript_dir(h)
    tpath = tdir / "sess-test.jsonl"
    tpath.write_text(json.dumps(GOOD) + "\n")
    event(h, "A"); sup.run_once()
    assert cstate(h)["transcript_scan"]["offset"] == tpath.stat().st_size
    partial = json.dumps(dict(GOOD, timestamp="2026-10-08T10:40:58.542Z"))
    with tpath.open("a") as f:
        f.write(partial[:20])
    event(h, "B"); sup.run_once()
    assert len(cstate(h)["cli_compactions"]) == 1 and cstate(h)["transcript_scan"]["offset"] == tpath.stat().st_size - 20
    with tpath.open("a") as f:
        f.write(partial[20:] + "\n")
    event(h, "C"); sup.run_once()
    assert [c["at"] for c in cstate(h)["cli_compactions"]][-1] == "2026-10-08T10:40:58.542Z"
    fresh = make(h, d)
    event(h, "D"); fresh.run_once()
    assert len(cstate(h)["cli_compactions"]) == 2, "a fresh supervisor finds nothing new from the recorded offset"
    tpath.write_text(json.dumps(GOOD) + "\n")  # truncated: rescanned without a duplicate
    event(h, "E"); sup.run_once()
    assert len(cstate(h)["cli_compactions"]) == 2 and cstate(h)["transcript_scan"]["offset"] == tpath.stat().st_size
    # a boundary appended to the OLD transcript before the rollover tick: recorded by the [compaction] turn's scan,
    # older than the rollover so not the status last; a newer one in the NEW transcript becomes the last
    (d / "context").write_text("940000")
    event(h, "F"); sup.run_once()
    with tpath.open("a") as f:
        f.write(json.dumps(dict(GOOD, timestamp="2026-10-08T11:30:00.000Z")) + "\n")
    event(h, "G"); sup.run_once()
    assert session_id(h) != "sess-test" and cstate(h)["cli_compactions"][-1]["at"] == "2026-10-08T11:30:00.000Z"
    assert "(cli)" not in status_line(h)
    newpath = tdir / f"{session_id(h)}.jsonl"
    newpath.write_text(json.dumps(dict(GOOD, timestamp="2026-10-08T13:00:00.000Z")) + "\n")
    event(h, "H"); sup.run_once()
    assert cstate(h)["transcript_scan"]["path"] == str(newpath) and "(cli)" in status_line(h)
    with newpath.open("a") as f:
        for i in range(51):
            f.write(json.dumps(dict(GOOD, timestamp=f"2026-10-09T00:{i // 60:02d}:{i % 60:02d}.000Z")) + "\n")
    event(h, "I"); sup.run_once()
    assert len(cstate(h)["cli_compactions"]) == 50 and cstate(h)["cli_compactions"][-1]["at"] == "2026-10-09T00:00:50.000Z"
    # a directory at the transcript path: the turn succeeds, one diagnostic per episode
    newpath.unlink(); newpath.mkdir()
    logged, orig = [], S.log
    S.log = lambda m: logged.append(str(m))
    try:
        for name in ("J", "K", "L"):
            event(h, name); assert sup.run_once() is True
    finally:
        S.log = orig
    assert len([m for m in logged if "transcript" in m.lower()]) == 1, logged


def test_k_missing_transcript_never_fails_the_turn():
    h, d = home(), fakes(context=100000)
    sup = make(h, d)
    event(h, "A"); assert sup.run_once() is True
    assert turns(h)[-1].get("cli_compacted") is False
