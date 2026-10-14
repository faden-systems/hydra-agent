"""loops/b11.md requirement 7 (s) and (t): the gated timer. A fake `gh`, temporary coder log directories, a fake
`systemctl`, a local health server, a scratch git checkout, a controlled clock; no engine call, ever."""
import datetime as dt
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from b11_helpers import (FADEN_REPO, HYDRA_REPO, Clock, S, calls, checks_log, coder_dir, event, fakes, home, make, pr,
                         sha, snapshot, timer_setup, turns)


def reasons(rec):
    return rec["reasons"]


def test_s_sources_wake_once_per_difference_and_never_for_the_excluded_inputs():
    h, d, gh, dog, coder, clock, sup, tick = timer_setup()
    try:
        rec, ev = tick()
        assert reasons(rec) == ["snapshot initialised"] and ev["source"] == "timer" and ev["payload"]["instructs"] is False
        rec, ev = tick(); assert not rec["woke"] and calls(d) == [], "no engine call, no wake without a difference"
        gh.repo(FADEN_REPO, head=sha("f2"), prs=[pr(261, sha("p1"), labels=("loop",), reviews=("APPROVED",), checks=("success",))])
        rec, ev = tick()
        assert reasons(rec) == [f"faden main {sha('f1')[:7]} -> {sha('f2')[:7]}"] and ev["payload"]["text"] == "timer: " + reasons(rec)[0]
        rec, _ = tick(); assert not rec["woke"]
        gh.repo(HYDRA_REPO, head=sha("h1"), prs=[pr(70, sha("q1"))])
        rec, _ = tick(); assert reasons(rec) == ["hydra-agent PR #70 opened"]
        cur = dict(head=sha("p1"), labels=("loop",), reviews=("APPROVED",), checks=("success",))
        for kw, clause in ((dict(head=sha("p2")), f"faden PR #261 head {sha('p1')[:7]} -> {sha('p2')[:7]}"),
                           (dict(updated="2026-10-08T01:00:00Z"), "faden PR #261 updated_at 2026-10-08T00:00:00Z -> 2026-10-08T01:00:00Z"),
                           (dict(labels=("loop", "approved")), "faden PR #261 labels loop -> approved,loop"),
                           (dict(comments=3), "faden PR #261 comments 0 -> 3"),
                           (dict(review_comments=2), "faden PR #261 comments 3 -> 5"),
                           (dict(reviews=("APPROVED", "CHANGES_REQUESTED")), "faden PR #261 reviews 1 APPROVED -> 2 CHANGES_REQUESTED"),
                           (dict(checks=("success", "failure")), "faden PR #261 checks success -> failure,success"),
                           (dict(checks=("success", None)), "faden PR #261 checks failure,success -> pending,success")):
            cur.update(kw)
            p = pr(261, **cur)
            gh.repo(FADEN_REPO, head=sha("f2"), prs=[p])
            rec, _ = tick(); assert reasons(rec) == [clause], (reasons(rec), clause)
            rec, _ = tick(); assert not rec["woke"]
        gh.repo(FADEN_REPO, head=sha("f2"), prs=[p], issues=[{"number": 58, "updated_at": "2026-10-08T12:30:00Z"},
                                                      {"number": 59, "updated_at": "2026-10-08T12:30:00Z", "pull_request": {"url": "u"}}])
        rec, _ = tick(); assert reasons(rec) == ["faden issue #58 updated"]
        rec, _ = tick(); assert not rec["woke"], "the cumulative issues map wakes once per update"
        gh.repo(FADEN_REPO, head=sha("f2"), prs=[], issues=[])
        rec, _ = tick(); assert reasons(rec) == ["faden PR #261 closed"]
        lists = [a[1] for a in gh.calls() if any(k in a[1] for k in ("/pulls?", "/issues?", "/reviews", "/check-runs"))]
        assert lists and all("per_page=100" in p for p in lists) and all(a[1].startswith(("repos/", "user")) for a in gh.calls())
        child = subprocess.Popen(["sleep", "300"])
        try:
            coder_dir(coder, "b99-logs", child.pid)
            rec, _ = tick(); assert reasons(rec) == ["coder b99-logs appeared"]
            os.utime(Path(coder, "b99-logs", "status.json"))
            rec, _ = tick(); assert not rec["woke"], "an mtime-only change is never a reason"
            Path(coder, "b99-logs", "status.json").write_text(json.dumps({"state": "done", "exit_code": 0}))
            rec, _ = tick(); assert "coder b99-logs status.json state running -> done" in reasons(rec)
            child.kill(); child.wait()
            rec, _ = tick(); assert reasons(rec) == [f"coder b99-logs launcher pid {child.pid} dead"]
            rec, _ = tick(); assert not rec["woke"]
            Path(coder, "b99-logs", "coder-exit.log").write_text("x\n")
            rec, _ = tick(); assert reasons(rec) == ["coder b99-logs new file coder-exit.log"]
            Path(coder, "b99-logs", "coder-exit.log").write_text("changed content\n")
            rec, _ = tick(); assert not rec["woke"], "exit file content is not compared"
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
        now_epoch = sup.clock_epoch()
        Path(h, "work-status.json").write_text(json.dumps({"turn": 1, "mode": "idle", "track": "ops", "deadline": int(now_epoch) - 10, "since": 1}))
        rec, _ = tick(); assert reasons(rec) == [f"work-status deadline {S.now_iso(int(now_epoch) - 10)} passed"]
        rec, _ = tick(); assert not rec["woke"], "the same deadline wakes once"
        Path(h, "work-status.json").write_text(json.dumps({"turn": 2, "mode": "waiting", "track": "ops", "who": "L", "since": 1}))
        clock.advance(36000)
        rec, _ = tick(); assert not rec["woke"], "a waiting record on L with no deadline never wakes"
        Path(h, "work-status.json").write_text(json.dumps({"turn": 3, "mode": "continue", "track": "b12", "deadline": 1}))
        rec, _ = tick(); assert not rec["woke"], "continue stays with the self-event scheduler"
        Path(h, "logs", "persistence.json").write_text(json.dumps({"status": "blocked", "error": "push failed", "pending_local_sha": "x"}))
        rec, _ = tick(); assert reasons(rec) == ["persistence status blocked"]
        rec, _ = tick(); assert not rec["woke"], "staying blocked is not a reason"
        Path(h, "logs", "persistence.json").write_text(json.dumps({"status": "synced", "error": None, "pending_local_sha": None}))
        rec, _ = tick(); assert not rec["woke"]
        dog.state.write_text("inactive\n")
        rec, _ = tick(); assert reasons(rec) == ["dogfood unit inactive"]
        dog.state.write_text("active\n")
        rec, _ = tick(); assert reasons(rec) == ["dogfood unit active"]
        dog.status.write_text("503")
        rec, _ = tick(); assert not rec["woke"], "a single non-200 answer does not wake"
        rec, _ = tick(); assert reasons(rec) == ["dogfood /health 503 twice"]
        rec, _ = tick(); assert not rec["woke"], "once per streak"
        dog.status.write_text("200")
        new_head = dog.commit("two")
        rec, _ = tick(); assert reasons(rec) == [f"dogfood sha {sha('')[:0]}{dog.git('rev-parse', 'HEAD~1')[:7]} -> {new_head[:7]}"]
        dog.git("branch", "scratch-not-a-pr")
        rec, _ = tick(); assert not rec["woke"], "a branch that is not an open PR is never compared"
        Path(h, "engine-runtime.json").write_text(json.dumps({"episode_id": "ep-1", "active": True}))
        rec, _ = tick(); assert reasons(rec) == ["fallback episode ep-1 started"]
        Path(h, "engine-runtime.json").write_text(json.dumps({"episode_id": "ep-1", "active": False}))
        rec, _ = tick(); assert reasons(rec) == ["fallback episode ep-1 ended"]
        Path(h, "state.json").write_text(json.dumps({"tracks": [{"id": "ops", "now": "changed"}]}))
        Path(h, "MANAGER-HANDOFF.md").write_text("tracks: changed\n")
        event(h, "a slack message", thread="3.3")
        rec, _ = tick(); assert not rec["woke"], "Slack and the manager's own files are never reasons"
        rows = checks_log(h)
        assert len(rows) == tick.n and all({"at", "woke", "reasons", "cost_ms"} <= set(r) for r in rows)
        line = next(l for l in S.status_text(h).splitlines() if l.startswith("timer: "))
        last_wake = [r for r in rows if r["woke"]][-1]
        assert line == f"timer: every 60s | last check {rows[-1]['at']} ({rows[-1]['cost_ms']} ms) | last wake {last_wake['at']}: fallback episode ep-1 ended", line
    finally:
        dog.close()


def test_s_failures_wake_once_on_the_third_and_the_issues_cursor_follows_successful_checks():
    h, d, gh, dog, coder, clock, sup, tick = timer_setup()
    try:
        first, _ = tick()
        gh.fail([f"repos/{FADEN_REPO}/"])
        gh.repo(HYDRA_REPO, head=sha("h2"))
        rec, _ = tick(); assert reasons(rec) == [f"hydra-agent main {sha('h1')[:7]} -> {sha('h2')[:7]}"], "the other repo is still read"
        rec, _ = tick(); assert not rec["woke"]
        rec, _ = tick(); assert reasons(rec) == [f"check failing: github:{FADEN_REPO}"]
        rec, _ = tick(); assert not rec["woke"], "the fourth failure does not wake again"
        snap = snapshot(h)
        assert snap["failures"][f"github:{FADEN_REPO}"] == 4 and snap["github"][FADEN_REPO]["head"] == sha("f1")
        gh.fail([])
        rec, _ = tick(); assert not rec["woke"] and not snapshot(h)["failures"].get(f"github:{FADEN_REPO}")
        sinces = gh.issue_sinces(FADEN_REPO)
        assert sinces[0] < first["at"] and all(x == first["at"] for x in sinces[1:]), sinces
        assert gh.issue_sinces(HYDRA_REPO)[-1] == checks_log(h)[-2]["at"], "the unaffected repo advances its own cursor"
        pers = Path(h, "logs", "persistence.json"); pers.write_text("{not json")
        tick(); tick()
        rec, _ = tick(); assert reasons(rec) == ["check failing: persistence"]
        pers.write_text(json.dumps({"status": "synced", "error": None, "pending_local_sha": None}))
        rec, _ = tick(); assert not rec["woke"] and not snapshot(h)["failures"].get("persistence")
        Path(coder, "not-a-dir-logs").write_text("a file where a directory is expected")
        rec, _ = tick(); assert not rec["woke"], "a stray file matching the glob is skipped"
        cfg = json.loads(Path(h, "config.json").read_text())
        cfg["timer"]["dogfood"]["systemctl_bin"] = str(Path(dog.d, "no-such-systemctl")); Path(h, "config.json").write_text(json.dumps(cfg))
        tick(); tick()
        rec, _ = tick(); assert reasons(rec) == ["check failing: dogfood"]
        assert snapshot(h)["dogfood"]["unit"] == "active", "the failing source keeps its last good entry"
    finally:
        dog.close()


def test_s_daily_digest_once_per_local_date_and_no_catch_up():
    h, d, gh, dog, coder, clock, sup, tick = timer_setup(start=dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc))  # 05:00 PDT
    try:
        rec, _ = tick(); assert "daily digest" not in reasons(rec)
        clock.advance((dt.datetime(2026, 10, 8, 13, 58, 0, tzinfo=dt.timezone.utc) - clock.now).total_seconds())
        rec, _ = tick(); assert not rec["woke"]
        clock.advance(120)
        rec, ev = tick(); assert reasons(rec) == ["daily digest"] and ev["payload"]["digest"] is True
        rec, _ = tick(); assert not rec["woke"]
        clock.advance(24 * 3600)
        rec, ev = tick(); assert reasons(rec) == ["daily digest"] and ev["payload"]["digest"] is True
    finally:
        dog.close()
    h, d, gh, dog, coder, clock, sup, tick = timer_setup(start=dt.datetime(2026, 10, 8, 20, 0, 0, tzinfo=dt.timezone.utc))  # 13:00 PDT
    try:
        rec, _ = tick(); assert "daily digest" not in reasons(rec)
        clock.advance(3600)
        rec, _ = tick(); assert not rec["woke"], "a snapshot initialised after digest_at never produces a catch-up digest"
    finally:
        dog.close()


def test_s_max_silence_only_while_something_is_in_flight_never_for_l_alone():
    h, d, gh, dog, coder, clock, sup, tick = timer_setup(start=dt.datetime(2026, 10, 8, 20, 0, 0, tzinfo=dt.timezone.utc))
    cfg = json.loads(Path(h, "config.json").read_text()); cfg["timer"]["max_silence_s"] = 1800
    Path(h, "config.json").write_text(json.dumps(cfg))
    try:
        tick()
        Path(h, "work-status.json").write_text(json.dumps({"turn": 1, "mode": "waiting", "track": "ops", "who": "L", "since": int(sup.clock_epoch())}))
        clock.advance(1920)
        rec, _ = tick(); assert not rec["woke"], "only L pending never wakes"
        child = subprocess.Popen(["sleep", "600"])
        try:
            coder_dir(coder, "b98-logs", child.pid)
            rec, _ = tick(); assert reasons(rec) == ["coder b98-logs appeared"]
            clock.advance(900); rec, _ = tick(); assert not rec["woke"]
            clock.advance(1200); rec, _ = tick()
            assert len(reasons(rec)) == 1 and reasons(rec)[0].startswith("max silence ") and reasons(rec)[0].endswith("; in flight: coder b98-logs")
            rec, _ = tick(); assert not rec["woke"]
            event(h, "a founder message", thread="4.4"); assert sup.run_once() is True
            clock.advance(900); rec, _ = tick(); assert not rec["woke"]
            clock.advance(1200); rec, _ = tick(); assert not rec["woke"], "the silence clock restarted at the check that observed the turn"
            clock.advance(700); rec, _ = tick(); assert reasons(rec)[0].startswith("max silence ")
            child.kill(); child.wait()
            rec, _ = tick(); assert reasons(rec) == [f"coder b98-logs launcher pid {child.pid} dead"]
            clock.advance(1920); rec, _ = tick(); assert not rec["woke"], "a dead run is not in flight"
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
        gh.repo(HYDRA_REPO, head=sha("h1"), prs=[pr(71, sha("m1"), login="hydra-manager")])
        rec, _ = tick(); assert reasons(rec) == ["hydra-agent PR #71 opened"]
        clock.advance(1920); rec, _ = tick(); assert reasons(rec)[0].endswith("; in flight: hydra-agent PR #71")
        gh.repo(HYDRA_REPO, head=sha("h1"), prs=[pr(72, sha("m2"), login="someone-else")])
        rec, _ = tick(); assert reasons(rec) == ["hydra-agent PR #71 closed", "hydra-agent PR #72 opened"]
        clock.advance(1920); rec, _ = tick(); assert not rec["woke"], "a PR by someone else is not the manager in flight"
        now_epoch = int(sup.clock_epoch())
        Path(h, "work-status.json").write_text(json.dumps({"turn": 2, "mode": "waiting", "track": "b12", "who": "Hermes", "deadline": now_epoch + 864000, "since": now_epoch}))
        rec, _ = tick(); assert not rec["woke"], "a new waiting record is not itself a reason"
        clock.advance(1920); rec, _ = tick(); assert reasons(rec)[0].endswith("; in flight: waiting on Hermes")
    finally:
        dog.close()


def test_s_service_loop_ticks_once_per_slot_not_while_paused_and_adopts_a_new_interval():
    h, d = home(), fakes(context=100000)
    coder = Path(h, "coder-scratch"); coder.mkdir()

    def configure(every_s):
        Path(h, "config.json").write_text(json.dumps({"timer": {"every_s": every_s, "github_repos": [], "coder_logs_glob": str(coder / "*-logs")}}))
    configure(60)
    clock = Clock(dt.datetime(2026, 10, 8, 15, 0, 0, tzinfo=dt.timezone.utc))
    stop = threading.Event()

    class Stop(BaseException):
        pass

    def fake_sleep(seconds):
        if stop.is_set():
            raise Stop()
        clock.advance(max(float(seconds), 1.0))
    sup = make(h, d, clock=clock, sleep=fake_sleep)
    ticks, real = [], sup.timer_tick

    def spy(now=None):
        ticks.append(sup.clock_epoch())
        return real(now)
    sup.timer_tick = spy
    errors = []

    def run():
        try:
            sup.loop(sleep_s=5)
        except Stop:
            pass
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))
    t = threading.Thread(target=run, daemon=True); t.start()

    def wait_clock(seconds):
        target = clock.now + dt.timedelta(seconds=seconds)
        deadline = time.time() + 120
        while clock.now < target and time.time() < deadline and t.is_alive():
            time.sleep(0.02)
        assert t.is_alive() and not errors and clock.now >= target, errors
    try:
        start = clock.now.timestamp()
        wait_clock(600)
        first = [ts for ts in ticks if ts <= start + 600]
        n = len(first)
        assert 9 <= n <= 12 and len({int(ts // 60) for ts in first}) == n, first
        Path(h, "PAUSE").write_text("test\n")
        paused_from = clock.now.timestamp()
        wait_clock(300)
        Path(h, "PAUSE").unlink()
        paused_to = clock.now.timestamp()
        assert not [ts for ts in ticks if paused_from + 1 < ts <= paused_to], "no timer_tick while paused"
        configure(120)
        second_from = clock.now.timestamp()
        wait_clock(600)
        second = [ts for ts in ticks if second_from < ts <= second_from + 600]
        assert 4 <= len(second) <= 6, second
        rows = checks_log(h)
        assert len(rows) == len(ticks) and [r["reasons"] for r in rows if r["woke"]] == [["snapshot initialised"]]
        assert len([r for r in turns(h) if r.get("events")]) == 1
    finally:
        stop.set(); t.join(10)


def test_t_timer_config_and_event_ids():
    assert not hasattr(S, "TIMER_EVERY_S") and not hasattr(S, "TIMER_TEXT")
    h = home()
    assert S.timer_config(h) == {**S.TIMER_DEFAULTS, "github_repos": list(S.TIMER_DEFAULTS["github_repos"])}
    sup = make(h, fakes())
    assert sup.add_timer_event(now=1000.0)["id"] == "timer-1" and sup.add_timer_event(now=1500.0) is None
    Path(h, "config.json").write_text(json.dumps({"timer": {"every_s": 120, "max_silence_s": 10}}))
    assert S.timer_config(h)["every_s"] == 120 and S.timer_config(h)["max_silence_s"] == 120
    assert sup.add_timer_event(now=1000000.0)["id"] == f"timer-120-{int(1000000.0 // 120)}"
    Path(h, "config.json").write_text(json.dumps({"timer": {"every_s": 5}}))
    assert S.timer_config(h)["every_s"] == 30
    Path(h, "config.json").write_text(json.dumps({"timer": {"every_s": 30}}))
    assert sup.add_timer_event(now=30.0)["id"] == "timer-30-1", "the same slot number under another interval is a distinct event"
    Path(h, "config.json").write_text(json.dumps({"timer": {"every_s": 900}}))
    assert sup.add_timer_event(now=1000.0) is None
    Path(h, "config.json").write_text("{not json")
    assert S.timer_config(h)["every_s"] == 900
    Path(h, "config.json").write_text(json.dumps({"compaction": {"over_ratio": 2.0}}))
    logged, orig = [], S.log
    S.log = lambda m: logged.append(str(m))
    try:
        sup2 = make(h, fakes(context=100000))
        for _ in range(3):
            sup2.compaction_config(); sup2.run_once()
    finally:
        S.log = orig
    assert len([m for m in logged if "over_ratio is retired" in m]) == 1
