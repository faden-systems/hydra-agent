"""Threads that reach the manager (loops/b4.md): thread identity on root and reply events; join on mention, on own
post, on an assignee line; queued without a mention once joined, not before; leave (founder only); the 14-day
prune; mirror thread_ts; the reply posting target and the supervisor's own join on posting; the status line."""
import datetime as dt
import json
import os

import pytest

from conftest import B, S, engines, fake_engine, queue_event


def read_queue(home):
    return S.read_jsonl(os.path.join(home, "inbox", "events.jsonl"))


def ids(home):
    return [e["id"] for e in read_queue(home)]


def threads(home):
    p = os.path.join(home, "threads.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def msg(text, user="U_FOUNDER", ts="10.0", thread=None, channel="C_DEV", **extra):
    ev = {"channel": channel, "user": user, "text": text, "ts": ts}
    if thread:
        ev["thread_ts"] = thread
    ev.update(extra)
    return ev


@pytest.fixture
def bridge(home, poster):
    return B.Bridge(home=home, allowlist={"U_FOUNDER": {"instructs": True}, "U_OPERATOR": {"instructs": False},
                                          "B_CODER": {"instructs": False}},
                    poster=poster, token_env={}, bot_user_id="U_MANAGER")


# ----------------------------------------------------------------------------------------------- identity and joins

def test_root_mention_joins_and_its_thread_is_its_own_ts(bridge, home):
    bridge.handle_message(msg("<@U_MANAGER> start here", ts="100.1"))
    q = read_queue(home)[-1]
    assert q["id"] == "100.1" and q["payload"]["thread_ts"] == "100.1"
    assert "100.1" in threads(home)["C_DEV"]
    rec = threads(home)["C_DEV"]["100.1"]
    assert rec["joined_at"] and rec["last_seen"]


def test_reply_in_a_joined_thread_is_queued_without_a_mention(bridge, home):
    bridge.handle_message(msg("<@U_MANAGER> start", ts="100.1"))
    bridge.handle_message(msg("follow-up", ts="100.2", thread="100.1"))
    bridge.handle_message(msg("done: step one", user="U_OPERATOR", ts="100.3", thread="100.1"))
    q = {e["id"]: e for e in read_queue(home)}
    assert q["100.2"]["payload"]["thread_ts"] == "100.1" and q["100.2"]["payload"]["addressed"] is False
    assert q["100.3"]["payload"]["thread_ts"] == "100.1" and q["100.3"]["payload"]["instructs"] is False
    assert threads(home)["C_DEV"]["100.1"]["last_seen"] >= threads(home)["C_DEV"]["100.1"]["joined_at"]


def test_reply_in_an_unjoined_thread_is_mirrored_only(bridge, home):
    bridge.handle_message(msg("chatter", ts="200.2", thread="200.1"))
    assert "200.2" not in ids(home)
    mirror = S.read_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"))[-1]
    assert mirror["text"] == "chatter" and mirror["thread_ts"] == "200.1"
    assert threads(home) == {} or "200.1" not in threads(home).get("C_DEV", {})


def test_top_level_without_a_mention_is_mirrored_only(bridge, home):
    bridge.handle_message(msg("just talking in the channel", ts="300.1"))
    assert "300.1" not in ids(home)
    mirror = S.read_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"))[-1]
    assert mirror["thread_ts"] is None, "a true top-level post is mirrored with thread_ts null"


def test_assignee_line_naming_the_manager_joins_without_a_mention(bridge, home):
    bridge.handle_message(msg("assignee: manager | track: t5\nplease plan", ts="400.1"))
    assert "400.1" in ids(home) and read_queue(home)[-1]["payload"]["thread_ts"] == "400.1"
    assert "400.1" in threads(home)["C_DEV"]
    bridge.handle_message(msg("Assignee: coder | track: t6\nnot for us", ts="400.2"))
    assert "400.2" not in ids(home), "an assignee line naming someone else does not join"
    bridge.handle_message(msg("assignee: coder, @manager | track: t7", ts="400.3"))
    assert "400.3" in ids(home), "the manager among several assignees counts"


def test_own_post_joins(bridge, home):
    bridge.note_own_post("C_DEV", "500.1")
    assert "500.1" in threads(home)["C_DEV"]
    bridge.handle_message(msg("reply to the manager's post", ts="500.2", thread="500.1"))
    assert "500.2" in ids(home)


def test_mention_in_a_reply_joins_that_thread(bridge, home):
    bridge.handle_message(msg("<@U_MANAGER> join us", ts="600.5", thread="600.1"))
    assert read_queue(home)[-1]["payload"]["thread_ts"] == "600.1" and "600.1" in threads(home)["C_DEV"]
    bridge.handle_message(msg("more", ts="600.6", thread="600.1"))
    assert "600.6" in ids(home)


def test_a_command_mention_joins_but_leave_does_not(bridge, home, poster):
    bridge.handle_message(msg("<@U_MANAGER> status", ts="700.2", thread="700.1"))
    assert "700.1" in threads(home)["C_DEV"] and "threads: 1 joined" in poster.posted[-1][2]
    bridge.handle_message(msg("<@U_MANAGER> leave", ts="800.2", thread="800.1"))
    assert "800.1" not in threads(home).get("C_DEV", {})


def test_bot_addressing_us_in_a_thread_joins_and_other_bots_never_queue(bridge, home):
    bridge.handle_message(msg("<@U_MANAGER> PR ready", user="U_X", bot_id="B_CODER", ts="900.2", thread="900.1"))
    assert "900.2" in ids(home) and "900.1" in threads(home)["C_DEV"]
    bridge.handle_message(msg("CI green", user="U_X", bot_id="B_CODER", ts="900.3", thread="900.1"))
    assert "900.3" not in ids(home), "a bot not addressing us is information for the channel, not an event"


def test_threads_are_per_channel(bridge, home):
    bridge.handle_message(msg("<@U_MANAGER> here", ts="1.1", channel="C_A"))
    bridge.handle_message(msg("same ts other channel", ts="1.2", thread="1.1", channel="C_B"))
    assert "1.2" not in ids(home)


# ----------------------------------------------------------------------------------------------- leave and prune

def test_leave_is_founder_only_and_stops_delivery(bridge, home, poster):
    bridge.handle_message(msg("<@U_MANAGER> start", ts="100.1"))
    bridge.handle_message(msg("<@U_MANAGER> leave", user="U_OPERATOR", ts="100.5", thread="100.1"))
    assert "not authorized" in poster.posted[-1][2] and "100.1" in threads(home)["C_DEV"]
    bridge.handle_message(msg("<@U_MANAGER> leave", ts="100.6", thread="100.1"))
    assert "100.1" not in threads(home).get("C_DEV", {}) and "left" in poster.posted[-1][2]
    bridge.handle_message(msg("after leave", ts="100.7", thread="100.1"))
    assert "100.7" not in ids(home)
    assert not any(e["id"] in ("100.5", "100.6") for e in read_queue(home)), "leave is a command, never a turn"


def test_leave_outside_a_joined_thread_says_so(bridge, home, poster):
    bridge.handle_message(msg("<@U_MANAGER> leave", ts="100.9"))
    assert "not in" in poster.posted[-1][2].lower()


def test_prune_drops_threads_silent_for_14_days(bridge, home):
    bridge.handle_message(msg("<@U_MANAGER> old", ts="1.1"))
    bridge.note_own_post("C_DEV", "2.1")
    th = threads(home)
    th["C_DEV"]["1.1"]["last_seen"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=15)).isoformat()
    th["C_DEV"]["2.1"]["last_seen"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=13)).isoformat()
    json.dump(th, open(os.path.join(home, "threads.json"), "w"))
    removed = bridge.prune()
    assert removed == [("C_DEV", "1.1")]
    assert "1.1" not in threads(home)["C_DEV"] and "2.1" in threads(home)["C_DEV"]


def test_prune_runs_on_bridge_start_and_daily(home, poster):
    S.note_thread(home, "C_DEV", "1.1", at="2020-01-01T00:00:00Z")
    S.note_thread(home, "C_DEV", "1.2")
    B.Bridge(home=home, allowlist={}, poster=poster, token_env={})
    assert "1.1" not in threads(home)["C_DEV"] and "1.2" in threads(home)["C_DEV"]
    br = B.Bridge(home=home, allowlist={}, poster=poster, token_env={})
    S.note_thread(home, "C_DEV", "1.3", at="2020-01-01T00:00:00Z")
    br.handle_message(msg("x", user="U_NOBODY", ts="9.9"))
    assert "1.3" in threads(home)["C_DEV"], "not pruned again within a day"
    br._last_prune -= 86401
    br.handle_message(msg("x", user="U_NOBODY", ts="9.10"))
    assert "1.3" not in threads(home)["C_DEV"]


def test_thread_helpers_are_atomic_and_tolerant(home):
    assert S.load_threads(home) == {}
    S.write_text(os.path.join(home, "threads.json"), "{not json")
    assert S.load_threads(home) == {}
    rec = S.note_thread(home, "C_DEV", "1.0", at="2026-10-01T00:00:00Z")
    assert rec == {"joined_at": "2026-10-01T00:00:00Z", "last_seen": "2026-10-01T00:00:00Z"}
    rec2 = S.note_thread(home, "C_DEV", "1.0", at="2026-10-02T00:00:00Z")
    assert rec2["joined_at"] == "2026-10-01T00:00:00Z" and rec2["last_seen"] == "2026-10-02T00:00:00Z"
    assert S.thread_joined(home, "C_DEV", "1.0") and not S.thread_joined(home, "C_DEV", "1.1")
    assert S.forget_thread(home, "C_DEV", "1.0") is True and S.forget_thread(home, "C_DEV", "1.0") is False
    assert S.threads_count(home) == 0 and not os.path.exists(os.path.join(home, "threads.json.tmp"))
    assert S.note_thread(home, None, "1.0") is None and S.note_thread(home, "C_DEV", None) is None


# ----------------------------------------------------------------------------------------------- the supervisor side

def test_reply_is_posted_into_the_thread_and_mirrored_with_it(home, poster, tmp_path):
    d = tmp_path / "eng"; d.mkdir()
    eng = fake_engine(str(d), "ok")
    br = B.Bridge(home=home, allowlist={"U_FOUNDER": {"instructs": True}}, poster=poster, token_env={}, bot_user_id="U_MANAGER")
    br.handle_message(msg("<@U_MANAGER> start", ts="100.1"))
    br.handle_message(msg("more", ts="100.2", thread="100.1"))
    assert S.Supervisor(home=home, engines=engines(eng), poster=poster).run_once() is True
    assert poster.posted[-1][:2] == ("C_DEV", "100.1")
    mirror = S.read_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"))[-1]
    assert mirror["user"] == "manager" and mirror["thread_ts"] == "100.1"


def test_supervisor_records_its_own_posts_as_joins(home, poster, tmp_path):
    d = tmp_path / "eng"; d.mkdir()
    eng = fake_engine(str(d), "ok")
    queue_event(home, "hello", thread="77.1")
    assert S.Supervisor(home=home, engines=engines(eng), poster=poster).run_once() is True
    assert "77.1" in threads(home)["C_DEV"]
    # a top-level post (thread None): the poster's returned ts names the thread it starts
    class TsPoster:
        def __call__(self, channel, thread_ts, text):
            return {"ok": True, "ts": "88.1"}
    queue_event(home, "console", source="cli", thread=None)
    assert S.Supervisor(home=home, engines=engines(eng), poster=TsPoster()).run_once() is True
    assert "88.1" in threads(home)["C_DEV"]


def test_status_counts_joined_threads(home, poster):
    assert "threads: 0 joined" in S.status_text(home)
    S.note_thread(home, "C_DEV", "1.0"); S.note_thread(home, "C_OPS", "2.0")
    assert "threads: 2 joined" in S.status_text(home)
