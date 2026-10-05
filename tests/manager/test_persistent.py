"""New b8 coverage (loops/b8.md): engine mode persistence/defaults, the identity contract, persistent_status
bookkeeping, the uncertain-disposition stop in run_engines, and the own-line handoff marker fix. The deep
process-lifecycle contracts (real stream-json child, attach, rollover, identity gating in production routing)
are exit-owned in loops/b8.fake.py/b8.identity.py/b8.mode.py/b8.rollover.py/b8.acceptance.py; this file covers
the pieces that are cheap and meaningful to check with plain pytest fixtures."""
import hashlib
import json
import os
import stat

from conftest import S, engines, fake_engine, make_home, queue_event


# ----------------------------------------------------------------------------------------------- engine mode

def test_mode_defaults_to_per_turn_and_is_lazy(home):
    """A legacy/missing mode never materializes a "mode" key (requirement 7, loops/b8.md): old call sites
    that compare the whole dict (`S.read_engine(...) == {"acc": ..., "model": ...}`) keep working exactly as
    before, while `pair["mode"]` still answers the per-turn default."""
    pair = S.read_engine(home)
    assert pair["mode"] == "per-turn"
    assert "mode" not in pair
    assert pair == {"acc": "claude-r2d2", "model": "claude-fable-5-1"}


def test_set_engine_mode_persists_and_survives_unrelated_changes(home):
    pair = S.set_engine(home, "claude-r2d2", "claude-sonnet-5", mode="persistent")
    assert pair["mode"] == "persistent"
    assert S.read_engine(home)["mode"] == "persistent"
    # An account/model-only change afterwards must not silently reset the mode back to per-turn.
    S.set_engine(home, "claude-l", "claude-sonnet-5")
    assert S.read_engine(home) == {"acc": "claude-l", "model": "claude-sonnet-5", "mode": "persistent"}
    # And an explicit mode-only change must not reset the account or the model (requirement 7).
    S.set_engine(home, "claude-l", "claude-sonnet-5", mode="per-turn")
    assert S.read_engine(home)["mode"] == "per-turn"


def test_parse_engine_command_mode(home):
    current = S.read_engine(home)
    pair = S.parse_engine_command("mode=persistent", current=current)
    assert pair == {"acc": "claude-r2d2", "model": "claude-fable-5-1", "mode": "persistent"}
    try:
        S.parse_engine_command("mode=sometimes", current=current)
        assert False, "an invalid mode must raise BadEngine"
    except S.BadEngine as e:
        assert "mode" in str(e)
    # mode= alone never resolves to the family default model when the account carries a different one.
    current = {"acc": "claude-l", "model": "claude-opus-5-5"}
    pair = S.parse_engine_command("mode=per-turn", current=current)
    assert pair == {"acc": "claude-l", "model": "claude-opus-5-5", "mode": "per-turn"}


def test_mode_suffix_only_once_explicitly_set(home):
    assert S.mode_suffix(S.read_engine(home)) == ""
    S.set_engine(home, "claude-r2d2", "claude-sonnet-5", mode="persistent")
    assert S.mode_suffix(S.read_engine(home)) == " [persistent]"


# ----------------------------------------------------------------------------------------------- identity contract

def test_credential_identity_default_allowed_without_a_configured_credential(home):
    """An engine spec with no `cred` (most test fixtures, Codex) has nothing to verify."""
    result = S.credential_identity(home, "claude-r2d2", {"bin": "x"})
    assert result == {"verified": True, "automatic_allowed": True, "account_id": None, "verified_at": None,
                      "mismatch": False}


def test_credential_identity_denies_missing_sidecar(tmp_path):
    home = str(tmp_path / "home")
    os.makedirs(os.path.join(home, "credentials"))
    cred = os.path.join(home, "credentials", "claude-l.env")
    with open(cred, "w") as f:
        f.write("CLAUDE_CODE_OAUTH_TOKEN=some-token\n")
    result = S.credential_identity(home, "claude-l", {"bin": "x", "cred": "claude-l.env"})
    assert result["verified"] is False and result["automatic_allowed"] is False


def test_credential_identity_verified_via_make_home_sidecar(home):
    """make_home() seeds a verified sidecar for both default accounts so every pre-b8 fallback/rotation test
    keeps exercising the same unrestricted behavior it always did."""
    result = S.credential_identity(home, "claude-r2d2", {"bin": "x", "cred": "claude-r2d2.env"})
    assert result["verified"] is True and result["automatic_allowed"] is True
    assert result["account_id"] == "fixture-claude-r2d2"


def test_observe_identity_default_is_a_noop(home):
    assert S.observe_identity(home, "claude-r2d2", {"bin": "x", "cred": "claude-r2d2.env"}) is None


# ----------------------------------------------------------------------------------------------- persistent status

def test_persistent_status_defaults_to_stopped(home):
    status = S.persistent_status(home)
    assert status["state"] == "stopped" and status["pid"] is None
    assert status["restarts"] == {"planned": 0, "unexpected": 0, "last": None}


def test_persistent_status_reports_dead_pid_truthfully(home):
    """A dead pid recorded in persisted metadata must never be reported as a healthy running process
    (requirement 9, loops/b8.md)."""
    S._save_persistent_status(home, pid=999999999, account="claude-r2d2", model="claude-sonnet-5",
                              started_at=0, turns_served=3)
    status = S.persistent_status(home)
    assert status["state"] == "stopped" and status["pid"] is None
    assert status["turns_served"] == 3, "metadata other than liveness is still reported"


def test_persistent_status_reports_parked_while_writer_held(home):
    S._save_persistent_status(home, pid=None, account="claude-r2d2", model="claude-sonnet-5")
    with S.acquire_writer(home, "someone@console"):
        assert S.persistent_status(home)["state"] == "parked"
    assert S.persistent_status(home)["state"] == "stopped"


def test_bump_restart_buckets_by_planned_vs_everything_else(home):
    S._bump_restart(home, "planned", "account switch")
    S._bump_restart(home, "crash", "boom")
    S._bump_restart(home, "timeout", "slow")
    restarts = S.persistent_status(home)["restarts"]
    assert restarts["planned"] == 1 and restarts["unexpected"] == 2
    assert restarts["last"] == {"category": "timeout", "reason": "slow"}


# ----------------------------------------------------------------------------------------------- uncertain disposition

def crash_engine(dir_):
    """Exits nonzero with no stdout/stderr at all: the uncertain, transport-level failure shape (PR45 8.1),
    distinct from a classifiable engine error."""
    p = os.path.join(dir_, "claude")
    with open(p, "w") as f:
        f.write("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nsys.exit(17)\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def test_run_engines_stops_on_an_uncertain_crash_without_trying_the_next_account(home, tmp_path):
    """requirement 1/8, loops/b8.md: a crash's disposition is uncertain (the request may have had a real
    side effect); run_engines must not silently replay it on a different configured account."""
    d = tmp_path / "crash"; d.mkdir()
    crash = crash_engine(str(d))
    ok_dir = tmp_path / "ok"; ok_dir.mkdir()
    ok = fake_engine(str(ok_dir), "ok")
    sup = S.Supervisor(home=home, engines=engines(crash, ok), poster=lambda *a: None)
    try:
        sup.run_engines("hello", [])
        assert False, "a transport crash must still raise AllEnginesFailed"
    except S.AllEnginesFailed as e:
        assert e.uncertain is True
    attempts = S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl"))
    assert len(attempts) == 1, "the second (healthy) account must never have been invoked"
    assert attempts[0]["success"] is False


def test_turn_marks_an_uncertain_failure_handled_without_retry(home, tmp_path):
    """The event layer gives an uncertain failure a durable disposition: it is not left pending for the
    ordinary retry-after backoff to resubmit later (requirement 9, PR45 8.1, loops/b8.md)."""
    d = tmp_path / "crash"; d.mkdir()
    crash = crash_engine(str(d))
    sup = S.Supervisor(home=home, engines={"claude-r2d2": {"bin": crash, "cred": "claude-r2d2.env"}},
                       poster=lambda *a: None)
    eid = queue_event(home, "hello")
    assert sup.run_once() is True
    assert eid in S.handled_ids(home), "an uncertain failure must be marked handled, not left pending"
    turns = S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))
    assert turns and turns[-1].get("error")


# ----------------------------------------------------------------------------------------------- handoff marker

def test_split_handoff_requires_its_own_line():
    """A mid-sentence mention of the marker text (e.g. the manager's own prompt instruction, echoed back by
    a fixture) must never be mistaken for the real handoff boundary (manager/CLAUDE.md: "a line containing
    only `---HANDOFF---`")."""
    text = "Please end with ---HANDOFF--- and the five lines.\n\nActual reply content."
    reply, handoff = S.split_handoff(text)
    assert handoff is None and reply == text.strip()


def test_split_handoff_still_matches_a_real_standalone_marker():
    text = "the actual reply\n---HANDOFF---\ntracks: t\nwaiting on: none\nlast decision: x\nnext action: y\nopen question: none"
    reply, handoff = S.split_handoff(text)
    assert reply == "the actual reply" and handoff.startswith("tracks: t")
