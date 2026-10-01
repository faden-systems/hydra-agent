"""The working indicator (loops/b5.md): an `eyes` reaction on every Slack message of a turn while it runs, removed
when the reply is delivered; kept while a reply is pending; replaced by `x` when every engine failed and cleared when a
later turn handles the event; nothing for timer and cli events; failures never block a turn and are logged once per
hour. Plus the real reactor (the bridge's slack_sdk adapter, mocked), the dry one, and `bridge.py --check`'s
scope probe. No test here talks to Slack."""
import json
import os
import time

import pytest
from slack_sdk.errors import SlackApiError

from conftest import B, S, FakePoster, engines, fake_engine, queue_event


class Recorder:
    """A reactor that records (action, channel, ts, name, when) and can fail on add."""

    def __init__(self, fail_add=False, fail_remove=False):
        self.calls = []
        self.fail_add, self.fail_remove = fail_add, fail_remove

    def add(self, channel, ts, name):
        self.calls.append(("add", channel, ts, name, time.monotonic()))
        if self.fail_add:
            raise RuntimeError("missing_scope")

    def remove(self, channel, ts, name):
        self.calls.append(("remove", channel, ts, name, time.monotonic()))
        if self.fail_remove:
            raise RuntimeError("no_reaction")

    def of(self, ts, action=None):
        return [(c[0], c[3]) for c in self.calls if c[2] == ts and (action is None or c[0] == action)]


class TimedPoster(FakePoster):
    """Records when each post happened; fails the first `fail` posts, or every post to `fail_thread`."""

    def __init__(self, fail=0, fail_thread=None):
        super().__init__()
        self.fail, self.fail_thread, self.t = fail, fail_thread, []

    def __call__(self, channel, thread_ts, text):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("slack down")
        if self.fail_thread is not None and thread_ts == self.fail_thread:
            raise RuntimeError("slack down for that thread")
        super().__call__(channel, thread_ts, text)
        self.t.append(time.monotonic())


def stamped_engine(tmp_path, name="stamped"):
    """An ok engine that also records when it ran (monotonic) in <dir>/ran."""
    d = str(tmp_path / name)
    os.makedirs(d, exist_ok=True)
    p = fake_engine(d, "ok")
    body = open(p).read().replace("msg=sys.stdin.read()\n",
                                  f"msg=sys.stdin.read()\nimport time\nopen({d!r}+'/ran','a').write(str(time.monotonic())+'\\n')\n")
    open(p, "w").write(body)
    return p


def ran_at(engine):
    return float(open(os.path.join(os.path.dirname(engine), "ran")).read().split()[0])


def turns(home):
    return S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))


def reactions_state(home):
    return S.read_reactions_state(home)


# ----------------------------------------------------------------------------------------------- the supervisor

def test_eyes_added_before_the_engine_and_removed_after_delivery(home, tmp_path):
    eng = stamped_engine(tmp_path)
    rec, post = Recorder(), TimedPoster()
    sup = S.Supervisor(home=home, engines=engines(eng), poster=post, reactor=rec)
    queue_event(home, "one", ts="10.1")
    queue_event(home, "two", ts="10.2")
    assert sup.run_once() is True
    adds = [c for c in rec.calls if c[0] == "add"]
    rems = [c for c in rec.calls if c[0] == "remove"]
    assert {c[2] for c in adds} == {"10.1", "10.2"} and all(c[1] == "C_DEV" and c[3] == "eyes" for c in adds)
    assert all(c[4] < ran_at(eng) for c in adds), "reactions go on before the engine runs"
    assert {c[2] for c in rems} == {"10.1", "10.2"} and all(c[3] == "eyes" for c in rems)
    assert all(c[4] >= post.t[-1] for c in rems), "and come off after the reply is posted"
    assert set(turns(home)[-1]["reacted"]) == {"10.1", "10.2"}
    assert reactions_state(home) == {}, "nothing stands once the reply is delivered"


def test_reaction_stays_while_the_reply_is_pending(home, ok_engine):
    rec, post = Recorder(), TimedPoster(fail=1)
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=post, reactor=rec)
    queue_event(home, "flaky", ts="20.1")
    assert sup.run_once() is True
    assert rec.of("20.1") == [("add", "eyes")], "no removal while the reply is pending"
    assert reactions_state(home)["20.1"]["name"] == "eyes"
    assert sup.run_once() is True, "the pending reply is delivered without a new turn"
    assert rec.of("20.1") == [("add", "eyes"), ("remove", "eyes")]
    assert reactions_state(home) == {}


def test_each_thread_is_released_as_its_reply_lands(home, ok_engine):
    rec, post = Recorder(), TimedPoster(fail_thread="2.0")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=post, reactor=rec)
    queue_event(home, "a", ts="30.1", thread="1.0")
    queue_event(home, "b", ts="30.2", thread="2.0")
    assert sup.run_once() is True
    assert rec.of("30.1") == [("add", "eyes"), ("remove", "eyes")], "thread 1.0 got its reply"
    assert rec.of("30.2") == [("add", "eyes")], "thread 2.0 did not"
    assert "30.1" in S.handled_ids(home) or os.path.exists(os.path.join(home, "inbox", "pending-replies.jsonl"))
    post.fail_thread = None
    assert sup.run_once() is True
    assert rec.of("30.2") == [("add", "eyes"), ("remove", "eyes")]
    assert len(rec.of("30.1")) == 2, "the delivered thread is not touched again"
    assert reactions_state(home) == {}


def test_x_when_every_engine_fails_cleared_when_handled_later(home, quota_engine, ok_engine):
    rec, post = Recorder(), TimedPoster()
    sup = S.Supervisor(home=home, engines=engines(quota_engine, quota_engine), poster=post, reactor=rec)
    queue_event(home, "doomed", ts="40.1")
    assert sup.run_once() is True
    assert rec.of("40.1") == [("add", "eyes"), ("remove", "eyes"), ("add", "x")]
    assert reactions_state(home)["40.1"]["name"] == "x"
    assert turns(home)[-1]["error"] and turns(home)[-1]["reacted"] == ["40.1"]
    os.remove(os.path.join(home, "logs", "retry-after"))
    sup.engines = engines(ok_engine)
    assert sup.run_once() is True
    assert rec.of("40.1") == [("add", "eyes"), ("remove", "eyes"), ("add", "x"),
                              ("remove", "x"), ("add", "eyes"), ("remove", "eyes")]
    assert reactions_state(home) == {}


def test_timer_and_cli_events_get_no_reaction(home, ok_engine):
    rec = Recorder()
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=FakePoster(), reactor=rec)
    sup.add_timer_event(now=1000.0)
    S.append_event(home, S.new_event("timer", {"text": "digest now", "digest": True, "channel": "C_DEV",
                                               "thread_ts": "1.0", "user": "U_FOUNDER", "instructs": True}))
    S.append_event(home, S.new_event("cli", {"text": "console", "user": "founder-console", "instructs": True,
                                             "channel": "C_DEV", "thread_ts": None}))
    assert sup.run_once() is True
    assert rec.calls == []
    assert turns(home)[-1]["reacted"] == []


def test_slack_event_without_payload_ts_uses_its_message_id(home, ok_engine):
    """Events queued by an older bridge carry the message ts only as their id."""
    rec = Recorder()
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=FakePoster(), reactor=rec)
    S.append_event(home, {"id": "50.1", "source": "slack", "at": time.time(),
                          "payload": {"channel": "C_DEV", "thread_ts": "1.0", "user": "U_FOUNDER", "text": "old", "instructs": True}})
    S.append_event(home, {"id": "not-a-ts", "source": "slack", "at": time.time(),
                          "payload": {"channel": "C_DEV", "thread_ts": "1.0", "user": "U_FOUNDER", "text": "odd", "instructs": True}})
    assert sup.run_once() is True
    assert [c[2] for c in rec.calls] == ["50.1", "50.1"], "an id that is not a message ts is not reacted to"


def test_add_failure_never_blocks_and_is_logged_once_per_hour(home, ok_engine, capsys):
    rec, post = Recorder(fail_add=True), FakePoster()
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=post, reactor=rec)
    queue_event(home, "no scope", ts="60.1")
    queue_event(home, "no scope either", ts="60.2")
    assert sup.run_once() is True and post.posted, "the reply goes out regardless"
    assert "60.1" in S.handled_ids(home) and "60.2" in S.handled_ids(home)
    err = capsys.readouterr().err
    assert err.count("reaction add failed") == 1, err
    assert "missing_scope" in err
    queue_event(home, "later", ts="60.3")
    assert sup.run_once() is True
    assert "reaction add failed" not in capsys.readouterr().err, "muted for an hour"
    notes = json.loads(open(os.path.join(home, "logs", "notes.json")).read())
    assert "reactions" in notes
    assert [c[0] for c in rec.calls if c[2] == "60.3"] == ["add", "remove"], "the removal is still attempted"


def test_remove_failure_is_tolerated(home, ok_engine):
    rec, post = Recorder(fail_remove=True), FakePoster()
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=post, reactor=rec)
    queue_event(home, "x", ts="61.1")
    assert sup.run_once() is True and "61.1" in S.handled_ids(home)
    assert reactions_state(home) == {}, "a reaction Slack would not remove is not kept on the books"


def test_default_reactor_is_dry_without_a_token(home, ok_engine):
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=FakePoster())
    assert isinstance(sup.reactor, S.DryReactor)
    queue_event(home, "dry", ts="70.1")
    assert sup.run_once() is True
    rows = S.read_jsonl(os.path.join(home, "logs", "reactions.jsonl"))
    assert [(r["action"], r["channel"], r["ts"], r["name"]) for r in rows] == \
        [("add", "C_DEV", "70.1", "eyes"), ("remove", "C_DEV", "70.1", "eyes")]


def test_default_reactor_is_the_bridges_sdk_adapter_with_a_token(home, monkeypatch):
    open(os.path.join(home, "credentials", "slack.env"), "w").write("SLACK_BOT_TOKEN=fake-token\n")
    r = S.default_reactor(home)
    assert isinstance(r, B.SdkReactor)
    assert type(r.client).__name__ == "WebClient" and r.client.token == "fake-token"
    sup = S.Supervisor(home=home, engines={}, poster=FakePoster())
    assert isinstance(sup.reactor, B.SdkReactor)
    # slack_sdk missing: the dry reactor, with one log line

    def no_sdk(token):
        raise ImportError("No module named 'slack_sdk'")
    monkeypatch.setattr(B, "sdk_reactor", no_sdk)
    assert isinstance(S.default_reactor(home), S.DryReactor)


# ----------------------------------------------------------------------------------------------- the real one

class FakeClient:
    """Stands in for slack_sdk's WebClient: records the calls, raises SlackApiError from a queue of Slack errors."""

    def __init__(self, errors=()):
        self.calls, self.errors = [], list(errors)

    def _answer(self, action, kw):
        self.calls.append((action, kw))
        error = self.errors.pop(0) if self.errors else None
        if error:
            raise SlackApiError(f"The request to the Slack API failed. ({error})", {"ok": False, "error": error})
        return {"ok": True}

    def reactions_add(self, **kw):
        return self._answer("add", kw)

    def reactions_remove(self, **kw):
        return self._answer("remove", kw)


def test_sdk_reactor_calls_reactions_add_and_remove():
    c = FakeClient()
    r = B.SdkReactor(c)
    r.add("C1", "1.5", "eyes")
    r.remove("C1", "1.5", "eyes")
    assert c.calls == [("add", {"channel": "C1", "timestamp": "1.5", "name": "eyes"}),
                       ("remove", {"channel": "C1", "timestamp": "1.5", "name": "eyes"})]


def test_sdk_reactor_errors_and_idempotent_cases():
    c = FakeClient(["missing_scope", "already_reacted", "no_reaction", "message_not_found"])
    r = B.SdkReactor(c)
    with pytest.raises(SlackApiError, match="missing_scope"):
        r.add("C1", "1.5", "eyes")
    r.add("C1", "1.5", "eyes")  # already there: done
    r.remove("C1", "1.5", "eyes")  # already gone: done
    with pytest.raises(SlackApiError, match="message_not_found"):
        r.remove("C1", "1.5", "eyes")
    assert len(c.calls) == 4


def test_supervisor_with_the_sdk_reactor_survives_a_missing_scope(home, ok_engine, capsys):
    c = FakeClient(["missing_scope", "missing_scope"])
    post = FakePoster()
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=post, reactor=B.SdkReactor(c))
    queue_event(home, "hi", ts="71.1")
    assert sup.run_once() is True and post.posted and "71.1" in S.handled_ids(home)
    assert [a for a, _ in c.calls] == ["add", "remove"]
    assert "missing_scope" in capsys.readouterr().err


def test_sdk_scope_probe_adds_then_removes(monkeypatch):
    made = []

    def fake_sdk_reactor(token):
        made.append(token)
        return B.SdkReactor(FakeClient())
    monkeypatch.setattr(B, "sdk_reactor", fake_sdk_reactor)
    assert B.sdk_scope_probe("fake-token", "C1", "1.5") is None and made == ["fake-token"]
    monkeypatch.setattr(B, "sdk_reactor", lambda token: B.SdkReactor(FakeClient(["missing_scope"])))
    assert B.sdk_scope_probe("fake-token", "C1", "1.5") == "missing_scope"
    monkeypatch.setattr(B, "sdk_reactor", lambda token: B.SdkReactor(FakeClient(["already_reacted", "no_reaction"])))
    assert B.sdk_scope_probe("fake-token", "C1", "1.5") is None, "a reaction already there still proves the scope"
    monkeypatch.setattr(B, "sdk_reactor", lambda token: B.SdkReactor(FakeClient(["message_not_found"])))
    assert B.sdk_scope_probe("fake-token", "C1", "1.5") == "message_not_found"


# ----------------------------------------------------------------------------------------------- the bridge

def test_bridge_payload_carries_the_message_ts(home, poster):
    br = B.Bridge(home=home, allowlist={"U_FOUNDER": {"instructs": True}}, poster=poster, token_env={}, bot_user_id="U_M")
    br.handle_message({"channel": "C_DEV", "user": "U_FOUNDER", "text": "<@U_M> hi", "ts": "80.1"})
    ev = S.read_jsonl(os.path.join(home, "inbox", "events.jsonl"))[-1]
    assert ev["id"] == "80.1" and ev["payload"]["ts"] == "80.1"


def test_check_without_a_token_says_the_scope_is_unverified(home, capsys):
    open(os.path.join(home, "allowlist.json"), "w").write(json.dumps({"U_A": {"instructs": True}}))
    open(os.path.join(home, "credentials", "slack.env"), "w").write("")
    assert B.check(home) is True
    out = capsys.readouterr().out
    assert "reactions:write: unverified" in out and "SLACK_BOT_TOKEN" in out


def test_check_probes_the_scope_on_the_bots_last_message(home, capsys):
    open(os.path.join(home, "allowlist.json"), "w").write(json.dumps({"U_A": {"instructs": True}}))
    open(os.path.join(home, "credentials", "slack.env"), "w").write("SLACK_BOT_TOKEN=fake-token\nSLACK_APP_TOKEN=fake-app\n")
    probed = []

    def probe(token, channel, ts):
        probed.append((channel, ts))
        return None
    # no own message yet: unverified, still ok
    assert B.check(home, probe=probe) is True
    out = capsys.readouterr().out
    assert "reactions:write: unverified" in out and probed == [] and "fake-token" not in out
    # the supervisor mirrored two of its own posts: the latest with a ts is probed
    S.append_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"),
                   {"mirrored_at": 1.0, "type": "message", "ts": "90.1", "thread_ts": "1.0", "user": "manager",
                    "subtype": "manager_reply", "turn": 1, "text": "first"})
    S.append_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"),
                   {"mirrored_at": 2.0, "type": "message", "ts": "91.1", "thread_ts": "1.0", "user": "U_FOUNDER",
                    "subtype": None, "text": "not mine"})
    S.append_jsonl(os.path.join(home, "mirror", "C_OPS.jsonl"),
                   {"mirrored_at": 3.0, "type": "message", "ts": None, "thread_ts": None, "user": "manager",
                    "subtype": "manager_reply", "turn": 2, "text": "dry post without a ts"})
    assert B.check(home, probe=probe) is True
    assert probed == [("C_DEV", "90.1")]
    assert "reactions:write: ok" in capsys.readouterr().out
    # the token lacks the scope: a warning, not a failure
    assert B.check(home, probe=lambda token, channel, ts: "missing_scope") is True
    out = capsys.readouterr().out
    assert "WARNING" in out and "reactions:write" in out and "missing" in out
    # any other trouble (deleted message, network): unverified, not a failure
    assert B.check(home, probe=lambda token, channel, ts: "message_not_found") is True
    assert "reactions:write: unverified (message_not_found)" in capsys.readouterr().out
    assert B.check(home, probe=lambda token, channel, ts: (_ for _ in ()).throw(OSError("no network"))) is True
    assert "reactions:write: unverified" in capsys.readouterr().out
