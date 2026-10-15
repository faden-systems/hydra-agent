"""loops/b12.md, founder decision of 2026-10-15 (same-tier stickiness, hourly re-probe, faults never move, the
configured/effective status line). Fake engines and posters only; the clock is a controllable fake."""
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import S, queue_event
from test_ladder import setup, FABLE, ASTRA, OPUS, ALL_OUT, no_downgrade, policy_line

STICKY_RUNTIME = {"configured": {"acc": "claude-r2d2", "model": FABLE}, "effective": {"acc": "claude-l", "model": FABLE},
                  "episode_id": "sticky-test", "since": "2026-10-11T18:00:00Z", "reason": "credits", "active": True}


def outbox(home, needle):
    return [it for it in S.read_outbox(home) if needle in (it.get("text") or "")]


def engine_line(home):
    return S.status_text(home).splitlines()[0]


def runtime(home):
    return json.loads(Path(S.engine_runtime_path(home)).read_text())


def test_r2d2_out_moves_to_l_and_the_next_turn_stays_on_l(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "credits"})
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-r2d2", FABLE), ("claude-l", FABLE)]
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": FABLE}
    rt = runtime(home)
    assert rt["configured"] == {"acc": "claude-r2d2", "model": FABLE} and rt["effective"] == {"acc": "claude-l", "model": FABLE}
    assert rt["active"] and "probe_after" not in rt, "a same-tier move schedules no probe"
    # rule 1: the next turn stays on the account that worked; the exhausted one is not called
    queue_event(home, "B")
    assert sup.run_once() is True
    assert fk.invoked()[2:] == [("claude-l", FABLE)]
    # rule 5: the operator's pair and the effective pair are shown separately; the runner matches the effective pair
    line = engine_line(home)
    assert line.startswith(f"engine: configured claude-r2d2 ({FABLE}) | effective claude-l ({FABLE}) | last turn ran on claude-l ({FABLE})")
    assert "MISMATCH" not in line
    assert "active tier 1/3 claude-l" in policy_line(home)
    no_downgrade(home)


def test_l_out_tries_r2d2_then_both_out_lands_on_astra_with_a_move_notice(home, tmp_path):
    fk, sup = setup(home, tmp_path, engine={"acc": "claude-l", "model": FABLE},
                    table={f"claude-r2d2|{FABLE}": "credits", f"claude-l|{FABLE}": "credits"})
    Path(S.engine_runtime_path(home)).write_text(json.dumps(STICKY_RUNTIME))
    queue_event(home, "A")
    assert sup.run_once() is True
    # rule 1/2: the sticky account first, the other Fable account before leaving the tier, then Astra
    assert fk.invoked() == [("claude-l", FABLE), ("claude-r2d2", FABLE), ("codex", ASTRA)]
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "codex", "model": ASTRA}
    moved = outbox(home, "fallback moved")
    assert len(moved) == 1 and moved[0]["thread_ts"] is None \
        and f"tier 1/3 claude-l ({FABLE}) unavailable" in moved[0]["text"] \
        and moved[0]["text"].endswith(f"now running tier 2/3 codex ({ASTRA})")
    rt = runtime(home)
    assert rt["effective"] == {"acc": "codex", "model": ASTRA} and rt["configured"] == {"acc": "claude-r2d2", "model": FABLE}
    # rule 3: landing below the configured tier schedules the re-probe one hour out
    due = S.parse_iso(rt["probe_after"]) - sup.clock.clock[0]
    assert dt.timedelta(seconds=S.TIER_PROBE_S - 10) <= due <= dt.timedelta(seconds=S.TIER_PROBE_S)
    assert engine_line(home).startswith(f"engine: configured claude-r2d2 ({FABLE}) | effective codex ({ASTRA}) | last turn ran on codex ({ASTRA})")
    no_downgrade(home)


def test_hourly_probe_from_astra_returns_to_fable(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "credits", f"claude-l|{FABLE}": "credits"})
    queue_event(home, "A")
    assert sup.run_once() is True and json.load(open(os.path.join(home, "engine")))["acc"] == "codex"
    # inside the hour: no Fable call at all, Astra serves
    queue_event(home, "B")
    assert sup.run_once() is True
    assert fk.invoked()[3:] == [("codex", ASTRA)]
    # the hour passes, Fable still out: one probe of both accounts, then Astra again; the next probe moves out an hour
    sup.clock.clock[0] += dt.timedelta(seconds=S.TIER_PROBE_S)
    queue_event(home, "C")
    assert sup.run_once() is True
    assert fk.invoked()[4:] == [("claude-r2d2", FABLE), ("claude-l", FABLE), ("codex", ASTRA)]
    after = runtime(home)["probe_after"]
    assert S.parse_iso(after) > sup.clock.clock[0] + dt.timedelta(seconds=S.TIER_PROBE_S - 60)
    queue_event(home, "D")
    assert sup.run_once() is True and fk.invoked()[7:] == [("codex", ASTRA)]
    # another hour, Fable is back on claude-l: the manager moves up, the move is announced, no probe is scheduled
    fk.table({f"claude-r2d2|{FABLE}": "credits"})
    sup.clock.clock[0] += dt.timedelta(seconds=S.TIER_PROBE_S)
    queue_event(home, "E")
    assert sup.run_once() is True
    assert fk.invoked()[8:] == [("claude-r2d2", FABLE), ("claude-l", FABLE)]
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": FABLE}
    rt = runtime(home)
    assert rt["active"] and rt["effective"] == {"acc": "claude-l", "model": FABLE} and "probe_after" not in rt
    moved = outbox(home, "fallback moved")
    assert len(moved) == 1 and f"tier 2/3 codex ({ASTRA}) unavailable" in moved[0]["text"] \
        and moved[0]["text"].endswith(f"now running tier 1/3 claude-l ({FABLE})")
    # r2d2 back as well: the configured pair serves, the episode ends and the engine file returns to it
    fk.table({})
    queue_event(home, "F")
    assert sup.run_once() is True
    assert fk.invoked()[10:] == [("claude-l", FABLE)], "sticky: the account that works keeps serving"
    S.set_engine(home, "claude-r2d2", FABLE, fk.engines())  # the operator re-selects the primary
    queue_event(home, "G")
    assert sup.run_once() is True and fk.invoked()[11:] == [("claude-r2d2", FABLE)]
    assert not runtime(home)["active"] and len(outbox(home, "fallback ended")) == 1
    no_downgrade(home)


def test_probe_returns_to_the_configured_pair_and_restores_the_engine_file(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "credits", f"claude-l|{FABLE}": "credits"})
    queue_event(home, "A")
    assert sup.run_once() is True and json.load(open(os.path.join(home, "engine")))["acc"] == "codex"
    fk.table({})
    sup.clock.clock[0] += dt.timedelta(seconds=S.TIER_PROBE_S)
    queue_event(home, "B")
    assert sup.run_once() is True
    assert fk.invoked()[3:] == [("claude-r2d2", FABLE)]
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-r2d2", "model": FABLE}
    rt = runtime(home)
    assert not rt["active"] and "probe_after" not in rt
    ended = outbox(home, "fallback ended")
    assert len(ended) == 1 and ended[0]["text"].endswith(f"back on tier 1/3 claude-r2d2 ({FABLE})")
    assert engine_line(home).startswith(f"engine: configured claude-r2d2 ({FABLE}) | last turn ran on claude-r2d2 ({FABLE})")
    queue_event(home, "C")
    assert sup.run_once() is True and fk.invoked()[4:] == [("claude-r2d2", FABLE)]
    no_downgrade(home)


def test_a_stated_reset_time_governs_the_probe(home, tmp_path):
    fk, sup = setup(home, tmp_path)
    reset = (sup.clock.clock[0] + dt.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = []

    def invoke(name, spec, message, model=None, preamble=""):
        seen.append((name, model))
        if name == "codex":
            return 0, "ok\n---HANDOFF---\ntracks: t\nwaiting on: n\nlast decision: n\nnext action: n\nopen question: n", "", None
        return 1, "", f"usage limit reached; resets {reset}", None
    sup.invoke = invoke
    queue_event(home, "A")
    assert sup.run_once() is True and seen == [("claude-r2d2", FABLE), ("claude-l", FABLE), ("codex", ASTRA)]
    assert runtime(home)["probe_after"] == reset, "the stated reset time, not a flat hour"
    sup.clock.clock[0] += dt.timedelta(minutes=10)
    queue_event(home, "B")
    assert sup.run_once() is True and seen[3:] == [("codex", ASTRA)]
    sup.clock.clock[0] += dt.timedelta(minutes=10)
    queue_event(home, "C")
    assert sup.run_once() is True and seen[4:6] == [("claude-r2d2", FABLE), ("claude-l", FABLE)]


@pytest.mark.parametrize("reason", ["other", "rate_limit"])
def test_session_error_on_l_changes_nothing_and_posts_one_fault(home, tmp_path, reason):
    fk, sup = setup(home, tmp_path, engine={"acc": "claude-l", "model": FABLE}, table={f"claude-l|{FABLE}": reason})
    rt_path = Path(S.engine_runtime_path(home))
    rt_path.write_text(json.dumps(STICKY_RUNTIME))
    before = Path(home, "engine").read_bytes(), rt_path.read_bytes()
    eid = queue_event(home, "A")
    assert sup.run_once() is True
    # rule 4: only the running engine is called; nothing moves; the event waits for the retry
    assert fk.invoked() == [("claude-l", FABLE)]
    assert (Path(home, "engine").read_bytes(), rt_path.read_bytes()) == before
    assert eid not in S.handled_ids(home) and os.path.exists(os.path.join(home, "logs", "retry-after"))
    assert not outbox(home, "all allowed engines failed") and not outbox(home, "fallback")
    faults = outbox(home, "engine fault: ")
    text = {"other": "other: a plain runtime crash", "rate_limit": "rate_limit"}[reason]
    assert len(faults) == 1 and faults[0]["thread_ts"] is None and faults[0]["channel"] == "C_DEV" \
        and faults[0]["text"] == f"engine fault: tier 1 claude-l ({FABLE}): {text}"
    fault = json.loads(Path(home, "logs", "fault.json").read_text())
    assert fault["episode_id"] and faults[0]["id"] == f"fault-{fault['episode_id']}" and fault["queued"]
    # the retry fails the same way: the same engine again, no second line
    os.remove(os.path.join(home, "logs", "retry-after"))
    assert sup.run_once() is True and fk.invoked()[1:] == [("claude-l", FABLE)] and len(outbox(home, "engine fault: ")) == 1
    # recovery serves the pending event and closes the fault episode silently
    fk.table({})
    os.remove(os.path.join(home, "logs", "retry-after"))
    assert sup.run_once() is True and eid in S.handled_ids(home)
    assert json.loads(Path(home, "logs", "fault.json").read_text())["episode_id"] is None
    assert len(outbox(home, "engine fault: ")) == 1
    # a later fault is a new episode with its own line
    fk.table({f"claude-l|{FABLE}": reason})
    queue_event(home, "B")
    assert sup.run_once() is True and len(outbox(home, "engine fault: ")) == 2
    no_downgrade(home)


@pytest.mark.parametrize("rc,what", [(17, "crash"), (124, "timeout")])
def test_crash_and_timeout_are_faults_with_a_durable_disposition(home, tmp_path, rc, what):
    fk, sup = setup(home, tmp_path)
    seen = []

    def invoke(name, spec, message, model=None, preamble=""):
        seen.append((name, model))
        return rc, "", "unstructured process failure", None
    sup.invoke = invoke
    eid = queue_event(home, "A")
    assert sup.run_once() is True
    assert seen == [("claude-r2d2", FABLE)], "an uncertain failure is never replayed on another step"
    assert eid in S.handled_ids(home), "b8: a crash/timeout keeps its durable disposition"
    faults = outbox(home, "engine fault: ")
    assert len(faults) == 1 and faults[0]["text"] == f"engine fault: tier 1 claude-r2d2 ({FABLE}): {what}"
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-r2d2", "model": FABLE}


def test_fault_notice_survives_a_restart_without_duplication(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "other"})
    queue_event(home, "A")
    assert sup.run_once() is True and len(outbox(home, "engine fault: ")) == 1
    fault = json.loads(Path(home, "logs", "fault.json").read_text())
    fault["queued"] = False  # a crash between writing the episode and queueing its line
    Path(home, "logs", "fault.json").write_text(json.dumps(fault))
    S.Supervisor(home=home, engines=fk.engines(), poster=lambda *a: None, buildlog_poster=lambda *a: None)
    assert len(outbox(home, "engine fault: ")) == 1, "the outbox id deduplicates the reconciled line"


def test_non_usage_failure_never_tries_the_next_engine(home, tmp_path):
    """Replaces test_engines.py::test_non_quota_failure_tries_next_without_persisting under founder rule 4."""
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "other"})
    eid = queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-r2d2", FABLE)] and eid not in S.handled_ids(home)
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-r2d2", "model": FABLE}


@pytest.mark.parametrize('active', [True, False])
def test_preserve_populated_runtime_with_policy_line(tmp_path, monkeypatch, active):
    """Replaces test_b13_status.py::test_preserve_populated_runtime: the b12 `engine policy:` line is the one
    further addition over the b13 baseline b91679a6."""
    from test_b13_status import setup as b13_setup
    b13_setup(tmp_path, mode='persistent')
    home = str(tmp_path)
    Path(S.engine_runtime_path(home)).write_text(json.dumps(dict(
        episode_id='test', active=active, configured=dict(acc='claude-r2d2', model='claude-fable-5-1', mode='persistent'),
        effective=dict(acc='codex', model='gpt-6-astra', mode='per-turn'), since='2026-10-14T22:00:00Z', reason='credits')))
    Path(S.persistent_status_path(home)).write_text(json.dumps(dict(pid=999999999, turns_served=4, restarts=dict(planned=1, unexpected=2, last=None))))
    file = tmp_path / 'baseline.py'
    file.write_bytes(subprocess.check_output(['git', 'show', 'b91679a6db14831b5e3effe257a4c13ee9db6a53:manager/supervisor.py']))
    spec = importlib.util.spec_from_file_location('baseline_b13', file)
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    monkeypatch.setattr(S, 'pid_alive', lambda _: False)
    monkeypatch.setattr(base, 'pid_alive', lambda _: False)
    added = ('engine:', 'engine policy:', 'last turn attempts:', 'off configured engine:', 'accounts:', 'engine fallback:')
    ours = [x for x in S.status_text(home).splitlines() if not x.startswith(added)]
    theirs = [x for x in base.status_text(home).splitlines() if not x.startswith(('engine:', 'engine fallback:'))]
    assert ours == theirs
    if active:
        assert 'since 2026-10-14 15:00:00 PDT' in S.status_text(home)
        assert S.status_text(home).splitlines()[0].startswith('engine: configured claude-r2d2 (claude-fable-5-1)')


# ---- review of build PR #77 (manager, 2026-10-15): six fixes, each with its regression


def test_probe_hold_survives_exhaustion_of_every_lower_step(home, tmp_path):
    """Founder rule 3 still forbids early Fable probes when the effective tier is exhausted."""
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "credits", f"claude-l|{FABLE}": "credits", f"codex|{ASTRA}": "usage_limit"})
    queue_event(home, "A")
    assert sup.run_once() is True
    before = runtime(home)
    count = len(fk.invoked())
    fk.table({f"claude-r2d2|{OPUS}": "credits", f"claude-l|{OPUS}": "credits"})
    eid = queue_event(home, "B")
    assert sup.run_once() is True
    assert fk.invoked()[count:] == [("claude-r2d2", OPUS), ("claude-l", OPUS)]
    assert eid not in S.handled_ids(home)
    assert runtime(home)["probe_after"] == before["probe_after"]
    assert runtime(home)["effective"] == before["effective"]
    no_downgrade(home)


def test_a_failing_effective_tier_never_postpones_the_configured_tiers_probe(home, tmp_path):
    """#77 F2: a usage failure of the effective tier (with a far reset time) while moving down to the floor keeps
    the pending probe of the configured tier; a stated reset can only bring the probe forward."""
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "credits", f"claude-l|{FABLE}": "credits"})
    queue_event(home, "A")
    assert sup.run_once() is True and json.load(open(os.path.join(home, "engine"))) == {"acc": "codex", "model": ASTRA}
    rt = runtime(home)
    pending = rt["probe_after"]
    rt["probe_after"] = S._iso(sup.clock.clock[0] + dt.timedelta(minutes=20))
    Path(S.engine_runtime_path(home)).write_text(json.dumps(rt))
    far = S._iso(sup.clock.clock[0] + dt.timedelta(hours=12))
    original = sup.invoke
    def invoke(name, spec, message, model, preamble=""):
        if name == "codex":
            return 1, "", f"usage limit reached; resets {far}", {}
        return original(name, spec, message, model, preamble=preamble)
    sup.invoke = invoke
    queue_event(home, "B")
    assert sup.run_once() is True
    assert fk.invoked()[3:] == [("claude-r2d2", OPUS)], fk.invoked()
    assert runtime(home)["probe_after"] == S._iso(sup.clock.clock[0] - dt.timedelta(seconds=1) + dt.timedelta(minutes=20)) or \
        S.parse_iso(runtime(home)["probe_after"]) <= sup.clock.clock[0] + dt.timedelta(minutes=20), runtime(home)
    assert S.parse_iso(runtime(home)["probe_after"]) < S.parse_iso(far)
    # a nearer stated reset of a tier above brings the probe forward
    soon = S._iso(sup.clock.clock[0] + dt.timedelta(minutes=5))
    fk.table({f"claude-r2d2|{OPUS}": "credits"})
    def invoke2(name, spec, message, model, preamble=""):
        if name == "codex":
            return 1, "", f"usage limit reached; resets {soon}", {}
        return original(name, spec, message, model, preamble=preamble)
    sup.invoke = invoke2
    rt = runtime(home); rt["probe_after"] = S._iso(sup.clock.clock[0] + dt.timedelta(minutes=50)); rt["effective"] = {"acc": "codex", "model": ASTRA}
    Path(S.engine_runtime_path(home)).write_text(json.dumps(rt))
    with open(os.path.join(home, "engine"), "w") as f:
        f.write(json.dumps({"acc": "codex", "model": ASTRA}) + "\n")
    queue_event(home, "C")
    assert sup.run_once() is True
    assert S.parse_iso(runtime(home)["probe_after"]) <= S.parse_iso(soon) + dt.timedelta(seconds=5), runtime(home)
    no_downgrade(home)


def test_the_serving_account_needs_no_verified_sidecar_on_a_probe_turn(home, tmp_path, monkeypatch):
    """#77 F4: on a probe-due turn the rotation starts at the configured tier, but the account that served (the
    engine file) and the operator's configured account still run without a verified identity sidecar."""
    fk, sup = setup(home, tmp_path, engine={"acc": "claude-l", "model": FABLE}, table={f"claude-r2d2|{FABLE}": "credits"})
    rt = dict(STICKY_RUNTIME, effective={"acc": "claude-l", "model": OPUS}, probe_after="2026-10-11T18:30:00Z")
    Path(S.engine_runtime_path(home)).write_text(json.dumps(rt))
    with open(os.path.join(home, "engine"), "w") as f:
        f.write(json.dumps({"acc": "claude-l", "model": OPUS}) + "\n")
    monkeypatch.setattr(S, "credential_identity", lambda h, name, spec, observed=None: {"automatic_allowed": False, "mismatch": False})
    queue_event(home, "A")
    assert sup.run_once() is True
    # r2d2 (configured) probed and out; l/fable is the other account of tier 1: unverified -> skipped; codex... no:
    # the serving account l runs when its own step comes (tier 3 l/opus after codex)
    # r2d2 (configured, unverified) is probed and out; l (the account that served, unverified) still runs: the
    # gate is per account, so its tier-1 step serves and the manager moves up
    assert fk.invoked() == [("claude-r2d2", FABLE), ("claude-l", FABLE)], fk.invoked()
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": FABLE}
    no_downgrade(home)


def test_a_move_up_after_skips_only_persists_and_announces(home, tmp_path, monkeypatch):
    """#77 F6 (a): a probe that wins on a higher tier after only skipped steps is a move up: engine file, move
    notice and probe bookkeeping all follow (founder rule 2), even though no persist-worthy failure happened."""
    fk, sup = setup(home, tmp_path, engine={"acc": "codex", "model": ASTRA})
    rt = dict(STICKY_RUNTIME, effective={"acc": "codex", "model": ASTRA}, probe_after="2026-10-11T18:30:00Z")
    Path(S.engine_runtime_path(home)).write_text(json.dumps(rt))
    real = S.credential_identity
    monkeypatch.setattr(S, "credential_identity", lambda h, name, spec, observed=None:
                        {"automatic_allowed": False, "mismatch": True} if name == "claude-r2d2" else real(h, name, spec, observed=observed))
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-l", FABLE)]
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": FABLE}
    rt = runtime(home)
    assert rt["active"] and rt["effective"] == {"acc": "claude-l", "model": FABLE} and "probe_after" not in rt
    assert len(outbox(home, "fallback moved")) == 1 and "MISMATCH" not in engine_line(home)
    no_downgrade(home)


def test_a_stale_engine_file_is_normalised_by_the_turn_that_serves(home, tmp_path):
    """#77 F6 (b): a pre-b12 engine file outside the ladder is never invoked (frozen) and, once the first ladder
    step on that account has served, the file names that step, so the status stops reading 'outside the ladder'."""
    fk, sup = setup(home, tmp_path, engine={"acc": "claude-l", "model": "claude-sonnet-5"})
    assert "outside the ladder" in policy_line(home)
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-l", FABLE)]
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": FABLE}
    assert "outside the ladder" not in policy_line(home) and "MISMATCH" not in engine_line(home)
    assert not S.read_engine_runtime(home).get("active"), "a normalisation is not a fallback episode"
    no_downgrade(home)


def test_attach_on_a_codex_engine_file_picks_a_claude_step_of_the_ladder_in_force(home, tmp_path):
    """#77 F5: `hydra attach` with a codex engine file attaches the first Claude step of the ladder in force, never
    a pair a narrowed engine_policy excludes; with no Claude step it refuses."""
    d = tmp_path / "att"; d.mkdir()
    script = d / "claude"
    script.write_text("#!/usr/bin/env python3\nimport sys, os, json\n"
                      f"open({str(d / 'calls.jsonl')!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'tok': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', '')}}) + '\\n')\n")
    script.chmod(0o755)
    tokens = {line.split("=", 1)[0]: line.split("=", 1)[1].strip() for name in ("claude-r2d2", "claude-l")
              for line in open(os.path.join(home, "credentials", f"{name}.env")) if line.startswith("CLAUDE_CODE_OAUTH_TOKEN=")}
    l_token = open(os.path.join(home, "credentials", "claude-l.env")).read().split("=", 1)[1].strip()
    cfg = {"dev_channel": "C_DEV", "engines": {"claude-r2d2": {"bin": str(script)}, "claude-l": {"bin": str(script)}},
           "engine_policy": {"ladder": [{"model": "gpt6", "accounts": ["codex"]}, {"model": "opus5.5", "accounts": ["claude-l"]}]}}
    with open(os.path.join(home, "config.json"), "w") as f:
        f.write(json.dumps(cfg))
    with open(os.path.join(home, "engine"), "w") as f:
        f.write(json.dumps({"acc": "codex", "model": ASTRA}) + "\n")
    hydra = os.path.join(os.path.dirname(S.__file__), "hydra")
    r = subprocess.run([sys.executable, hydra, "attach"], env={**os.environ, "HYDRA_HOME": home}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.stdout, r.stderr)
    call = json.loads((d / "calls.jsonl").read_text().strip().splitlines()[-1])
    assert call["argv"][call["argv"].index("--model") + 1] == OPUS and call["tok"] == l_token, call
    cfg["engine_policy"] = {"ladder": [{"model": "gpt6", "accounts": ["codex"]}]}
    with open(os.path.join(home, "config.json"), "w") as f:
        f.write(json.dumps(cfg))
    r = subprocess.run([sys.executable, hydra, "attach"], env={**os.environ, "HYDRA_HOME": home}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "no Claude step" in r.stderr, (r.stdout, r.stderr)
    assert len((d / "calls.jsonl").read_text().strip().splitlines()) == 1, "no process on a refused attach"


def test_a_stated_reset_time_may_say_at(home, tmp_path):
    """#77 F8: `resets at <ISO instant>` and `reset: <ISO instant>` are read like `resets <ISO instant>`."""
    fk, sup = setup(home, tmp_path)
    now = sup.clock.clock[0]
    at = S._iso(now + dt.timedelta(minutes=25))
    assert sup._next_tier_probe([f"usage limit reached; resets at {at}"]) == at
    assert sup._next_tier_probe([f"limit reset: {at}"]) == at
    assert sup._next_tier_probe(["resets 2026-10-11T19:30:00"]) == S._iso(sup.clock.clock[0] + dt.timedelta(seconds=S.TIER_PROBE_S)), "a zone-less instant is ignored"
