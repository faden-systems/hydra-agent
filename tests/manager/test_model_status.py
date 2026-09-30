"""Model/account selection is shared by status, invocation and immutable turn logs."""
import json
import os

import pytest

from conftest import B, S, calls, engines, queue_event
from test_cli import run


@pytest.fixture(autouse=True)
def codex_home(tmp_path, monkeypatch):
    p = tmp_path / "codex-home"
    p.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(p))
    return p


@pytest.mark.parametrize("name,account", [("claude-r2d2", "Claude account R2D2"),
                                          ("claude-l", "Claude account L")])
def test_cli_and_bridge_status(home, poster, name, account):
    S.set_engine(home, name)
    expected = f"model: {name} = claude-fable-5-1 via {account}"
    assert expected in run(home, "status").stdout.splitlines()
    bridge = B.Bridge(home=home, poster=poster, token_env={}, bot_user_id="U_MANAGER",
                      allowlist={"U_FOUNDER": {"instructs": True}})
    bridge.handle_message({"text": "<@U_MANAGER> status", "user": "U_FOUNDER",
                           "channel": "C_DEV", "ts": "1.0"})
    assert expected in poster.posted[-1][2].splitlines()


@pytest.mark.parametrize("config,expected", [
    ('model = "configured-model"\n', "configured-model"),
    ('model = "base-model"\nprofile = "work"\n[profiles.work]\nmodel = "profile-model"\n', "profile-model"),
    ('', "installed-default"),
])
def test_codex_config_or_installed_catalog(home, codex_home, config, expected):
    (codex_home / "config.toml").write_text(config)
    (codex_home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "hidden", "priority": 0, "visibility": "hide"},
        {"slug": "later", "priority": 4, "visibility": "list"},
        {"slug": "installed-default", "priority": 1, "visibility": "list"}]}))
    S.set_engine(home, "codex")
    assert f"model: codex = {expected} via ChatGPT Pro" in run(home, "status").stdout.splitlines()


def test_unknown_codex_does_not_invent_model(home):
    S.set_engine(home, "codex")
    assert "model: codex = unknown via ChatGPT Pro" in run(home, "status").stdout


@pytest.mark.parametrize("name", ["claude-r2d2", "codex"])
def test_override_drives_invocation_and_captured_history(home, ok_engine, poster, name):
    cfg = {"engines": {name: {"bin": ok_engine, "model": "chosen-model", "account": "chosen-account"}}}
    S.write_text(os.path.join(home, "config.json"), json.dumps(cfg))
    S.set_engine(home, name)
    sup = S.Supervisor(home=home, poster=poster)
    for _ in range(2):
        queue_event(home, "hello")
        assert sup.run_once()
        argv = calls(os.path.dirname(ok_engine))[-1]["argv"]
        assert argv[argv.index("--model") + 1] == "chosen-model"
    turn = S.last_turn(home)
    assert (turn.get("model"), turn.get("account")) == ("chosen-model", "chosen-account")
    cfg["engines"][name].update(model="new-model", account="new-account")
    S.write_text(os.path.join(home, "config.json"), json.dumps(cfg))
    assert f"model: {name} = chosen-model via chosen-account" in run(home, "logs").stdout.splitlines()
    assert "new-model" not in run(home, "logs").stdout


def test_fallback_logs_successful_engine_identity(home, quota_engine, ok_engine, poster):
    specs = engines(quota_engine, ok_engine)
    queue_event(home, "hello")
    assert S.Supervisor(home=home, engines=specs, poster=poster).run_once()
    turn = S.last_turn(home)
    assert (turn.get("engine"), turn.get("model"), turn.get("account")) == (
        "claude-l", "claude-fable-5-1", "Claude account L")


def test_failed_turn_captures_identity(home, quota_engine, poster):
    queue_event(home, "hello")
    assert S.Supervisor(home=home, engines=engines(quota_engine), poster=poster).run_once()
    turn = S.last_turn(home)
    assert turn.get("model") == "claude-fable-5-1"
    assert turn.get("account") == "Claude account R2D2"


def test_codex_selection_refreshes_between_turns(home, codex_home, ok_engine, poster):
    (codex_home / "config.toml").write_text('model = "before"\n')
    S.write_text(os.path.join(home, "config.json"), json.dumps({"engines": {"codex": {"bin": ok_engine}}}))
    S.set_engine(home, "codex")
    sup = S.Supervisor(home=home, poster=poster)
    for model in ("before", "after"):
        (codex_home / "config.toml").write_text(f'model = "{model}"\n')
        queue_event(home, "hello")
        assert sup.run_once()
        argv = calls(os.path.dirname(ok_engine))[-1]["argv"]
        assert argv[argv.index("--model") + 1] == model
        assert S.last_turn(home)["model"] == model
    assert [t["model"] for t in sup.turns()] == ["before", "after"]


def test_legacy_logs_are_explicitly_unknown_not_reconstructed(home):
    S.append_jsonl(os.path.join(home, "logs", "turns.jsonl"),
                   {"n": 1, "engine": "claude-l", "at": 1000})
    assert "model: claude-l = unknown via unknown" in run(home, "logs").stdout.splitlines()
