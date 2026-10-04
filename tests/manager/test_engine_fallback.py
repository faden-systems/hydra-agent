"""requirement 11 (stdout parsing/classification) and requirement 12 (model fallback), loops/b7.md."""
import json
import os
from unittest.mock import patch

from conftest import S, engines, fake_engine, queue_event


# ----------------------------------------------------------------------------------------------- classify_engine_error

def test_classification_table():
    cases = [
        ("Out of usage credits. Switch to another model.", "credits"),
        ("usage limit reached for this account", "usage_limit"),
        ("HTTP 401 Unauthorized", "auth"),
        ("status 403 forbidden", "auth"),
        ("unauthorized", "auth"),
        ("Rate limit exceeded", "rate_limit"),
        ("429 Too Many Requests", "rate_limit"),
        ("Prompt is too long for this model", "prompt_too_long"),
        ("a plain runtime crash", "other"),
        ("record 14013 failed", "other"),
        ("generate a separate report", "other"),
    ]
    for text, expected in cases:
        assert S.classify_engine_error(text) == expected, text


def test_credits_takes_precedence_over_prompt_too_long():
    text = "Prompt is too long; automatic compaction failed: out of usage credits"
    assert S.classify_engine_error(text) == "credits"


def test_classification_is_phrase_not_substring():
    for text in ("generate a separate report failed", "this is unrelated", "a 4013 record"):
        assert S.classify_engine_error(text) == "other"


def test_classify_handles_none_and_empty():
    assert S.classify_engine_error(None) == "other"
    assert S.classify_engine_error("") == "other"


# ----------------------------------------------------------------------------------------------- stdout parsing on every rc

def test_nonzero_rc_is_failure_even_with_successful_looking_json(home, tmp_path):
    d = tmp_path / "e"; d.mkdir()
    exe = d / "claude"
    body = json.dumps({"type": "result", "result": "looks fine", "is_error": False})
    exe.write_text(f"#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint({body!r})\nsys.exit(1)\n")
    exe.chmod(0o755)
    sup = S.Supervisor(home=home, poster=lambda *a: None)
    rc, out, err, usage = sup.invoke("claude-r2d2", {"bin": str(exe), "cred": None}, "x", "claude-sonnet-5")
    assert rc != 0 and usage is None


def test_failure_reason_bounded_to_4000_chars(home, tmp_path):
    d = tmp_path / "e"; d.mkdir()
    exe = d / "claude"
    long_err = "x" * 5000
    exe.write_text(f"#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint('', file=sys.stderr)\n"
                   f"sys.stderr.write({long_err!r})\nsys.exit(1)\n")
    exe.chmod(0o755)
    sup = S.Supervisor(home=home, poster=lambda *a: None)
    rc, out, err, usage = sup.invoke("claude-r2d2", {"bin": str(exe), "cred": None}, "x", "claude-sonnet-5")
    assert rc != 0 and len(err) <= 4000


def test_jsonl_stream_with_terminal_error_record(home, tmp_path):
    d = tmp_path / "e"; d.mkdir()
    exe = d / "claude"
    first = json.dumps({"type": "assistant", "message": {"id": "m1", "usage": {"input_tokens": 10}}})
    result = json.dumps({"type": "result", "is_error": True, "error": {"message": "usage limit reached"},
                         "session_id": "bad"})
    exe.write_text("#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\n"
                   f"print({first!r})\nprint({result!r})\nsys.exit(1)\n")
    exe.chmod(0o755)
    sup = S.Supervisor(home=home, poster=lambda *a: None)
    S.write_text(os.path.join(home, "session-id"), "original\n")
    rc, out, err, usage = sup.invoke("claude-r2d2", {"bin": str(exe), "cred": None}, "x", "claude-sonnet-5")
    assert rc != 0 and "usage limit reached" in err
    assert S.read_text(os.path.join(home, "session-id")).strip() == "original", "a failed result never saves a session id"


# ----------------------------------------------------------------------------------------------- model fallback (requirement 12)

def test_same_account_non_fable_retry_before_next_account(home, tmp_path):
    S.set_engine(home, "claude-r2d2", "claude-fable-5-1", {"claude-r2d2": {}, "claude-l": {}})
    cfg = {"engine_fallback": {"claude_models": ["claude-sonnet-5"]}}
    eng_map = {"claude-r2d2": {"bin": "fixture"}, "claude-l": {"bin": "fixture"}}
    sup = S.Supervisor(home=home, engines=eng_map, config=cfg, poster=lambda *a: None)
    calls = []

    def invoke(name, spec, message, model=None, preamble=""):
        calls.append((name, model))
        if model == "claude-sonnet-5":
            return 0, "ok\n" + HANDOFF, "", {"context_tokens": 5}
        return 1, "", "Out of usage credits. Switch model.", None
    with patch.object(sup, "invoke", side_effect=invoke):
        name, model, *_ = sup.run_engines("hi", [])
    assert calls == [("claude-r2d2", "claude-fable-5-1"), ("claude-r2d2", "claude-sonnet-5")]
    assert (name, model) == ("claude-r2d2", "claude-sonnet-5")
    assert S.read_engine(home, eng_map) == {"acc": "claude-r2d2", "model": "claude-sonnet-5"}


def test_cross_family_fallback_to_codex_when_claude_exhausted(home):
    eng_map = {"claude-r2d2": {"bin": "fixture"}, "codex": {"bin": "fixture", "kind": "codex"}}
    S.set_engine(home, "claude-r2d2", "claude-sonnet-5", eng_map)
    cfg = {"engine_fallback": {"claude_models": []}}
    sup = S.Supervisor(home=home, engines=eng_map, config=cfg, poster=lambda *a: None)

    def invoke(name, spec, message, model=None, preamble=""):
        if name == "codex":
            return 0, "ok", "", None
        return 1, "", "Out of usage credits", None
    with patch.object(sup, "invoke", side_effect=invoke):
        name, *_ = sup.run_engines("hi", [])
    assert name == "codex"
    assert S.read_engine(home, eng_map)["acc"] == "codex", "a credits failure persists the fallback pair"


def test_auth_error_skips_model_retry(home):
    eng_map = {"claude-r2d2": {"bin": "fixture"}, "codex": {"bin": "fixture", "kind": "codex"}}
    cfg = {"engine_fallback": {"claude_models": ["claude-sonnet-5"]}}
    sup = S.Supervisor(home=home, engines=eng_map, config=cfg, poster=lambda *a: None)
    seen = []

    def invoke(name, spec, message, model=None, preamble=""):
        seen.append((name, model))
        return (0, "ok", "", None) if name == "codex" else (1, "", "HTTP 401 Unauthorized", None)
    with patch.object(sup, "invoke", side_effect=invoke):
        sup.run_engines("hi", [])
    assert seen == [("claude-r2d2", "claude-fable-5-1"), ("codex", "gpt-6-astra")], seen


HANDOFF = "---HANDOFF---\ntracks: t\nwaiting on: none\nlast decision: x\nnext action: none\nopen question: none"


def test_attempts_jsonl_records_every_attempt(home):
    eng_map = {"claude-r2d2": {"bin": "fixture"}, "codex": {"bin": "fixture", "kind": "codex"}}
    cfg = {"engine_fallback": {"claude_models": []}}
    sup = S.Supervisor(home=home, engines=eng_map, config=cfg, poster=lambda *a: None)

    def invoke(name, spec, message, model=None, preamble=""):
        return (0, "ok\n" + HANDOFF, "", {"context_tokens": 1}) if name == "codex" else (1, "", "Out of usage credits", None)
    with patch.object(sup, "invoke", side_effect=invoke):
        sup.run_engines("hi", [])
    attempts = S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl"))
    assert len(attempts) == 2
    assert attempts[0]["success"] is False and attempts[0]["classification"] == "credits"
    assert attempts[1]["success"] is True and attempts[1]["engine"] == "codex"


def test_fallback_notice_persists_across_restart_without_duplication(home):
    eng_map = {"claude-r2d2": {"bin": "fixture"}, "codex": {"bin": "fixture", "kind": "codex"}}
    cfg = {"dev_channel": "C_DEV_FIXTURE", "engine_fallback": {"claude_models": []}}
    sup = S.Supervisor(home=home, engines=eng_map, config=cfg, poster=lambda *a: None)

    def invoke(name, spec, message, model=None, preamble=""):
        return (0, "ok", "", None) if name == "codex" else (1, "", "Out of usage credits", None)
    with patch.object(sup, "invoke", side_effect=invoke):
        sup.run_engines("first", [])
    notices = S.read_outbox(home)
    assert len(notices) == 1 and "fallback started" in notices[0]["text"].lower()
    assert notices[0]["channel"] == "C_DEV_FIXTURE" and not notices[0].get("thread_ts")
    # a restart (fresh Supervisor) running the same failing pattern must not queue a second start notice
    sup2 = S.Supervisor(home=home, engines=eng_map, config=cfg, poster=lambda *a: None)
    with patch.object(sup2, "invoke", side_effect=invoke):
        sup2.run_engines("second", [])
    assert len(S.read_outbox(home)) == 1


def test_deliberately_selecting_codex_is_not_a_fallback_episode(home):
    eng_map = {"claude-r2d2": {"bin": "fixture"}, "codex": {"bin": "fixture", "kind": "codex"}}
    S.set_engine(home, "codex", "gpt-6-astra", eng_map)
    sup = S.Supervisor(home=home, engines=eng_map, poster=lambda *a: None)
    with patch.object(sup, "invoke", return_value=(0, "ok", "", None)):
        sup.run_engines("x", [])
    assert not S.read_outbox(home)
