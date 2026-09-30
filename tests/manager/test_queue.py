"""The queue: duplicate ids dropped, batching order, the handled log, the batched message."""
import json
import os

from conftest import S, calls, engines, queue_event


def test_duplicate_ids_are_dropped(home):
    queue_event(home, "one", ts="1.1")
    queue_event(home, "one again", ts="1.1")
    queue_event(home, "two", ts="1.2")
    pending = S.pending_events(home)
    assert [e["id"] for e in pending] == ["1.1", "1.2"]
    assert pending[0]["payload"]["text"] == "one"


def test_handled_ids_are_skipped(home):
    queue_event(home, "one", ts="1.1")
    S.mark_handled(home, ["1.1"], turn=1)
    assert S.pending_events(home) == []
    queue_event(home, "redelivered", ts="1.1")
    assert S.pending_events(home) == []
    handled = S.read_jsonl(os.path.join(home, "inbox", "handled.jsonl"))
    assert handled[0]["id"] == "1.1" and handled[0]["turn"] == 1


def test_batching_keeps_queue_order_and_one_header_per_event(home, ok_engine, poster):
    for i in range(3):
        queue_event(home, f"message {i}", ts=f"2.{i}")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    assert sup.run_once() is True
    stdin = calls(os.path.dirname(ok_engine))[-1]["stdin"]
    assert stdin.count("source: slack") == 3
    assert stdin.index("message 0") < stdin.index("message 1") < stdin.index("message 2")
    assert "handled 3 events" in poster.texts
    assert poster.posted == [("C_DEV", "1.0", poster.posted[0][2])], "one reply per thread, not per event"
    handled = {r["id"] for r in S.read_jsonl(os.path.join(home, "inbox", "handled.jsonl"))}
    assert handled == {"2.0", "2.1", "2.2"}
    assert sup.run_once() is False, "nothing left after the batch"


def test_event_header_fields(home):
    ev = {"id": "x", "source": "slack", "at": 0,
          "payload": {"channel": "C1", "thread_ts": "9.9", "user": "U1", "text": "hi", "instructs": False,
                      "files": [{"path": "/tmp/f.png", "name": "f.png"}]}}
    header = S.event_header(ev)
    for line in ("source: slack", "channel: C1", "thread: 9.9", "sender: U1", "instructs: false",
                 "attachments: /tmp/f.png"):
        assert line in header
    msg = S.build_message([ev])
    assert msg.count("source:") == 1 and "hi" in msg


def test_replies_go_to_each_distinct_thread(home, ok_engine, poster):
    queue_event(home, "a", ts="3.1", channel="C_DEV", thread="1.0")
    queue_event(home, "b", ts="3.2", channel="C_DEV", thread="2.0")
    queue_event(home, "c", ts="3.3", channel="C_DEV", thread="1.0")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    assert sup.run_once() is True
    assert sorted((c, t) for c, t, _ in poster.posted) == [("C_DEV", "1.0"), ("C_DEV", "2.0")]


def test_turn_log_records_engine_events_and_duration(home, ok_engine, poster):
    eid = queue_event(home, "log me")
    S.Supervisor(home=home, engines=engines(ok_engine), poster=poster).run_once()
    turn = S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))[-1]
    assert turn["engine"] == "claude-r2d2" and turn["events"] == [eid] and turn["n"] == 1
    assert isinstance(turn["duration_s"], float)


def test_timer_event_is_added_once_per_slot(home):
    sup = S.Supervisor(home=home, engines={}, poster=lambda *a: None)
    assert sup.add_timer_event(now=1000.0)["id"] == "timer-1"
    assert sup.add_timer_event(now=1500.0) is None
    assert sup.add_timer_event(now=1900.0)["id"] == "timer-2"
    pending = S.pending_events(home)
    assert [e["source"] for e in pending] == ["timer", "timer"]
    assert pending[0]["payload"]["instructs"] is False


def test_timer_only_turn_posts_nothing(home, ok_engine, poster):
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    sup.add_timer_event(now=1000.0)
    assert sup.run_once() is True
    assert poster.posted == []
    assert "timer-1" in S.handled_ids(home)
    assert "gh" in calls(os.path.dirname(ok_engine))[-1]["stdin"]


def test_cli_event_reply_is_written_and_mirrored(home, ok_engine, poster):
    ev = S.append_event(home, S.new_event("cli", {"text": "console question", "user": "founder-console",
                                                  "instructs": True, "channel": "C_DEV", "thread_ts": None}))
    S.Supervisor(home=home, engines=engines(ok_engine), poster=poster).run_once()
    reply = open(os.path.join(home, "inbox", "replies", f"{ev['id']}.txt")).read()
    assert "handled 1 events" in reply
    assert poster.posted[-1][0] == "C_DEV" and poster.posted[-1][1] is None
    assert "from founder-console via console: console question" in poster.posted[-1][2]
