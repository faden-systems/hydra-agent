"""Direct posts (loops/b6.md): every post the manager makes goes through one path, `post_and_record` (the bridge's
`post`, the supervisor's delivery): post, record the join, mirror the line with its `thread_ts`. `hydra post` writes
`inbox/outbox.jsonl`; the bridge drains it through that path, in order, keeping a failed line for the next drain and
moving a poison line aside after `OUTBOX_MAX_ATTEMPTS`. `-` posts top level and the ts the poster returns names the
joined thread. `status` counts direct posts. No test here talks to Slack."""
import os
import subprocess
import sys
import threading
import time


from conftest import B, S, MANAGER, FakePoster, engines, fake_engine, queue_event

HYDRA = os.path.join(MANAGER, "hydra")


class TsPoster(FakePoster):
    """Returns Slack's shape of answer with `ts` when one is given; fails the first `fail` calls."""

    def __init__(self, ts=None, fail=0):
        super().__init__()
        self.ts, self.fail = ts, fail

    def __call__(self, channel, thread_ts, text):
        if self.fail > 0:
            self.fail -= 1
            raise RuntimeError("slack down")
        super().__call__(channel, thread_ts, text)
        return {"ok": True, "ts": self.ts} if self.ts else None


def mirror(home, channel="C_DEV"):
    return S.read_jsonl(os.path.join(home, "mirror", f"{channel}.jsonl"))


def threads(home):
    return S.load_threads(home)


def ids(home):
    return [e["id"] for e in S.read_jsonl(os.path.join(home, "inbox", "events.jsonl"))]


def make_bridge(home, poster):
    return B.Bridge(home=home, allowlist={"U_FOUNDER": {"instructs": True}}, poster=poster, token_env={}, bot_user_id="U_MANAGER")


def run(home, *args, **kw):
    return subprocess.run([sys.executable, HYDRA, *args], env={**os.environ, "HYDRA_HOME": home, **kw.pop("env", {})},
                          capture_output=True, text=True, timeout=60, **kw)


# ----------------------------------------------------------------------------------------------- the outbox

def test_queue_post_writes_an_outbox_line(home):
    rec = S.queue_post(home, "C_DEV", "77.1", "hello", user="hydra@post")
    lines = S.read_outbox(home)
    assert len(lines) == 1 and lines[0]["id"] == rec["id"] and lines[0]["at"]
    assert (lines[0]["channel"], lines[0]["thread_ts"], lines[0]["text"], lines[0]["user"]) == ("C_DEV", "77.1", "hello", "hydra@post")
    assert S.queue_post(home, "C_DEV", "-", "top")["thread_ts"] is None, "`-` means top level"
    assert S.queue_post(home, "C_DEV", None, "top")["thread_ts"] is None
    assert len(S.read_outbox(home)) == 3


def test_drain_posts_in_order_through_the_common_path(home, poster):
    br = make_bridge(home, poster)
    S.queue_post(home, "C_DEV", "77.1", "first")
    S.queue_post(home, "C_OPS", "5.0", "second")
    assert br.drain_outbox() == 2
    assert poster.posted == [("C_DEV", "77.1", "first"), ("C_OPS", "5.0", "second")]
    assert "77.1" in threads(home)["C_DEV"] and "5.0" in threads(home)["C_OPS"], "a direct post joins its thread"
    m = mirror(home)[-1]
    assert m["thread_ts"] == "77.1" and m["text"] == "first" and m["user"] == "manager" and m["subtype"] == "manager_post"
    assert mirror(home, "C_OPS")[-1]["thread_ts"] == "5.0"
    assert not os.path.exists(S.outbox_path(home)), "a drained outbox is gone"
    assert br.drain_outbox() == 0 and len(poster.posted) == 2, "drained once"
    assert S.outbox_stats(home)["posted"] == 2 and S.outbox_stats(home)["failed"] == 0


def test_top_level_post_joins_the_thread_the_poster_names(home):
    br = make_bridge(home, TsPoster(ts="88.1"))
    S.queue_post(home, "C_DEV", "-", "a new top-level post")
    assert br.drain_outbox() == 1
    assert br.poster.posted[-1] == ("C_DEV", None, "a new top-level post")
    assert "88.1" in threads(home)["C_DEV"], "the ts the poster returns is the joined thread"
    m = mirror(home)[-1]
    assert m["ts"] == "88.1" and m["thread_ts"] == "88.1" and m["text"] == "a new top-level post"
    br.handle_message({"channel": "C_DEV", "ts": "88.2", "thread_ts": "88.1", "user": "U_FOUNDER", "text": "a reply"})
    assert "88.2" in ids(home), "replies to a direct top-level post reach the manager"


def test_dry_top_level_post_records_no_join(home, poster):
    br = make_bridge(home, poster)
    S.queue_post(home, "C_DEV", None, "dry top level")
    assert br.drain_outbox() == 1 and poster.posted[-1] == ("C_DEV", None, "dry top level")
    assert threads(home) == {}, "a poster that returns nothing names no thread"
    assert mirror(home)[-1]["thread_ts"] is None and mirror(home)[-1]["ts"] is None


def test_replies_to_a_direct_thread_post_reach_the_manager(home, poster):
    br = make_bridge(home, poster)
    S.queue_post(home, "C_DEV", "77.1", "hello from inside a turn")
    assert br.drain_outbox() == 1
    br.handle_message({"channel": "C_DEV", "ts": "77.2", "thread_ts": "77.1", "user": "U_FOUNDER", "text": "reply"})
    assert "77.2" in ids(home)
    br.handle_message({"channel": "C_DEV", "ts": "78.2", "thread_ts": "78.1", "user": "U_FOUNDER", "text": "elsewhere"})
    assert "78.2" not in ids(home), "an unjoined thread stays unjoined"


def test_bridge_post_and_supervisor_delivery_share_the_path(home, tmp_path):
    post = TsPoster(ts="100.5")
    br = make_bridge(home, post)
    br.post("C_DEV", "1.0", "direct")
    assert post.posted[-1] == ("C_DEV", "1.0", "direct") and "1.0" in threads(home)["C_DEV"]
    m = mirror(home)[-1]
    assert (m["thread_ts"], m["ts"], m["subtype"], m["text"]) == ("1.0", "100.5", "manager_post", "direct")
    d = tmp_path / "eng"; d.mkdir()
    queue_event(home, "hello", thread="77.1")
    assert S.Supervisor(home=home, engines=engines(fake_engine(str(d), "ok")), poster=post).run_once() is True
    assert post.posted[-1][:2] == ("C_DEV", "77.1") and "77.1" in threads(home)["C_DEV"]
    m = mirror(home)[-1]
    assert (m["thread_ts"], m["ts"], m["subtype"], m["turn"]) == ("77.1", "100.5", "manager_reply", 1)
    # the supervisor's top-level posts name the thread they start, like a `-` post
    S.post_and_record(home, post, "C_DEV", None, "top", turn=2)
    assert "100.5" in threads(home)["C_DEV"] and mirror(home)[-1]["thread_ts"] == "100.5"


def test_a_failed_post_stays_queued_in_order(home):
    post = TsPoster(fail=1)
    br = make_bridge(home, post)
    S.queue_post(home, "C_DEV", "1.0", "first")
    S.queue_post(home, "C_DEV", "1.0", "second")
    assert br.drain_outbox() == 0 and post.posted == []
    lines = S.read_outbox(home)
    assert [l["text"] for l in lines] == ["first", "second"] and lines[0]["attempts"] == 1 and "slack down" in lines[0]["error"]
    assert "second" not in open(os.path.join(home, "mirror", "C_DEV.jsonl")).read() if os.path.exists(os.path.join(home, "mirror", "C_DEV.jsonl")) else True
    assert br.drain_outbox() == 2
    assert [p[2] for p in post.posted] == ["first", "second"]
    assert not os.path.exists(S.outbox_path(home))


def test_a_poison_line_moves_aside_after_max_attempts(home):
    post = TsPoster(fail=S.OUTBOX_MAX_ATTEMPTS)
    br = make_bridge(home, post)
    S.queue_post(home, "C_DEV", "1.0", "poison")
    S.queue_post(home, "C_DEV", "1.0", "fine")
    for _ in range(S.OUTBOX_MAX_ATTEMPTS - 1):
        assert br.drain_outbox() == 0
    assert br.drain_outbox() == 1, "the poison line is set aside and the next one goes out"
    assert [p[2] for p in post.posted] == ["fine"]
    failed = S.read_jsonl(S.outbox_failed_path(home))
    assert len(failed) == 1 and failed[0]["text"] == "poison" and failed[0]["attempts"] == S.OUTBOX_MAX_ATTEMPTS
    assert not os.path.exists(S.outbox_path(home))
    assert S.outbox_stats(home) == {**S.outbox_stats(home), "posted": 1, "failed": 1}


def test_queue_post_while_draining_is_not_lost(home, poster):
    """`hydra post` may append while the bridge posts: the rewrite keeps lines added meanwhile."""
    br = make_bridge(home, poster)
    S.queue_post(home, "C_DEV", "1.0", "first")
    original = S.post_and_record

    def post_and_queue_another(*a, **kw):
        S.queue_post(home, "C_DEV", "1.0", "appended during the drain")
        return original(*a, **kw)
    S.post_and_record = post_and_queue_another
    try:
        assert br.drain_outbox() == 1
    finally:
        S.post_and_record = original
    assert [l["text"] for l in S.read_outbox(home)] == ["appended during the drain"]
    assert br.drain_outbox() == 1 and poster.posted[-1][2] == "appended during the drain"


def test_status_counts_direct_posts(home, poster):
    assert "direct posts: 0 posted, 0 queued" in S.status_text(home)
    S.queue_post(home, "C_DEV", "1.0", "x")
    assert "direct posts: 0 posted, 1 queued" in S.status_text(home)
    make_bridge(home, poster).drain_outbox()
    assert "direct posts: 1 posted, 0 queued" in S.status_text(home)


def test_own_last_message_includes_direct_posts(home):
    br = make_bridge(home, TsPoster(ts="90.5"))
    br.post("C_DEV", None, "mine")
    assert B.own_last_message(home) == ("C_DEV", "90.5")


def test_sdk_poster_returns_slacks_answer():
    class Response:
        data = {"ok": True, "ts": "1.2"}

    class Client:
        def chat_postMessage(self, **kw):
            self.kw = kw
            return Response()
    c = Client()
    post = B.sdk_poster(c)
    assert post("C_DEV", "1.0", "hi") == {"ok": True, "ts": "1.2"}
    assert c.kw == {"channel": "C_DEV", "thread_ts": "1.0", "text": "hi"}
    assert post("C_DEV", None, "top") == {"ok": True, "ts": "1.2"} and "thread_ts" not in c.kw


def test_pump_drains_until_stopped(home, poster):
    br = make_bridge(home, poster)
    stop = threading.Event()
    t = threading.Thread(target=br.pump_outbox, args=(stop, 0.05), daemon=True)
    t.start()
    S.queue_post(home, "C_DEV", "1.0", "pumped")
    deadline = time.monotonic() + 5
    while not poster.posted and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    t.join(5)
    assert not t.is_alive() and poster.posted == [("C_DEV", "1.0", "pumped")]


def test_pump_survives_a_broken_drain(home, poster, capsys):
    br = make_bridge(home, poster)
    stop = threading.Event()
    calls = []

    def broken():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        if len(calls) >= 3:
            stop.set()
        return 0
    br.drain_outbox = broken
    br.pump_outbox(stop, 0.01)
    assert len(calls) >= 3 and "disk full" in capsys.readouterr().err


def test_bridge_pid_file(home):
    assert B.bridge_alive(home) is False
    B.write_bridge_pid(home)
    assert B.bridge_alive(home) is True
    S.write_text(os.path.join(home, "logs", "bridge.pid"), "999999\n")
    assert B.bridge_alive(home) is False


# ----------------------------------------------------------------------------------------------- hydra post

def test_hydra_post_queues_and_says_so_without_a_bridge(home):
    r = run(home, "post", "C_DEV", "77.1", "hello", "from", "a", "turn")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "queued" in r.stdout and "outbox" in r.stdout
    line = S.read_outbox(home)[-1]
    assert (line["channel"], line["thread_ts"], line["text"]) == ("C_DEV", "77.1", "hello from a turn") and line["user"]
    r = run(home, "post", "C_DEV", "-", "top level")
    assert r.returncode == 0 and S.read_outbox(home)[-1]["thread_ts"] is None
    assert len(S.read_outbox(home)) == 2


def test_hydra_post_reads_the_text_from_stdin(home):
    r = run(home, "post", "C_DEV", "1.0", input="line one\nline two\n")
    assert r.returncode == 0, r.stderr
    assert S.read_outbox(home)[-1]["text"] == "line one\nline two"


def test_hydra_post_usage(home):
    for args in (("post",), ("post", "C_DEV"), ("post", "C_DEV", "1.0")):
        r = run(home, *args, input="")
        assert r.returncode == 2 and "usage" in r.stderr, (args, r.stderr)
    assert not os.path.exists(S.outbox_path(home))


def test_hydra_post_waits_for_a_live_bridge(home):
    S.write_text(os.path.join(home, "logs", "bridge.pid"), f"{os.getpid()}\n")
    src = ("import os, sys, time, json; sys.path.insert(0, sys.argv[2]); import supervisor as S; h = sys.argv[1]\n"
           "def post(channel, thread_ts, text):\n"
           "    open(os.path.join(h, 'posted.txt'), 'a').write(json.dumps([channel, thread_ts, text]) + '\\n'); return {'ok': True, 'ts': '5.5'}\n"
           "deadline = time.time() + 30\n"
           "while time.time() < deadline:\n"
           "    if S.drain_outbox(h, lambda c, t, x: S.post_and_record(h, post, c, t, x, subtype='manager_post')): break\n"
           "    time.sleep(0.1)\n")
    responder = subprocess.Popen([sys.executable, "-c", src, home, MANAGER])
    try:
        r = run(home, "post", "C_DEV", "1.0", "wait for me")
    finally:
        responder.wait(timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().startswith("posted"), r.stdout
    assert open(os.path.join(home, "posted.txt")).read().strip() == '["C_DEV", "1.0", "wait for me"]'
    assert not os.path.exists(S.outbox_path(home))


def test_hydra_post_reports_a_bridge_that_does_not_drain(home):
    S.write_text(os.path.join(home, "logs", "bridge.pid"), f"{os.getpid()}\n")
    r = run(home, "post", "C_DEV", "1.0", "nobody drains", env={"HYDRA_POST_TIMEOUT": "1"})
    assert r.returncode == 1 and "still queued" in r.stderr, r.stdout + r.stderr
    assert len(S.read_outbox(home)) == 1, "the line stays for the bridge"


def test_hydra_post_reports_a_post_set_aside(home):
    S.write_text(os.path.join(home, "logs", "bridge.pid"), f"{os.getpid()}\n")
    src = ("import os, sys, time, json; sys.path.insert(0, sys.argv[2]); import supervisor as S; h = sys.argv[1]\n"
           "def post(channel, thread_ts, text): raise RuntimeError('channel_not_found')\n"
           "deadline = time.time() + 30\n"
           "while time.time() < deadline and os.path.exists(S.outbox_path(h)) or not S.read_jsonl(S.outbox_failed_path(h)):\n"
           "    S.drain_outbox(h, post); time.sleep(0.05)\n"
           "    if S.read_jsonl(S.outbox_failed_path(h)): break\n")
    responder = subprocess.Popen([sys.executable, "-c", src, home, MANAGER])
    try:
        r = run(home, "post", "C_DEV", "1.0", "doomed")
    finally:
        responder.wait(timeout=60)
    assert r.returncode == 1 and "channel_not_found" in r.stderr, r.stdout + r.stderr


def test_the_rules_tell_the_engine_to_post_through_hydra_post():
    text = open(os.path.join(MANAGER, "CLAUDE.md")).read()
    assert "hydra post <channel> <thread_ts|-> <text>" in text and "never call the Slack API directly" in text
