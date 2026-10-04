"""requirement 13 (memory-writing rollover instead of /compact), loops/b7.md."""
import json
import os
from unittest.mock import patch

from conftest import S, engines, queue_event

HANDOFF = "---HANDOFF---\ntracks: t\nwaiting on: none\nlast decision: x\nnext action: none\nopen question: none"


def rollover_engine(dir_, memory_write=True, handoff=HANDOFF, fail=False):
    exe = os.path.join(dir_, "claude")
    body = ["#!/usr/bin/env python3", "import sys, os", "sys.stdin.read()",
            "if '/compact' in sys.argv: raise SystemExit('obsolete')"]
    if fail:
        body.append("sys.exit(1)")
    else:
        if memory_write:
            body += ["from pathlib import Path",
                     "m = Path(os.environ['HYDRA_MEMORY_DIR']) / 'MEMORY.md'",
                     "m.write_text(m.read_text() + '\\nnote\\n')"]
        body.append(f"print('compacted\\n' + {handoff!r})")
    open(exe, "w").write("\n".join(body) + "\n")
    os.chmod(exe, 0o755)
    return exe


def test_held_by_default_records_diagnostic_without_engine_call(home, tmp_path):
    exe = rollover_engine(str(tmp_path))
    sup = S.Supervisor(home=home, engines=engines(exe), poster=lambda *a: None)
    old = S.read_text(os.path.join(home, "session-id")).strip()
    sup.run_compaction("force")
    assert S.read_text(os.path.join(home, "session-id")).strip() == old
    assert "held" in sup.compaction_state()["last"]["error"].lower()


def test_rollover_enabled_replaces_session_id_and_preserves_old_transcript(home, tmp_path):
    exe = rollover_engine(str(tmp_path))
    S.write_text(os.path.join(home, "config.json"), json.dumps({"compaction": {"rollover_enabled": True}}))
    old = S.read_text(os.path.join(home, "session-id")).strip()
    transcript_dir = os.path.join(home, ".claude", "projects", home.replace("/", "-"))
    os.makedirs(transcript_dir, exist_ok=True)
    transcript = os.path.join(transcript_dir, f"{old}.jsonl")
    open(transcript, "wb").write(b"OLD SENTINEL\n")
    sup = S.Supervisor(home=home, engines=engines(exe), poster=lambda *a: None)
    sup.run_compaction("force")
    new = S.read_text(os.path.join(home, "session-id")).strip()
    assert new != old
    assert open(transcript, "rb").read() == b"OLD SENTINEL\n"
    rec = sup.compaction_state()["last"]
    assert rec["old_id"] == old and rec["new_id"] == new and rec["ok"] is None


def test_missing_handoff_retains_old_id(home, tmp_path):
    exe = rollover_engine(str(tmp_path), handoff="")
    S.write_text(os.path.join(home, "config.json"), json.dumps({"compaction": {"rollover_enabled": True}}))
    old = S.read_text(os.path.join(home, "session-id")).strip()
    sup = S.Supervisor(home=home, engines=engines(exe), poster=lambda *a: None)
    sup.run_compaction("force")
    assert S.read_text(os.path.join(home, "session-id")).strip() == old
    assert sup.compaction_state()["last"]["ok"] is False


def test_incomplete_handoff_is_rejected(home, tmp_path):
    exe = rollover_engine(str(tmp_path), handoff="---HANDOFF---\ntracks: only-one-line")
    S.write_text(os.path.join(home, "config.json"), json.dumps({"compaction": {"rollover_enabled": True}}))
    old = S.read_text(os.path.join(home, "session-id")).strip()
    sup = S.Supervisor(home=home, engines=engines(exe), poster=lambda *a: None)
    sup.run_compaction("force")
    assert S.read_text(os.path.join(home, "session-id")).strip() == old
    assert "five-line" in sup.compaction_state()["last"]["error"]


def test_identical_content_memory_rewrite_does_not_qualify(home, tmp_path):
    exe = rollover_engine(str(tmp_path), memory_write=False)  # no write at all: bytes unchanged
    S.write_text(os.path.join(home, "config.json"), json.dumps({"compaction": {"rollover_enabled": True}}))
    old = S.read_text(os.path.join(home, "session-id")).strip()
    sup = S.Supervisor(home=home, engines=engines(exe), poster=lambda *a: None)
    sup.run_compaction("force")
    assert S.read_text(os.path.join(home, "session-id")).strip() == old
    assert "did not change MEMORY.md" in sup.compaction_state()["last"]["error"]


def test_failed_memory_turn_retains_old_id_and_is_an_attempt_not_a_ledger_entry(home, tmp_path):
    exe = rollover_engine(str(tmp_path), fail=True)
    S.write_text(os.path.join(home, "config.json"), json.dumps({"compaction": {"rollover_enabled": True}}))
    old = S.read_text(os.path.join(home, "session-id")).strip()
    sup = S.Supervisor(home=home, engines=engines(exe), poster=lambda *a: None)
    sup.ensure_memory_layout()
    sup.run_compaction("force")
    assert S.read_text(os.path.join(home, "session-id")).strip() == old
    assert sup.compaction_state()["last"]["ok"] is False
    led = S.read_ledger(sup.memory_dir)
    assert not any(e.get("kind") == "compaction" for e in led)
    attempts = S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl"))
    assert attempts and attempts[-1]["success"] is False


def test_replace_session_id_is_atomic_and_never_touches_old_on_failure(home):
    sup = S.Supervisor(home=home, poster=lambda *a: None)
    path = os.path.join(home, "session-id")
    S.write_text(path, "ORIGINAL\n")
    real_replace = os.replace

    def crash(src, dst, *a, **kw):
        if os.path.abspath(dst) == os.path.abspath(path):
            raise OSError("fixture failure")
        return real_replace(src, dst, *a, **kw)
    with patch.object(S.os, "replace", side_effect=crash):
        try:
            sup.replace_session_id("NEW")
        except OSError:
            pass
        else:
            raise AssertionError("expected the injected failure to propagate")
    assert S.read_text(path).strip() == "ORIGINAL"


def test_verify_compaction_unknown_usage_stays_pending_until_measured(home, tmp_path):
    sup = S.Supervisor(home=home, poster=lambda *a: None)
    state = sup.compaction_state()
    state["verify"] = {"before_tokens": 500000, "at": "fixture", "reason": "tokens", "old_id": "a", "new_id": "b"}
    sup.save_compaction_state(state)
    rec = sup.verify_compaction(None)
    assert rec["ok"] is None
    assert sup.compaction_state()["verify"] is not None, "unknown usage must not consume the pending verification"
    rec2 = sup.verify_compaction(10)
    assert rec2["ok"] is True and rec2["old_id"] == "a" and rec2["new_id"] == "b"
    assert sup.compaction_state()["verify"] is None
