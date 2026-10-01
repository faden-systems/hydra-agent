"""The heartbeat reaction (loops/b6.md): while a turn runs, every `reactions.heartbeat_seconds` the supervisor swaps
the reaction on each triggering message between `eyes` and `hourglass_flowing_sand`; delivery removes whichever
stands; `0` disables it; a failing swap never blocks and the recorded name is the last add attempted. The ticker is
a background thread that waits with the supervisor's injectable `sleep`; every interval here is fractional so the
tests run in real but short time. No test here talks to Slack."""
import json
import os
import stat
import time

from conftest import S, FakePoster, engines, fake_engine, queue_event

NAMES = (S.REACTION_WORKING, S.REACTION_HEARTBEAT)


class Recorder:
    def __init__(self, fail_add=False, fail_remove=False):
        self.calls, self.fail_add, self.fail_remove = [], fail_add, fail_remove

    def add(self, channel, ts, name):
        self.calls.append(("add", channel, ts, name, time.monotonic()))
        if self.fail_add:
            raise RuntimeError("missing_scope")

    def remove(self, channel, ts, name):
        self.calls.append(("remove", channel, ts, name, time.monotonic()))
        if self.fail_remove:
            raise RuntimeError("no_reaction")

    def of(self, ts):
        return [(c[0], c[3]) for c in self.calls if c[2] == ts]


class TimedPoster(FakePoster):
    def __init__(self, fail=0):
        super().__init__()
        self.fail, self.t = fail, []

    def __call__(self, channel, thread_ts, text):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("slack down")
        super().__call__(channel, thread_ts, text)
        self.t.append(time.monotonic())


def slow_engine(tmp_path, seconds, behaviour="ok", name="slow"):
    """An engine that takes `seconds` before it answers (or fails with a quota message)."""
    d = str(tmp_path / name)
    os.makedirs(d, exist_ok=True)
    p = fake_engine(d, behaviour)
    body = open(p).read().replace("msg=sys.stdin.read()\n", f"msg=sys.stdin.read()\nimport time\ntime.sleep({seconds})\n")
    open(p, "w").write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    return p


def alternation(seq):
    """Asserts `seq` is add eyes, then (remove standing, add other) pairs, then remove standing. Returns the swaps."""
    assert seq and seq[0] == ("add", S.REACTION_WORKING), seq
    assert seq[-1][0] == "remove", seq
    swaps = seq[1:-1]
    assert len(swaps) % 2 == 0, ("swaps come in remove/add pairs", seq)
    current, n = S.REACTION_WORKING, 0
    for k in range(0, len(swaps), 2):
        assert swaps[k] == ("remove", current), ("a swap removes the standing reaction", seq)
        assert swaps[k + 1][0] == "add" and swaps[k + 1][1] in NAMES and swaps[k + 1][1] != current, seq
        current = swaps[k + 1][1]
        n += 1
    assert seq[-1] == ("remove", current), ("delivery removes the standing reaction", seq)
    return n


def turns(home):
    return S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))


def heartbeat_config(seconds):
    return {"reactions": {"heartbeat_seconds": seconds}}


def test_heartbeat_alternates_on_every_message_until_delivery(home, tmp_path):
    eng = slow_engine(tmp_path, 0.9)
    rec, post = Recorder(), TimedPoster()
    sup = S.Supervisor(home=home, engines=engines(eng), poster=post, reactor=rec, config=heartbeat_config(0.2))
    queue_event(home, "slow", ts="10.1")
    queue_event(home, "slow too", ts="10.2")
    assert sup.run_once() is True
    for ts in ("10.1", "10.2"):
        n = alternation(rec.of(ts))
        assert 2 <= n <= 6, (ts, rec.of(ts))
    assert not [c for c in rec.calls if c[0] == "add" and c[4] > post.t[-1]], "no add after delivery"
    assert all(c[4] >= post.t[-1] for c in rec.calls[-2:]), "the final removes come after the reply is posted"
    assert S.read_reactions_state(home) == {}
    assert set(turns(home)[-1]["reacted"]) == {"10.1", "10.2"}
    n_calls = len(rec.calls)
    time.sleep(0.5)
    assert len(rec.calls) == n_calls, "the ticker is gone once the turn is over"


def test_heartbeat_reads_config_json(home, tmp_path):
    open(os.path.join(home, "config.json"), "w").write(json.dumps(heartbeat_config(0.2)))
    eng = slow_engine(tmp_path, 0.7)
    rec = Recorder()
    sup = S.Supervisor(home=home, engines=engines(eng), poster=TimedPoster(), reactor=rec)
    assert sup.heartbeat_interval() == 0.2
    queue_event(home, "slow", ts="11.1")
    assert sup.run_once() is True
    assert alternation(rec.of("11.1")) >= 1


def test_zero_disables_the_heartbeat(home, tmp_path):
    eng = slow_engine(tmp_path, 0.6)
    rec = Recorder()
    sup = S.Supervisor(home=home, engines=engines(eng), poster=TimedPoster(), reactor=rec, config=heartbeat_config(0))
    queue_event(home, "slow", ts="20.1")
    assert sup.run_once() is True
    assert rec.of("20.1") == [("add", "eyes"), ("remove", "eyes")]


def test_the_default_interval_is_twenty_seconds_and_never_fires_in_a_short_turn(home, ok_engine):
    rec = Recorder()
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=TimedPoster(), reactor=rec)
    assert sup.heartbeat_interval() == 20
    queue_event(home, "quick", ts="21.1")
    assert sup.run_once() is True
    assert rec.of("21.1") == [("add", "eyes"), ("remove", "eyes")]


def test_heartbeat_seconds_parsing():
    assert S.heartbeat_seconds({}) == 20
    assert S.heartbeat_seconds({"reactions": {}}) == 20
    assert S.heartbeat_seconds({"reactions": {"heartbeat_seconds": 2.5}}) == 2.5
    assert S.heartbeat_seconds({"reactions": {"heartbeat_seconds": 0}}) == 0
    assert S.heartbeat_seconds({"reactions": {"heartbeat_seconds": -3}}) == 0
    assert S.heartbeat_seconds({"reactions": {"heartbeat_seconds": "soon"}}) == 20
    assert S.heartbeat_seconds({"reactions": "nope"}) == 20


def test_a_failing_remove_does_not_stop_the_add_that_follows(home, tmp_path):
    eng = slow_engine(tmp_path, 0.7)
    rec, post = Recorder(fail_remove=True), TimedPoster()
    sup = S.Supervisor(home=home, engines=engines(eng), poster=post, reactor=rec, config=heartbeat_config(0.2))
    queue_event(home, "slow", ts="30.1")
    assert sup.run_once() is True and post.posted
    seq = rec.of("30.1")
    adds = [name for action, name in seq if action == "add"]
    assert len(adds) >= 3 and all(a != b for a, b in zip(adds, adds[1:])), ("adds keep alternating", seq)
    assert seq[-1] == ("remove", adds[-1]), "delivery removes the name of the last add attempted"
    assert not [c for c in rec.calls if c[0] == "add" and c[4] > post.t[-1]]
    assert S.read_reactions_state(home) == {}


def test_a_failing_swap_is_logged_once_per_hour_under_the_shared_key(home, tmp_path, capsys):
    eng = slow_engine(tmp_path, 0.7)
    rec = Recorder(fail_add=True, fail_remove=True)
    sup = S.Supervisor(home=home, engines=engines(eng), poster=TimedPoster(), reactor=rec, config=heartbeat_config(0.2))
    queue_event(home, "slow", ts="31.1")
    assert sup.run_once() is True
    assert len(rec.calls) >= 4, "the swaps were attempted"
    err = capsys.readouterr().err
    assert err.count("reaction ") == 1 and "failed" in err, err
    assert "reactions" in json.loads(open(os.path.join(home, "logs", "notes.json")).read())


def test_pending_delivery_keeps_the_standing_name_until_delivered(home, tmp_path):
    eng = slow_engine(tmp_path, 0.7)
    rec, post = Recorder(), TimedPoster(fail=1)
    sup = S.Supervisor(home=home, engines=engines(eng), poster=post, reactor=rec, config=heartbeat_config(0.2))
    queue_event(home, "slow", ts="40.1")
    assert sup.run_once() is True
    seq = rec.of("40.1")
    assert seq[-1][0] == "add" and seq[-1][1] in NAMES, "nothing is removed while the reply is pending"
    standing = seq[-1][1]
    assert S.read_reactions_state(home)["40.1"]["name"] == standing
    n_calls = len(rec.calls)
    assert sup.run_once() is True, "the pending reply is delivered without a new turn"
    assert rec.of("40.1")[n_calls:] == [("remove", standing)]
    assert S.read_reactions_state(home) == {}


def test_every_engine_failing_replaces_the_standing_name_with_x(home, tmp_path):
    bad = slow_engine(tmp_path, 0.7, behaviour="quota", name="bad")
    rec = Recorder()
    sup = S.Supervisor(home=home, engines=engines(bad, bad), poster=TimedPoster(), reactor=rec, config=heartbeat_config(0.2))
    queue_event(home, "doomed", ts="50.1")
    assert sup.run_once() is True
    seq = rec.of("50.1")
    assert seq[0] == ("add", "eyes") and seq[-1] == ("add", "x"), seq
    swaps = seq[1:-2]
    assert len(swaps) >= 2 and len(swaps) % 2 == 0
    standing = swaps[-1][1] if swaps else "eyes"
    assert seq[-2] == ("remove", standing), ("x replaces whatever stands", seq)
    assert S.read_reactions_state(home)["50.1"]["name"] == "x"
    n_calls = len(rec.calls)
    time.sleep(0.5)
    assert len(rec.calls) == n_calls, "the ticker stopped with the failed turn"
    os.remove(os.path.join(home, "logs", "retry-after"))
    sup.engines = engines(slow_engine(tmp_path, 0.5, name="good"))
    assert sup.run_once() is True
    later = rec.of("50.1")[n_calls:]
    assert later[:2] == [("remove", "x"), ("add", "eyes")] and alternation(later[1:]) >= 1
    assert S.read_reactions_state(home) == {}


def test_the_ticker_waits_with_the_injectable_sleep(home, tmp_path):
    eng = slow_engine(tmp_path, 0.6)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        time.sleep(seconds)
    rec = Recorder()
    sup = S.Supervisor(home=home, engines=engines(eng), poster=TimedPoster(), reactor=rec, sleep=sleep,
                       config=heartbeat_config(0.1))
    queue_event(home, "slow", ts="60.1")
    assert sup.run_once() is True
    assert waits and all(w == 0.1 for w in waits), waits
    assert alternation(rec.of("60.1")) >= 2


def test_timer_events_are_not_swapped(home, tmp_path):
    eng = slow_engine(tmp_path, 0.5)
    rec = Recorder()
    sup = S.Supervisor(home=home, engines=engines(eng), poster=TimedPoster(), reactor=rec, config=heartbeat_config(0.1))
    sup.add_timer_event(now=1000.0)
    assert sup.run_once() is True
    assert rec.calls == []
