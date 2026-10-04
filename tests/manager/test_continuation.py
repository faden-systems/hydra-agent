"""requirement 16 (work-status.json / settle_work) and requirement 17 (capped automatic continuation),
loops/b7.md."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from conftest import S


def write_status(home, **fields):
    base = {"turn": 1, "mode": "continue", "track": "t", "channel": "C", "thread_ts": "th", "message_ts": "m",
            "next_action": "go"}
    base.update(fields)
    S.write_text(os.path.join(home, "work-status.json"), json.dumps(base))
    return base


def make(home, now=1000.0, max_per_hour=6):
    return S.Supervisor(home=home, config={"continuation": {"max_per_hour": max_per_hour}},
                        clock=lambda: now, poster=lambda *a: None)


def test_missing_work_status_is_a_noop(home):
    sup = make(home)
    sup.settle_work(1, [])
    assert not S.pending_events(home)


def test_malformed_json_is_a_noop(home):
    S.write_text(os.path.join(home, "work-status.json"), "{not json")
    make(home).settle_work(1, [])
    assert not S.pending_events(home)


def test_stale_turn_disables_continuation(home):
    write_status(home, turn=5)
    make(home).settle_work(6, [])
    assert not S.pending_events(home)


def test_unknown_mode_disables_continuation(home):
    write_status(home, mode="bogus")
    make(home).settle_work(1, [])
    assert not S.pending_events(home)


def test_missing_required_base_fields_disables(home):
    S.write_text(os.path.join(home, "work-status.json"), json.dumps({"turn": 1, "mode": "continue"}))
    make(home).settle_work(1, [])
    assert not S.pending_events(home)


def test_continue_needs_nonempty_next_action(home):
    write_status(home, next_action="")
    make(home).settle_work(1, [])
    assert not S.pending_events(home)


def test_turn_must_be_int_not_bool(home):
    S.write_text(os.path.join(home, "work-status.json"),
                 json.dumps({"turn": True, "mode": "continue", "track": "t", "channel": "C",
                            "thread_ts": "th", "message_ts": "m", "next_action": "go"}))
    make(home).settle_work(1, [])
    assert not S.pending_events(home)


def test_continue_reserves_and_enqueues_deterministic_event(home):
    write_status(home, turn=1)
    make(home).settle_work(1, [])
    q = S.pending_events(home)
    assert len(q) == 1 and q[0]["id"] == "continue-1"
    assert q[0]["payload"]["instructs"] is False
    assert "t" in q[0]["payload"]["text"] and "go" in q[0]["payload"]["text"]


def test_same_turn_settle_work_is_idempotent(home):
    write_status(home, turn=1)
    sup = make(home)
    sup.settle_work(1, [])
    sup.settle_work(1, [])
    assert len(S.pending_events(home)) == 1


def test_cap_blocks_further_reservations_within_the_rolling_hour(home):
    write_status(home, turn=1)
    sup = make(home, now=1000.0, max_per_hour=1)
    sup.settle_work(1, [])
    assert len(S.pending_events(home)) == 1
    sup.deliver({"slack": [], "cli": []}, ["continue-1"], 1)
    write_status(home, turn=2)
    sup2 = S.Supervisor(home=home, config={"continuation": {"max_per_hour": 1}}, clock=lambda: 1001.0,
                        poster=lambda *a: None)
    sup2.settle_work(2, [])
    assert not S.pending_events(home), "the cap must persist across a fresh Supervisor instance"


def test_cap_releases_once_the_window_expires(home):
    write_status(home, turn=1)
    S.Supervisor(home=home, config={"continuation": {"max_per_hour": 1}}, clock=lambda: 1000.0,
                poster=lambda *a: None).settle_work(1, [])
    S.Supervisor(home=home, poster=lambda *a: None).deliver({"slack": [], "cli": []}, ["continue-1"], 1)
    write_status(home, turn=2)
    later = S.Supervisor(home=home, config={"continuation": {"max_per_hour": 1}}, clock=lambda: 1000.0 + 3601,
                         poster=lambda *a: None)
    later.settle_work(2, [])
    assert [e["id"] for e in S.pending_events(home)] == ["continue-2"]


def test_backward_clock_never_clears_the_cap(home):
    write_status(home, turn=1)
    S.Supervisor(home=home, config={"continuation": {"max_per_hour": 1}}, clock=lambda: 1000.0,
                poster=lambda *a: None).settle_work(1, [])
    S.Supervisor(home=home, poster=lambda *a: None).deliver({"slack": [], "cli": []}, ["continue-1"], 1)
    write_status(home, turn=2)
    earlier = S.Supervisor(home=home, config={"continuation": {"max_per_hour": 1}}, clock=lambda: 900.0,
                           poster=lambda *a: None)
    earlier.settle_work(2, [])
    assert not S.pending_events(home)


def test_invalid_cap_values_disable_with_diagnostic(home):
    write_status(home, turn=1)
    for bad in (-1, True, "6", 61):
        h = home
        sup = S.Supervisor(home=h, config={"continuation": {"max_per_hour": bad}}, poster=lambda *a: None)
        sup.settle_work(1, [])
        assert not S.pending_events(h), repr(bad)


def test_zero_cap_disables(home):
    write_status(home, turn=1)
    S.Supervisor(home=home, config={"continuation": {"max_per_hour": 0}}, poster=lambda *a: None).settle_work(1, [])
    assert not S.pending_events(home)


def test_pending_external_event_suppresses_automatic_continuation(home):
    write_status(home, turn=1)
    S.append_event(home, S.new_event("slack", {"channel": "C", "ts": "f", "text": "hi", "instructs": True},
                                     event_id="founder"))
    make(home).settle_work(1, [])
    assert [e["id"] for e in S.pending_events(home)] == ["founder"]


def test_pending_reply_suppresses_automatic_continuation(home):
    write_status(home, turn=1)
    S.write_text(os.path.join(home, "inbox", "pending-replies.jsonl"), json.dumps({"turn": 0}))
    make(home).settle_work(1, [])
    assert not S.pending_events(home)


def test_pause_suppresses_at_execution_time(home):
    write_status(home, turn=1)
    sup = make(home)
    sup.settle_work(1, [])
    assert S.pending_events(home)
    import pathlib
    pathlib.Path(home, "PAUSE").touch()
    with patch.object(sup, "turn") as turn, patch.object(sup, "compaction_due", return_value=(False, "")):
        sup.run_once()
    assert not turn.called


def test_continuation_deferred_behind_new_founder_event_at_execution(home):
    write_status(home, turn=1)
    sup = make(home)
    sup.settle_work(1, [])
    S.append_event(home, S.new_event("slack", {"channel": "C", "ts": "f", "text": "hi", "instructs": True},
                                     event_id="founder"))
    batches = []
    with patch.object(sup, "turn", side_effect=lambda ev: (batches.append(ev) or True)), \
         patch.object(sup, "compaction_due", return_value=(False, "")):
        sup.run_once()
    assert batches and all(e["id"] != "continue-1" for e in batches[0])
    assert any(e["id"] == "founder" for e in batches[0])


def test_restart_reconciles_an_incomplete_reservation_once(home):
    write_status(home, turn=1)
    sup = S.Supervisor(home=home, config={"continuation": {"max_per_hour": 6}}, clock=lambda: 1000.0,
                       poster=lambda *a: None)
    hits = []

    def crash(name):
        if name == "continuation_reserved":
            hits.append(name)
            raise SystemExit("fixture")
    with patch.object(sup, "persistence_checkpoint", side_effect=crash):
        try:
            sup.settle_work(1, [])
        except SystemExit:
            pass
    assert hits == ["continuation_reserved"]
    assert not S.pending_events(home)
    restarted = S.Supervisor(home=home, clock=lambda: 1000.0, poster=lambda *a: None)
    with patch.object(restarted, "turn", return_value=False), patch.object(restarted, "compaction_due",
                                                                           return_value=(False, "")):
        restarted.run_once()
    assert [e["id"] for e in S.pending_events(home)] == ["continue-1"]
    # a second reconciliation (another idle tick) must not duplicate it
    with patch.object(restarted, "turn", return_value=False), patch.object(restarted, "compaction_due",
                                                                           return_value=(False, "")):
        restarted.run_once()
    assert [e["id"] for e in S.pending_events(home)] == ["continue-1"]


# ----------------------------------------------------------------------------------------------- requirement 18: waiting footer and reaction

def test_la_time_label_shows_pdt_in_october():
    assert S.la_time_label(1791070200).endswith("PDT")


def test_idle_footer_text(home):
    write_status(home, mode="idle", deadline=1791070200.0)
    sup = make(home)
    ev = {"id": "m", "source": "slack", "payload": {"channel": "C", "ts": "m", "thread_ts": "th",
                                                     "text": "x", "instructs": False, "addressed": False}}
    footer = sup._prepare_waiting_footer(1, [ev])
    assert footer == "⏲ next check 16:30 PDT"


def test_waiting_footer_names_who(home):
    write_status(home, mode="waiting", deadline=1791070200.0, who="Hermes", since=1791069600.0)
    sup = make(home)
    ev = {"id": "m", "source": "slack", "payload": {"channel": "C", "ts": "m", "thread_ts": "th",
                                                     "text": "x", "instructs": False, "addressed": False}}
    footer = sup._prepare_waiting_footer(1, [ev])
    assert footer == "⏲ waiting on Hermes; next check 16:30 PDT"


def test_continue_and_done_never_get_a_footer(home):
    ev = {"id": "m", "source": "slack", "payload": {"channel": "C", "ts": "m", "thread_ts": "th",
                                                     "text": "x", "instructs": False, "addressed": False}}
    for mode, extra in (("continue", {"next_action": "go"}), ("done", {})):
        write_status(home, mode=mode, **extra)
        assert make(home)._prepare_waiting_footer(1, [ev]) is None


def test_reaction_target_must_match_an_actual_event_not_a_caller_claim(home):
    write_status(home, mode="waiting", deadline=1791070200.0, who="H", since=1791069000.0,
                channel="OTHER_CHANNEL")
    added = []
    reactor = SimpleNamespace(add=lambda *a: added.append(a), remove=lambda *a: None)
    sup = S.Supervisor(home=home, poster=lambda *a: None, reactor=reactor)
    ev = {"id": "m", "source": "slack", "payload": {"channel": "C", "ts": "m", "thread_ts": "th",
                                                     "text": "x", "instructs": False, "addressed": False}}
    sup.settle_work(1, [ev])
    assert not added


def test_reaction_add_retried_on_failure_without_false_confirmation(home):
    write_status(home, mode="waiting", deadline=1791070200.0, who="H", since=1791069000.0)
    calls = {"fail": True}
    added = []

    def add(channel, ts, name):
        added.append((channel, ts, name))
        if calls["fail"]:
            raise RuntimeError("offline")
    reactor = SimpleNamespace(add=add, remove=lambda *a: None)
    ev = {"id": "m", "source": "slack", "payload": {"channel": "C", "ts": "m", "thread_ts": "th",
                                                     "text": "x", "instructs": False, "addressed": False}}
    sup = S.Supervisor(home=home, poster=lambda *a: None, reactor=reactor)
    sup.settle_work(1, [ev])
    assert len(added) == 1
    calls["fail"] = False
    sup.reconcile_work_reaction()
    assert len(added) == 2
    sup.reconcile_work_reaction()
    assert len(added) == 2, "a confirmed reaction must not be retried again"


def test_reaction_removed_when_active_work_resumes(home):
    write_status(home, mode="waiting", deadline=1791070200.0, who="H", since=1791069000.0)
    added, removed = [], []
    reactor = SimpleNamespace(add=lambda *a: added.append(a), remove=lambda *a: removed.append(a))
    ev = {"id": "m", "source": "slack", "payload": {"channel": "C", "ts": "m", "thread_ts": "th",
                                                     "text": "x", "instructs": False, "addressed": False}}
    sup = S.Supervisor(home=home, poster=lambda *a: None, reactor=reactor)
    sup.settle_work(1, [ev])
    assert added == [("C", "m", "timer_clock")]
    write_status(home, mode="continue", turn=2, next_action="go")
    sup.settle_work(2, [])
    assert removed == [("C", "m", "timer_clock")]


def test_status_shows_waiting_on_since(home):
    write_status(home, mode="waiting", deadline=1791070200.0, who="Hermes", since=1791069600.0)
    assert "waiting on Hermes since" in S.status_text(home)


def test_status_shows_idle_until(home):
    write_status(home, mode="idle", deadline=1791070200.0)
    assert "idle until" in S.status_text(home)


def test_legacy_state_without_work_status_has_no_waiting_claim(home):
    text = S.status_text(home)
    assert "waiting on" not in text and "idle until" not in text
