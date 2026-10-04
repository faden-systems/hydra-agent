"""New regressions for the founder-approved bounded repair (loops/b7.md 2026-10-04) and its audit gap
correction: partial-delivery recovery must durably retain confirmed destinations across repeated recovery
crashes (requirement 21, B2), and post_unavailable must route self notices to their own usable thread or
durable local logs, never a top-level Slack post (requirement 25). Also covers the other repair boundaries
(B1 continuation intent, B2 settlement checkpoints, S1 waiting-clock restart) with a fresh Supervisor per
simulated restart, offline fixtures only."""
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from conftest import S, calls, engines, queue_event


# --------------------------------------------------------------------------------------- audit gap: B2 partial delivery

def test_partial_delivery_recovery_survives_repeated_crash_without_reposting_confirmed(home, ok_engine):
    """Once "first" is confirmed, a crash during the recovery retry -- even one that never reaches `deliver`'s
    own except block -- must never fall back to the full original batch and repost it."""
    fail = {"second": True}
    posted = []

    def poster(channel, thread_ts, text):
        if thread_ts == "second" and fail["second"]:
            raise RuntimeError("offline second")
        posted.append(thread_ts)

    first = queue_event(home, "x", channel="C", thread="first", ts="first-id")
    second = queue_event(home, "y", channel="C", thread="second", ts="second-id")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    assert sup.run_once() is True
    assert posted == ["first"], "the fixture must confirm only the first destination"
    pending_path = os.path.join(home, "inbox", "pending-replies.jsonl")
    assert os.path.exists(pending_path), "the durable remainder must be retained"
    calls_before = len(calls(os.path.dirname(ok_engine)))

    def crash_deliver(self, deliveries, event_ids, n):
        raise RuntimeError("process crash mid-recovery")

    for _ in range(2):
        with patch.object(S.Supervisor, "deliver", crash_deliver):
            with pytest.raises(RuntimeError):
                S.Supervisor(home=home, engines=engines(ok_engine), poster=poster).run_once()
        # the crash happened before `deliver` ever ran its own bookkeeping: the remainder on disk is untouched
        remainder = S.read_jsonl(pending_path)
        assert len(remainder) == 1 and remainder[0]["slack"][0]["thread_ts"] == "second", \
            "a crashed recovery attempt must not lose or replace the durable remainder"

    fail["second"] = False
    for _ in range(3):
        S.Supervisor(home=home, engines=engines(ok_engine), poster=poster).run_once()
    assert posted == ["first", "second"], f"confirmed destination replayed or remainder lost: {posted}"
    assert len(calls(os.path.dirname(ok_engine))) == calls_before, "recovery must never rerun the engine"
    assert not os.path.exists(pending_path)
    assert not S.pending_events(home)
    assert first in S.handled_ids(home) and second in S.handled_ids(home)


def test_pending_replies_record_replaced_not_appended_on_repeated_failure(home, ok_engine):
    """The durable pending-replies record reflects the current remainder; a second failed retry must replace
    it, not pile a duplicate record on top (which would otherwise resurrect an already-confirmed destination
    the next time the file is read)."""
    def always_down(channel, thread_ts, text):
        raise RuntimeError("down")
    queue_event(home, "x", channel="C", thread="t1")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=always_down)
    assert sup.run_once() is True
    assert sup.run_once() is False
    assert sup.run_once() is False
    path = os.path.join(home, "inbox", "pending-replies.jsonl")
    lines = [l for l in open(path) if l.strip()]
    assert len(lines) == 1, "repeated failures must replace the durable remainder, never accumulate records"


# --------------------------------------------------------------------------------------- audit gap: self-event unavailable routing

def test_post_unavailable_self_with_thread_posts_once_and_dedupes_across_restart(home):
    posted = []
    ev = S.new_event("self", {"text": "continue b", "channel": "C", "thread_ts": "track", "instructs": False},
                      event_id="self-thread")
    S.Supervisor(home=home, poster=lambda *a: posted.append(a)).post_unavailable([ev])
    S.Supervisor(home=home, poster=lambda *a: posted.append(a)).post_unavailable([ev])
    assert len(posted) == 1 and posted[0][:2] == ("C", "track")


@pytest.mark.parametrize("channel,thread", [("C", None), (None, None)])
def test_post_unavailable_self_without_thread_logs_locally_never_posts(home, channel, thread):
    posted = []
    ev = S.new_event("self", {"text": "continue b", "channel": channel, "thread_ts": thread, "instructs": False},
                      event_id="self-unroutable")
    S.Supervisor(home=home, poster=lambda *a: posted.append(a)).post_unavailable([ev])
    S.Supervisor(home=home, poster=lambda *a: posted.append(a)).post_unavailable([ev])
    assert not posted, "an unroutable self notice must never fall back to a top-level Slack post"
    rows = S.read_jsonl(os.path.join(home, "logs", "self-replies.jsonl"))
    assert len(rows) == 1 and rows[0]["id"] == "self-unroutable" and rows[0]["text"]


def test_post_unavailable_mixed_batch_routes_each_source_independently(home):
    posted = []
    events = [S.new_event("self", {"text": "c", "channel": "C", "instructs": False}, event_id="local-self"),
              S.new_event("self", {"text": "c", "channel": "C", "thread_ts": "track", "instructs": False},
                          event_id="thread-self"),
              S.new_event("cli", {"text": "founder task", "channel": "C", "user": "founder-console",
                                   "instructs": True}, event_id="founder-cli"),
              S.new_event("slack", {"text": "external", "channel": "C", "thread_ts": "external",
                                     "instructs": True}, event_id="external-slack")]
    for _ in range(2):
        S.Supervisor(home=home, poster=lambda *a: posted.append(a)).post_unavailable(events)
    assert sorted((p[0], p[1]) for p in posted) == [("C", "external"), ("C", "track")]
    assert all("via console" not in p[2] for p in posted), "a self notice must never read as console impersonation"
    rows = S.read_jsonl(os.path.join(home, "logs", "self-replies.jsonl"))
    assert len(rows) == 1 and rows[0]["id"] == "local-self"
    replies = os.path.join(home, "inbox", "replies")
    assert open(os.path.join(replies, "founder-cli.txt")).read().strip()
    assert not os.path.exists(os.path.join(replies, "local-self.txt"))
    assert not os.path.exists(os.path.join(replies, "thread-self.txt"))


def test_all_engines_failed_self_event_with_thread_posts_only_there(home, quota_engine, poster):
    """Through real service dispatch (turn -> AllEnginesFailed -> post_unavailable), not a direct invoke."""
    ev = S.new_event("self", {"text": "continue x", "channel": "C", "thread_ts": "track", "instructs": False})
    S.append_event(home, ev)
    sup = S.Supervisor(home=home, engines=engines(quota_engine, quota_engine), poster=poster)
    assert sup.run_once() is True
    assert len(poster.posted) == 1 and poster.posted[0][:2] == ("C", "track")


def test_all_engines_failed_self_event_without_thread_never_posts_top_level(home, quota_engine, poster):
    ev = S.new_event("self", {"text": "continue x", "channel": "C", "thread_ts": None, "instructs": False},
                      event_id="self-x")
    S.append_event(home, ev)
    sup = S.Supervisor(home=home, engines=engines(quota_engine, quota_engine), poster=poster)
    assert sup.run_once() is True
    assert not poster.posted
    rows = S.read_jsonl(os.path.join(home, "logs", "self-replies.jsonl"))
    assert len(rows) == 1 and rows[0]["id"] == "self-x" and rows[0]["text"]


# --------------------------------------------------------------------------------------- prior repair boundaries: B1/B2/S1

def _status(home, **fields):
    base = {"turn": 1, "mode": "continue", "track": "t", "channel": "C", "thread_ts": "th", "message_ts": "m",
            "next_action": "go"}
    base.update(fields)
    S.write_text(os.path.join(home, "work-status.json"), json.dumps(base))


def test_continuation_intent_saved_checkpoint_crash_preserves_timestamp_and_cap(home):
    """B1: a crash right after the durable intent (before the reservation is even counted) must not let
    recovery lose or re-stamp the original reservation timestamp, and the hourly cap must still see it."""
    _status(home)
    sup = S.Supervisor(home=home, clock=lambda: 1000.0, config={"continuation": {"max_per_hour": 1}},
                       poster=lambda *a: None)

    def crash(name):
        if name == "continuation_intent_saved":
            raise RuntimeError("fixture crash")
    with patch.object(sup, "persistence_checkpoint", side_effect=crash):
        with pytest.raises(RuntimeError):
            sup.settle_work(1, [])
    assert not S.pending_events(home)

    restarted = S.Supervisor(home=home, clock=lambda: 1100.0, config={"continuation": {"max_per_hour": 1}},
                             poster=lambda *a: None)
    with patch.object(restarted, "turn", return_value=False), patch.object(restarted, "compaction_due",
                                                                           return_value=(False, "")):
        restarted.run_once()
    reservations = S.read_jsonl(restarted.continuation_reservations_path())
    assert len(reservations) == 1 and reservations[0]["at"] == 1000, "the original reservation timestamp was lost"
    assert [e["id"] for e in S.pending_events(home)] == ["continue-1"]

    # the cap (1/hour) must see this recovered reservation, not a fresh one
    restarted.deliver({"slack": [], "cli": []}, ["continue-1"], 1)
    _status(home, turn=2)
    S.Supervisor(home=home, clock=lambda: 1150.0, config={"continuation": {"max_per_hour": 1}},
                poster=lambda *a: None).settle_work(2, [])
    assert not S.pending_events(home), "the hourly cap must not be bypassed after recovery"


def test_settlement_before_delivery_crash_recovers_without_rerunning_engine(home, ok_engine, poster):
    """B2: a crash immediately after the pre-delivery settlement journal (before `deliver` is even attempted)
    must recover and complete delivery on restart without a second engine invocation."""
    eid = queue_event(home, "x")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)

    def crash(name):
        if name == "settlement_before_delivery":
            raise RuntimeError("fixture crash")
    with patch.object(sup, "persistence_checkpoint", side_effect=crash):
        with pytest.raises(RuntimeError):
            sup.run_once()
    assert eid not in S.handled_ids(home)
    calls_before = len(calls(os.path.dirname(ok_engine)))

    restarted = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    assert restarted.run_once() is True
    assert eid in S.handled_ids(home)
    assert len(calls(os.path.dirname(ok_engine))) == calls_before, "recovery must not rerun the engine"
    assert poster.posted, "the confirmed reply must still be delivered on recovery"


def test_settlement_completed_crash_does_not_duplicate_confirmed_reply(home, ok_engine, poster):
    """B2: a crash after a fully confirmed delivery (between settle_work and clearing the settlement journal)
    must never repost the already-confirmed reply or rerun the engine on restart."""
    eid = queue_event(home, "x")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)

    def crash(name):
        if name == "settlement_completed":
            raise RuntimeError("fixture crash")
    with patch.object(sup, "persistence_checkpoint", side_effect=crash):
        with pytest.raises(RuntimeError):
            sup.run_once()
    assert eid in S.handled_ids(home), "delivery was already confirmed before this checkpoint"
    assert len(poster.posted) == 1
    calls_before = len(calls(os.path.dirname(ok_engine)))

    restarted = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    with patch.object(restarted, "turn", return_value=False), patch.object(restarted, "compaction_due",
                                                                           return_value=(False, "")):
        restarted.run_once()
    assert len(poster.posted) == 1, "a confirmed reply must never be reposted during settlement recovery"
    assert len(calls(os.path.dirname(ok_engine))) == calls_before


def test_failed_waiting_clock_removal_survives_restart_and_completes(home):
    """S1: a failed removal of an old waiting-clock target must not be forgotten across a restart; the new
    Supervisor instance must still retry and finish removing it."""
    applied = set()
    failing = {"old": True}

    def add(channel, ts, name):
        applied.add((channel, ts, name))

    def remove(channel, ts, name):
        if ts == "old" and failing["old"]:
            raise RuntimeError("offline")
        applied.discard((channel, ts, name))
    reactor = SimpleNamespace(add=add, remove=remove)
    sup = S.Supervisor(home=home, reactor=reactor, poster=lambda *a: None)
    sup._set_desired_work_reaction(("C", "old", "timer_clock"))
    sup._set_desired_work_reaction(("C", "new", "timer_clock"))
    assert ("C", "old", "timer_clock") in applied, "the removal failure must not be silently treated as done"

    failing["old"] = False
    restarted = S.Supervisor(home=home, reactor=reactor, poster=lambda *a: None)
    restarted.run_once()
    assert ("C", "old", "timer_clock") not in applied, "a restart must still retry the pending removal"
    assert ("C", "new", "timer_clock") in applied
