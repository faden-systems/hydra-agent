"""loops/b11.md requirement 7 (a)-(e), (g), (i)-(j), (n), (q): the ordinary-overage due rule, the retired
over_ratio, quiet hours not gating, the held path, the failure back-off, the status line, bytes pending, the
engine-family guard and the 2026-10-08 stale-pending regression. Fake engines, a controlled clock, fake posters."""
import json
import os
from pathlib import Path

from b11_helpers import (Clock, Poster, S, calls, config, cstate, event, fakes, home, kinds, make, session_id,
                         status_line, turns)

THRESHOLD = 300000


def test_a_three_times_threshold_compacts_before_the_next_turn_without_config():
    h, d = home(), fakes(context=3 * THRESHOLD, context_after=90000)
    sup = make(h, d)
    event(h, "A"); assert sup.run_once() is True
    assert turns(h)[-1]["context_tokens"] == 3 * THRESHOLD and cstate(h)["pending"]["reason"] == "tokens"
    assert sup.compaction_due() == (True, "tokens")
    event(h, "B"); assert sup.run_once() is True
    assert kinds(d) == ["turn", "compaction", "turn"], "the [compaction] call precedes the queued event's call"
    assert session_id(h) != "sess-test"
    records = [t for t in turns(h) if t.get("kind") == "compaction" or t.get("events")]
    assert [t.get("kind") for t in records] == [None, "compaction", None], "the compaction record precedes the event's record"
    last = cstate(h)["last"]
    assert last["ok"] is True and last["after_tokens"] == 90000, "the following measured turn verifies ok true"
    assert sup.compaction_due() == (False, None)


def test_a_tick_without_events_compacts_and_verify_blocks_a_second_rollover():
    h, d = home(), fakes(context=3 * THRESHOLD, context_after=90000)
    sup = make(h, d)
    event(h, "A"); sup.run_once()
    assert sup.run_once() is True, "a tick with no queued event compacts"
    assert kinds(d) == ["turn", "compaction"]
    assert cstate(h).get("verify"), "the rollover awaits its verifying turn"
    assert sup.compaction_due() == (False, None)
    assert sup.run_once() is False and kinds(d) == ["turn", "compaction"], "no second rollover while verify is outstanding"
    (d / "context_after").write_text(str(3 * THRESHOLD))  # the verifying turn still measures over the limit
    event(h, "B"); sup.run_once()
    assert kinds(d) == ["turn", "compaction", "turn"]
    assert cstate(h)["last"]["ok"] is False, "a verifying turn over the threshold is a failed compaction"


def test_b_exactly_the_threshold_does_not_compact_one_over_does_and_over_ratio_is_retired():
    h, d = home(), fakes(context=THRESHOLD, context_after=90000)
    sup = make(h, d)
    event(h, "A"); sup.run_once()
    assert sup.compaction_due() == (False, None) and cstate(h).get("pending") is None
    (d / "context").write_text(str(THRESHOLD + 1))
    event(h, "B"); sup.run_once()
    assert sup.compaction_due() == (True, "tokens")
    h2, d2 = home(), fakes(context=int(1.5 * THRESHOLD), context_after=90000)
    config(h2, over_ratio=2.0)
    logged, orig = [], S.log
    S.log = lambda m: logged.append(str(m))
    try:
        sup2 = make(h2, d2)
        event(h2, "A"); sup2.run_once()
        assert sup2.compaction_due() == (True, "tokens"), "1.5x still compacts: a configured over_ratio raises nothing"
        sup2.compaction_config(); S.compaction_config_for(h2); sup2.run_once()
    finally:
        S.log = orig
    retired = [m for m in logged if "over_ratio is retired" in m]
    assert len(retired) == 1, retired
    assert "over_ratio" not in sup2.compaction_config() and "over_ratio" not in S.COMPACTION_DEFAULTS


def test_c_quiet_hours_do_not_gate_the_ordinary_overage():
    h, d = home(), fakes(context=3 * THRESHOLD, context_after=90000)
    config(h, quiet_hours=[2, 5], quiet_hours_tz="UTC")
    clock = Clock(); clock.set(2026, 10, 8, 3, 0, 0)
    sup = make(h, d, clock=clock)
    event(h, "A"); sup.run_once()
    event(h, "B"); sup.run_once()
    assert kinds(d) == ["turn", "compaction", "turn"]


def test_d_held_path_posts_one_line_per_episode_and_keeps_pending():
    h, d = home(), fakes(context=940000, context_after=90000)
    config(h, rollover_enabled=False)
    blog = Poster()
    sup = make(h, d, blog=blog)
    event(h, "A"); sup.run_once()
    event(h, "B"); sup.run_once()
    sup.run_once(); sup.run_once()
    assert kinds(d) == ["turn", "turn"], "no engine call for a held compaction"
    st = cstate(h)
    assert st["last"]["error"].startswith("held: automatic rollover is disabled") and st["last"]["ok"] is None
    assert st["pending"], "pending kept"
    held = [t for t in blog.texts if "compaction held" in t]
    assert held == [f"hydra-manager: compaction held (rollover disabled): context 940000 over {THRESHOLD}; enable compaction.rollover_enabled"]
    Path(h, "COMPACT").write_text("")
    sup.run_once()
    assert not Path(h, "COMPACT").exists() and kinds(d) == ["turn", "turn"], "a forced request is consumed without an engine call"
    assert len([t for t in blog.texts if "compaction held" in t]) == 1
    (d / "context").write_text("100000")
    event(h, "C"); sup.run_once()
    assert not cstate(h).get("pending"), "a clearing turn ends the episode"
    (d / "context").write_text("940000")
    event(h, "D"); sup.run_once(); event(h, "E"); sup.run_once()
    assert len([t for t in blog.texts if "compaction held" in t]) == 2, "a new overage is a new episode"


def test_d_bytes_overage_with_rollover_disabled_posts_one_held_line_across_turns():
    h, d = home(), fakes(context=100000, context_after=90000)
    config(h, rollover_enabled=False, max_bytes=10)
    blog = Poster()
    sup = make(h, d, blog=blog)
    tdir = Path(h, ".claude", "projects", str(h).replace("/", "-")); tdir.mkdir(parents=True)
    Path(tdir, "sess-test.jsonl").write_bytes(b"x" * 64 + b"\n")
    for name in ("A", "B", "C"):
        event(h, name); sup.run_once()
    assert cstate(h)["pending"]["reason"] == "bytes", cstate(h)
    assert len([t for t in blog.texts if "compaction held" in t]) == 1, blog.texts
    assert kinds(d) == ["turn", "turn", "turn"]


def test_e_failed_rollover_backs_off_and_retries_after_retry_after_s():
    h, d = home(), fakes(context=940000, context_after=90000, fail_compact=True)
    config(h, retry_after_s=3600)
    clock = Clock(); blog = Poster()
    sup = make(h, d, clock=clock, blog=blog)
    event(h, "A"); sup.run_once()
    event(h, "B"); sup.run_once()
    assert kinds(d) == ["turn", "compaction", "turn"]
    st = cstate(h)
    assert st.get("failed_at") and st["last"]["ok"] is False and session_id(h) == "sess-test"
    assert len([t for t in blog.texts if "compaction" in t.lower()]) == 1, "the failure line is posted once"
    for name in ("C", "D", "E"):
        event(h, name); sup.run_once()
    assert kinds(d).count("compaction") == 1, "no second attempt within the back-off"
    clock.advance(3700)
    event(h, "F"); sup.run_once()
    assert kinds(d).count("compaction") == 2, "attempted again after the back-off"


def test_g_status_line_pins():
    now = Clock().now
    h = home()
    line = status_line(h, now)
    assert line == f"context: unknown | due over {THRESHOLD} | last compaction: none"
    S.append_jsonl(os.path.join(h, "logs", "turns.jsonl"), {"n": 1, "at": 1.0, "engine": "claude-r2d2", "events": ["x"], "context_tokens": 100000})
    assert status_line(h, now) == f"context: 100000 tokens (turn 1) | due over {THRESHOLD} | last compaction: none"
    S.append_jsonl(os.path.join(h, "logs", "turns.jsonl"), {"n": 2, "at": 1.0, "engine": "claude-r2d2", "events": ["x"], "context_tokens": THRESHOLD})
    assert "; compaction" not in status_line(h, now), "exactly the threshold: no suffix"
    S.append_jsonl(os.path.join(h, "logs", "turns.jsonl"), {"n": 3, "at": 1.0, "engine": "claude-r2d2", "events": ["x"], "context_tokens": 940000})
    assert status_line(h, now).endswith("; compaction: due (tokens)")
    Path(h, "COMPACT").write_text("")
    assert status_line(h, now).endswith("; compaction: forced")
    Path(h, "COMPACT").unlink()
    S.write_text(S.compaction_state_path(h), json.dumps({"pending": {"reason": "tokens", "value": 940000},
                 "last": {"before_tokens": 940000, "after_tokens": None, "at": "2026-10-08T09:58:22Z", "ok": None, "reason": "forced", "error": "held: automatic rollover is disabled"}}))
    line = status_line(h, now)
    assert "last compaction: 2026-10-08T09:58:22Z (940000 -> held)" in line and line.endswith("; compaction: held")
    S.write_text(S.compaction_state_path(h), json.dumps({"verify": {"before_tokens": 940000, "at": "2026-10-08T11:00:00Z", "reason": "tokens", "old_id": "a", "new_id": "b"}}))
    assert status_line(h, now).endswith("; compaction: awaiting verification")
    S.write_text(S.compaction_state_path(h), json.dumps({"last": {"before_tokens": 940000, "after_tokens": 90000, "at": "2026-10-08T11:00:00Z", "ok": True, "reason": "tokens"},
                                                         "cli_compactions": [{"at": "2026-10-08T12:00:00Z", "turn": 4}]}))
    S.append_jsonl(os.path.join(h, "logs", "turns.jsonl"), {"n": 4, "at": 1.0, "engine": "claude-r2d2", "events": ["x"], "context_tokens": 90000})
    line = status_line(h, now)
    assert "last compaction: 2026-10-08T12:00:00Z (cli)" in line and "; compaction" not in line
    S.write_text(S.compaction_state_path(h), json.dumps({"pending": {"reason": "tokens", "value": 999999, "since": "x"}}))
    line = status_line(h, now)
    assert line.startswith("context: 90000 tokens (turn 4)") and "999999" not in line and "compaction: pending (" not in line


def test_j_bytes_pending_compacts_and_respects_the_back_off():
    h, d = home(), fakes(context=100000, context_after=90000, fail_compact=True)
    config(h, max_bytes=10, retry_after_s=3600)
    clock = Clock()
    sup = make(h, d, clock=clock)
    tdir = Path(h, ".claude", "projects", str(h).replace("/", "-")); tdir.mkdir(parents=True)
    Path(tdir, "sess-test.jsonl").write_bytes(b"x" * 64 + b"\n")
    event(h, "A"); sup.run_once()
    assert cstate(h)["pending"]["reason"] == "bytes" and sup.compaction_due() == (True, "bytes")
    event(h, "B"); sup.run_once()
    assert kinds(d) == ["turn", "compaction", "turn"] and cstate(h)["last"]["ok"] is False
    event(h, "C"); sup.run_once()
    assert kinds(d).count("compaction") == 1, "suppressed during the back-off"
    assert "; compaction: due" not in status_line(h, clock.now)
    (d / "fail_compact").unlink()
    clock.advance(3700)
    event(h, "D"); sup.run_once()
    assert kinds(d).count("compaction") == 2 and session_id(h) != "sess-test", "retried after the back-off"


def test_n_codex_selected_ignores_a_stale_claude_measurement():
    h, d = home(), fakes(context=940000, context_after=90000)
    sup = make(h, d)
    event(h, "A"); sup.run_once()
    Path(h, "engine").write_text("codex\n")
    assert sup.compaction_due() == (False, None)
    assert "; compaction: due" not in status_line(h)
    event(h, "B"); sup.run_once()
    assert kinds(d) == ["turn", "codex"]
    Path(h, "engine").write_text("claude-r2d2\n")
    assert sup.compaction_due() == (True, "tokens")


def test_q_regression_2026_10_08_stale_pending_cli_boundary_lower_measurement():
    h, d = home(), fakes(context=94586)
    S.write_text(S.compaction_state_path(h), json.dumps({"pending": {"reason": "tokens", "value": 935607, "limit": THRESHOLD, "since": "2026-10-08T07:09:40Z"},
                                                         "cli_compactions": [{"at": "2026-10-08T09:40:58Z", "turn": 1060}]}))
    sup = make(h, d)
    event(h, "A"); sup.run_once()
    line = status_line(h)
    assert line.startswith("context: 94586 tokens") and "(cli)" in line and "; compaction" not in line and "935607" not in line
    assert sup.compaction_due() == (False, None)
    assert not cstate(h).get("pending"), "the measured turn below the threshold cleared the stale pending"
