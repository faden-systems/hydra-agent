"""loops/b11.md requirement 7 (h), (m), (o), (p): the per-turn state digest replacing the whole-file prefix, its
selection rules and byte caps, the degradation order, the supported 400-track boundary, and `message_bytes` on
every turn record of every engine."""
import json
from pathlib import Path

from b11_helpers import S, calls, event, fakes, home, make, turns


def big_state(n_tracks=24, entry_kb=5):
    tracks = []
    for i in range(n_tracks):
        tid = f"t{i:02d}"
        tracks.append({"id": tid, "repo": "faden", "title": f"track {tid}", "now": f"now of {tid}: " + ("x" * 100),
                       "waits_on": f"waits {tid}", "next_action": f"next {tid}", "thread": f"17910000{i:02d}.000001",
                       "detail": {"marker": f"MARKER-{tid}-DEEP", "nested": {"thread": f"17919999{i:02d}.000001"},
                                  "blob": "y" * (entry_kb * 1024)}})
    return {"schema_version": 2, "updated": "2026-10-08T10:00:00+00:00",
            "founder_decisions_in_force": [f"2026-10-{d:02d}: decision number {d} " + ("z" * 200) for d in range(1, 31)],
            "tracks": tracks, "next_gate": "the next gate text", "waits_on": "the waits text"}


def digest_of(msg):
    start = msg.index("# factory/state.json (digest")
    end = msg.index("## end of state digest", start) + len("## end of state digest")
    return msg[start:end]


def test_h_digest_bounds_the_message_and_selects_only_the_touched_tracks():
    h, d = home(), fakes(context=100000)
    state = big_state()
    Path(h, "state.json").write_text(json.dumps(state, indent=1))
    before = Path(h, "state.json").stat().st_size
    assert before > 120000
    Path(h, "work-status.json").write_text(json.dumps({"turn": 1, "mode": "continue", "track": "t21", "next_action": "x"}))
    sup = make(h, d)
    event(h, "thread event", thread="1791000005.000001")
    event(h, "<@U0B5LK5DX43> assignee: Hermes | track: t09\nbody")
    event(h, "continue t13: next step", source="self", instructs=False)
    assert sup.run_once() is True
    msg = calls(d)[-1]["stdin"]
    digest = digest_of(msg)
    assert len(msg.encode()) < 40000 and len(digest.encode()) <= 36000
    assert msg.count("# factory/state.json") == 1
    for i in range(24):
        assert f"- t{i:02d}: now: now of t{i:02d}:" in digest
    for tid in ("t05", "t09", "t13", "t21"):
        assert f"MARKER-{tid}-DEEP" in digest
    assert sum(1 for i in range(24) if f"MARKER-t{i:02d}-DEEP" in digest) == 4, "no marker of any unselected track"
    sections = [l[4:] for l in digest.splitlines() if l.startswith("### ")]
    assert sections == ["t05", "t09", "t13", "t21"], "selection order: events in batch order, (a)(b)(c), then work-status last"
    rec = turns(h)[-1]
    assert rec["message_bytes"] == len(msg.encode("utf-8"))
    assert "## founder decisions (30; newest 10 below, the rest in the file)" in digest
    assert "## next_gate\nthe next gate text" in digest and "## waits_on\nthe waits text" in digest


def test_h_cap_drops_full_entries_in_reverse_selection_order_with_the_truncation_note():
    h, d = home(), fakes(context=100000)
    state = big_state(entry_kb=9)  # four full entries would overflow 36,000 bytes
    Path(h, "state.json").write_text(json.dumps(state, indent=1))
    sup = make(h, d)
    event(h, "thread event", thread="1791000012.000001")   # t12 first by event order, later in file order than t03
    event(h, "assignee: Hermes | track: t03")
    event(h, "continue t07: go", source="self", instructs=False)
    event(h, "continue t01: go", source="self", instructs=False)
    assert sup.run_once() is True
    digest = digest_of(calls(d)[-1]["stdin"])
    assert len(digest.encode()) <= 36000
    assert "MARKER-t12-DEEP" in digest, "the first-selected track by event order survives"
    assert "MARKER-t01-DEEP" not in digest, "the last-selected is dropped first"
    assert "(truncated: " in digest.splitlines()[0] and "touched track(s) left out)" in digest.splitlines()[0]


def test_m_message_bytes_on_every_record_including_codex_and_the_compaction_record():
    h, d = home(), fakes(context=940000, context_after=90000)
    Path(h, "state.json").write_text(json.dumps(big_state(4, 1)))
    sup = make(h, d)
    event(h, "non-ASCII text: éè日本")
    assert sup.run_once() is True
    event(h, "next"); assert sup.run_once() is True  # compaction then the turn
    Path(h, "engine").write_text("codex\n")
    event(h, "codex turn"); assert sup.run_once() is True
    recorded = calls(d)
    assert [c["kind"] for c in recorded] == ["turn", "compaction", "turn", "codex"]
    rows = [r for r in turns(h) if r.get("message_bytes") is not None]
    assert len(rows) == 4 and [r["message_bytes"] for r in rows] == [len(c["stdin"].encode("utf-8")) for c in recorded]
    assert [r.get("kind") for r in rows] == [None, "compaction", None, None]
    codex_stdin = recorded[-1]["stdin"]
    assert codex_stdin.count("# factory/state.json") == 1 and "## end of state digest" in codex_stdin, "invoke_codex carries the same digest, no second prefix"


def test_o_assignee_line_forms():
    h, d = home(), fakes(context=100000)
    Path(h, "state.json").write_text(json.dumps(big_state(3, 1)))
    sup = make(h, d)
    event(h, "assignee: Hermes | track: t01\nbody")
    event(h, "<@U0B5LK5DX43> <@U0AQR2PJN85> assignee: Simba | track: t02\nbody")
    event(h, "please assignee: Hermes | track: t00 extra words")
    assert sup.run_once() is True
    digest = digest_of(calls(d)[-1]["stdin"])
    assert "MARKER-t01-DEEP" in digest and "MARKER-t02-DEEP" in digest and "MARKER-t00-DEEP" not in digest


def test_p_degradation_order_and_the_supported_boundary():
    h, d = home(), fakes(context=100000)
    state = big_state(80, 1)
    for tr in state["tracks"]:
        tr["now"] = "n" * 230; tr["waits_on"] = "w" * 230; tr["next_action"] = "a" * 230
    state["founder_decisions_in_force"] = [f"2026-10-{d % 28 + 1:02d}: " + ("z" * 590) for d in range(60)]
    Path(h, "state.json").write_text(json.dumps(state))
    sup = make(h, d)
    event(h, "hello", thread="9.9")
    assert sup.run_once() is True
    msg = calls(d)[-1]["stdin"]
    digest = digest_of(msg)
    assert len(msg.encode()) < 40000 and "(mandatory part shortened)" in digest.splitlines()[0]
    for tr in state["tracks"]:
        assert f"- {tr['id']}" in digest
    assert "## founder decisions (60; newest 3 below, the rest in the file)" in digest
    lines = [l for l in digest.splitlines() if l.startswith("- t")]
    assert all(l == f"- {l[2:5]}: now: " + "n" * 120 for l in lines), lines[:2]
    assert "waits_on:" not in digest and "next_action:" not in digest
    # the supported boundary: 400 tracks with 56-byte four-byte-character ids, 30 long decisions, long fields
    ids = [chr(0x1F600 + i) * 14 for i in range(400)]  # 14 x 4 bytes = 56 bytes
    assert all(len(i.encode()) == 56 for i in ids)
    state = {"updated": "x", "founder_decisions_in_force": ["\U0001F4A1" * 600 for _ in range(30)],
             "tracks": [{"id": i, "now": "\U0001F4A1" * 300, "waits_on": "w" * 300, "next_action": "a" * 300, "thread": "1.1"} for i in ids],
             "next_gate": "g" * 1000, "waits_on": "w" * 1000}
    Path(h, "state.json").write_text(json.dumps(state, ensure_ascii=False))
    digest = S.build_state_digest(h, None, [])
    assert len(digest.encode()) <= 36000 and "(mandatory part over budget)" not in digest
    for i in ids:
        assert f"- {i}" in digest
