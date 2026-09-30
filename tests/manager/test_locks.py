"""PAUSE, the WRITER lock, the dead-man, pending replies after a delivery failure, persistence."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import MANAGER, S, calls, engines, queue_event


def test_pause_blocks_turns(home, ok_engine, poster):
    queue_event(home, "anything")
    open(os.path.join(home, "PAUSE"), "w").write("L\n")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    assert sup.run_once() is False and calls(os.path.dirname(ok_engine)) == []
    os.remove(os.path.join(home, "PAUSE"))
    assert sup.run_once() is True


def test_writer_lock_is_exclusive_and_released(home):
    with S.acquire_writer(home, "a") as path:
        assert open(path).read().split() == [str(os.getpid()), "a"]
        with pytest.raises(S.WriterHeld):
            with S.acquire_writer(home, "b"):
                pass
    assert not os.path.exists(os.path.join(home, "WRITER"))


def test_writer_lock_released_on_exception(home):
    with pytest.raises(RuntimeError):
        with S.acquire_writer(home, "x"):
            raise RuntimeError("boom")
    assert not os.path.exists(os.path.join(home, "WRITER"))


def test_stale_lock_is_reclaimed(home):
    open(os.path.join(home, "WRITER"), "w").write("999999 console\n")
    with S.acquire_writer(home, "me") as path:
        assert open(path).read().startswith(str(os.getpid()))
    assert S.writer_status(home) is None


def test_live_holder_in_another_process_blocks_the_supervisor(home, ok_engine, poster):
    holder = subprocess.Popen([sys.executable, "-c",
                               f"import sys, time; sys.path.insert(0, {MANAGER!r}); import supervisor as S\n"
                               f"with S.acquire_writer({home!r}, 'console'):\n"
                               "    print('held', flush=True); sys.stdin.readline()"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        queue_event(home, "blocked")
        sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
        assert sup.run_once() is False
        assert S.writer_status(home)[1] == "console"
        assert "console session" in S.status_text(home)
    finally:
        holder.stdin.write("\n"); holder.stdin.flush(); holder.wait(timeout=15)
    assert not os.path.exists(os.path.join(home, "WRITER"))
    assert sup.run_once() is True


def test_pending_reply_after_delivery_failure(home, ok_engine):
    class Flaky:
        def __init__(self):
            self.posted, self.fail = [], 1

        def __call__(self, channel, thread_ts, text):
            if self.fail:
                self.fail -= 1
                raise RuntimeError("slack down")
            self.posted.append((channel, thread_ts, text))
    post = Flaky()
    eid = queue_event(home, "deliver me")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=post)
    assert sup.run_once() is True
    assert eid not in S.handled_ids(home)
    pending = S.read_jsonl(os.path.join(home, "inbox", "pending-replies.jsonl"))
    assert pending and pending[0]["events"] == [eid]
    n = len(calls(os.path.dirname(ok_engine)))
    assert sup.run_once() is True
    assert len(calls(os.path.dirname(ok_engine))) == n
    assert post.posted and "handled 1 events" in post.posted[-1][2]
    assert eid in S.handled_ids(home)
    assert not os.path.exists(os.path.join(home, "inbox", "pending-replies.jsonl"))


def test_pending_delivery_failing_again_stays_pending(home, ok_engine):
    def always_down(channel, thread_ts, text):
        raise RuntimeError("down")
    eid = queue_event(home, "x")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=always_down)
    assert sup.run_once() is True
    assert sup.run_once() is False
    assert os.path.exists(os.path.join(home, "inbox", "pending-replies.jsonl")) and eid not in S.handled_ids(home)
    assert len(calls(os.path.dirname(ok_engine))) == 1


def test_deadman_due_only_with_queued_events_and_silence(home, ok_engine, poster):
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    assert sup.deadman_due() is False
    queue_event(home, "stuck")
    sup.heartbeat()
    assert sup.deadman_due() is False
    assert sup.deadman_due(now=time.time() + S.DEADMAN_S + 1) is True
    os.remove(os.path.join(home, "logs", "heartbeat"))
    assert sup.deadman_due() is True


def _git(*args, **kw):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, **kw)


@pytest.fixture
def clone(tmp_path):
    bare = str(tmp_path / "bare.git"); _git("init", "-q", "--bare", bare)
    repo = str(tmp_path / "repo"); _git("clone", "-q", bare, repo)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    os.makedirs(os.path.join(repo, "factory"))
    open(os.path.join(repo, "factory", "state.json"), "w").write("{}")
    _git("-C", repo, "add", "-A"); _git("-C", repo, "commit", "-qm", "init", env=env)
    _git("-C", repo, "push", "-q", "-u", "origin", "HEAD")
    return bare, repo


def test_persistence_commits_mirror_and_state_and_pushes(home, ok_engine, poster, clone, monkeypatch):
    bare, repo = clone
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    open(os.path.join(home, "mirror", "C_DEV.jsonl"), "w").write(json.dumps({"ts": "1.0", "text": "hello"}) + "\n")
    open(os.path.join(home, "state.json"), "w").write('{"tracks": {"t1": "building"}}')
    queue_event(home, "persist")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster, repo=repo)
    assert sup.run_once() is True
    log = subprocess.run(["git", "--git-dir", bare, "log", "--format=%s"], capture_output=True, text=True).stdout.split("\n")
    assert log[0] == "manager: turn 1"
    files = subprocess.run(["git", "--git-dir", bare, "ls-tree", "-r", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    assert "factory/log/C_DEV.jsonl" in files and "factory/state.json" in files
    assert "building" in subprocess.run(["git", "--git-dir", bare, "show", "HEAD:factory/state.json"], capture_output=True, text=True).stdout
    assert "factory/manager-memory/LEDGER.jsonl" in files and "factory/manager-memory/MANAGER-HANDOFF.md" in files
    # every turn appends a ledger line and mirrors the manager's reply, so every turn commits (b2)
    queue_event(home, "again")
    assert sup.run_once() is True
    log2 = subprocess.run(["git", "--git-dir", bare, "log", "--format=%s"], capture_output=True, text=True).stdout.split("\n")
    assert log2[:2] == ["manager: turn 2", "manager: turn 1"]
    ledger = subprocess.run(["git", "--git-dir", bare, "show", "HEAD:factory/manager-memory/LEDGER.jsonl"], capture_output=True, text=True).stdout
    assert [json.loads(l)["turn"] for l in ledger.splitlines() if l.strip()] == [1, 2]


def test_state_is_seeded_from_the_repo_when_home_has_none(home, ok_engine, poster, clone):
    bare, repo = clone
    open(os.path.join(repo, "factory", "state.json"), "w").write('{"tracks": {"seed": "x"}}')
    queue_event(home, "seed")
    S.Supervisor(home=home, engines=engines(ok_engine), poster=poster, repo=repo).run_once()
    assert json.load(open(os.path.join(home, "state.json")))["tracks"] == {"seed": "x"}


def test_no_repo_skips_persistence(home, ok_engine, poster):
    queue_event(home, "x")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    assert sup.run_once() is True and sup.persist(1) is False


def test_status_text_renders_from_fixtures(home):
    open(os.path.join(home, "state.json"), "w").write(json.dumps({"tracks": {"t1": {"stage": "building", "owner": "coder"}, "t2": "review"}}))
    S.append_jsonl(os.path.join(home, "logs", "turns.jsonl"), {"n": 1, "at": 1000.0, "engine": "claude-l", "events": ["a"], "duration_s": 2.0})
    queue_event(home, "pending one")
    text = S.status_text(home)
    assert "engine: claude-r2d2" in text and "last turn: 1970-01-01T00:16:40Z on claude-l (1 events" in text
    assert "queue: 1 pending" in text and "t1: building (coder)" in text and "t2: review" in text
