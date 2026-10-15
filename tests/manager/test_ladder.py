"""loops/b12.md: the manager's model floor. A temporary home, fake engines whose behaviour is chosen per
(account, model) by a table file, fake posters only: never a real engine, GitHub or Slack."""
import datetime as dt
import json
import os
import subprocess
import sys

import pytest

from conftest import S, queue_event

HYDRA = os.path.join(os.path.dirname(__file__), "..", "..", "manager", "hydra")
FABLE, ASTRA, OPUS = "claude-fable-5-1", "gpt-6-astra", "claude-opus-5-5"
LADDER = {FABLE, ASTRA, OPUS}

FAKE = r'''#!/usr/bin/env python3
import sys, os, json
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]
kind = %(kind)r
if kind == "codex":
    acc, model = "codex", argv[argv.index("-m") + 1]
else:
    acc = {"fake-claude-r2d2": "claude-r2d2", "fake-claude-l": "claude-l"}.get(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""), "?")
    model = argv[argv.index("--model") + 1]
msg = sys.stdin.read()
table = json.loads((D / "table.json").read_text())
behaviour = table.get(acc + "|" + model, "ok")
with open(D / "calls.jsonl", "a") as f:
    f.write(json.dumps({"acc": acc, "model": model, "argv": argv}) + "\n")
ERRORS = {"credits": "You're out of usage credits.", "usage_limit": "usage limit reached for this account",
          "auth": "HTTP 401 Unauthorized", "prompt_too_long": "Prompt is too long for this model",
          "rate_limit": "429 Too Many Requests", "other": "a plain runtime crash"}
if behaviour != "ok":
    print(ERRORS[behaviour], file=sys.stderr); sys.exit(1)
reply = "REPLY: ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none\n"
if kind == "codex":
    Path(argv[argv.index("-o") + 1]).write_text(reply)
else:
    print(reply)
'''


class Fakes:
    def __init__(self, d, table=None):
        self.d = d
        self.table(table or {})
        for kind in ("claude", "codex"):
            p = d / kind
            p.write_text(FAKE % {"dir": str(d), "kind": kind})
            p.chmod(0o755)

    def table(self, t):
        (self.d / "table.json").write_text(json.dumps(t))

    def engines(self):
        return {"claude-r2d2": {"bin": str(self.d / "claude"), "cred": "claude-r2d2.env"},
                "claude-l": {"bin": str(self.d / "claude"), "cred": "claude-l.env"},
                "codex": {"bin": str(self.d / "codex"), "cred": None}}

    def invoked(self):
        p = self.d / "calls.jsonl"
        return [(c["acc"], c["model"]) for c in map(json.loads, p.read_text().splitlines())] if p.exists() else []


class Poster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))


def setup(home, tmp_path, engine=None, config=None, table=None):
    with open(os.path.join(home, "engine"), "w") as f:
        f.write(json.dumps(engine or {"acc": "claude-r2d2", "model": FABLE}) + "\n")
    with open(os.path.join(home, "config.json"), "w") as f:
        f.write(json.dumps(dict({"dev_channel": "C_DEV"}, **(config or {}))))
    (tmp_path / "eng").mkdir()
    fk = Fakes(tmp_path / "eng", table)
    clock = [dt.datetime(2026, 10, 11, 19, tzinfo=dt.timezone.utc)]

    def tick():
        clock[0] += dt.timedelta(seconds=1)
        return clock[0]
    sup = S.Supervisor(home=home, engines=fk.engines(), poster=Poster(), buildlog_poster=Poster(), clock=tick)
    return fk, sup


def attempts(home):
    return S.read_jsonl(os.path.join(home, "logs", "attempts.jsonl"))


def no_downgrade(home, allowed=LADDER):
    assert all(a.get("model") in allowed for a in attempts(home)), attempts(home)


def policy_line(home):
    lines = [l for l in S.status_text(home).splitlines() if l.startswith("engine policy:")]
    assert len(lines) == 1
    return lines[0]


def hydra(home, *args):
    return subprocess.run([sys.executable, HYDRA, *args], env={**os.environ, "HYDRA_HOME": home},
                          capture_output=True, text=True, timeout=60)


ALL_OUT = {f"claude-r2d2|{FABLE}": "credits", f"claude-l|{FABLE}": "credits", f"codex|{ASTRA}": "usage_limit",
           f"claude-r2d2|{OPUS}": "credits", f"claude-l|{OPUS}": "credits"}


def test_default_ladder_helpers():
    ladder = S.manager_ladder({})
    assert [t["model"] for t in ladder] == [FABLE, ASTRA, OPUS] and S.floor_model(ladder) == OPUS
    assert list(ladder[0]["accounts"]) == ["claude-r2d2", "claude-l"] and list(ladder[1]["accounts"]) == ["codex"]
    emap = {"claude-r2d2": {"bin": "x"}, "claude-l": {"bin": "x"}, "codex": {"bin": "x"}}
    assert S.ladder_steps(ladder, emap) == [(1, "claude-r2d2", FABLE), (1, "claude-l", FABLE), (2, "codex", ASTRA),
                                            (3, "claude-r2d2", OPUS), (3, "claude-l", OPUS)]
    assert S.ladder_position({"acc": "claude-l", "model": OPUS}, ladder) == 3
    assert S.ladder_position({"acc": "codex", "model": OPUS}, ladder) is None
    with pytest.raises(S.BadEngine) as e:
        S.enforce_engine_policy({"acc": "claude-l", "model": "claude-sonnet-5"}, ladder)
    assert str(e.value) == ("model claude-sonnet-5 is not on the manager's allowlist (floor claude-opus-5-5); allowed: "
                            "claude-fable-5-1 on claude-r2d2, claude-l; gpt-6-astra on codex; "
                            "claude-opus-5-5 on claude-r2d2, claude-l")
    S.enforce_engine_policy({"acc": "codex", "model": ASTRA}, ladder)


def test_fable_out_lands_on_astra_then_opus(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "credits", f"claude-l|{FABLE}": "credits"})
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-r2d2", FABLE), ("claude-l", FABLE), ("codex", ASTRA)]
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "codex", "model": ASTRA}
    notices = [it["text"] for it in S.read_outbox(home) if "fallback started" in it["text"]]
    assert len(notices) == 1 and "tier 1/3 claude-r2d2 (claude-fable-5-1) unavailable" in notices[0] \
        and notices[0].endswith("now running tier 2/3 codex (gpt-6-astra)")
    assert "active tier 2/3 codex (gpt-6-astra)" in policy_line(home)
    fk.table(dict(ALL_OUT, **{f"claude-r2d2|{OPUS}": "ok"}))
    queue_event(home, "B")
    assert sup.run_once() is True
    # during the episode the rotation starts at the configured primary (tier 1), then walks toward the floor
    assert fk.invoked()[3:] == [("claude-r2d2", FABLE), ("claude-l", FABLE), ("codex", ASTRA), ("claude-r2d2", OPUS)]
    assert "active tier 3/3 claude-r2d2 (claude-opus-5-5)" in policy_line(home)
    no_downgrade(home)


def test_all_out_one_unavailable_line_and_recovery(home, tmp_path):
    fk, sup = setup(home, tmp_path, table=ALL_OUT)
    eid = queue_event(home, "A")
    assert sup.run_once() is True
    lines = [it for it in S.read_outbox(home) if "all allowed engines failed" in it["text"]]
    assert len(lines) == 1 and lines[0]["text"] == (
        f"manager unavailable: all allowed engines failed (tier 1 claude-r2d2 ({FABLE}): credits; "
        f"tier 1 claude-l ({FABLE}): credits; tier 2 codex ({ASTRA}): usage_limit; "
        f"tier 3 claude-r2d2 ({OPUS}): credits; tier 3 claude-l ({OPUS}): credits)")
    ep = json.load(open(os.path.join(home, "logs", "unavailable.json")))
    assert lines[0]["id"] == f"unavailable-{ep['episode_id']}" and eid not in S.handled_ids(home)
    assert not sup.poster.posted
    os.remove(os.path.join(home, "logs", "retry-after"))
    assert sup.run_once() is True
    assert len([it for it in S.read_outbox(home) if "all allowed engines failed" in it["text"]]) == 1
    fk.table({})
    os.remove(os.path.join(home, "logs", "retry-after"))
    assert sup.run_once() is True and eid in S.handled_ids(home)
    assert json.load(open(os.path.join(home, "logs", "unavailable.json")))["episode_id"] is None
    no_downgrade(home)


def test_stale_engine_file_starts_at_the_ladder(home, tmp_path):
    fk, sup = setup(home, tmp_path, engine={"acc": "claude-l", "model": "claude-sonnet-5"})
    assert "active tier -/3 (configured claude-l (claude-sonnet-5) is outside the ladder; the next turn starts at tier 1)" \
        in policy_line(home)
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-l", FABLE)]
    no_downgrade(home)


def test_rotation_from_a_selected_lower_tier(home, tmp_path):
    fk, sup = setup(home, tmp_path, engine={"acc": "claude-l", "model": OPUS},
                    table={f"claude-l|{OPUS}": "credits", f"claude-r2d2|{OPUS}": "credits"})
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-l", OPUS), ("claude-r2d2", OPUS), ("claude-r2d2", FABLE)]
    no_downgrade(home)


def test_prompt_too_long_skips_claude_family(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "prompt_too_long"})
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-r2d2", FABLE), ("codex", ASTRA)]


def test_legacy_claude_models_key_is_ignored(home, tmp_path):
    fk, sup = setup(home, tmp_path, config={"engine_fallback": {"claude_models": ["claude-sonnet-5"]}},
                    table={f"claude-r2d2|{FABLE}": "credits"})
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-r2d2", FABLE), ("claude-l", FABLE)]
    no_downgrade(home)


@pytest.mark.parametrize("reason", ["auth", "rate_limit", "other"])
def test_only_persist_worthy_failures_persist(home, tmp_path, reason):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": reason})
    before = open(os.path.join(home, "engine")).read()
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("claude-r2d2", FABLE), ("claude-l", FABLE)]
    notices = [it for it in S.read_outbox(home) if "fallback started" in it["text"]]
    if reason == "auth":
        assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": FABLE} and len(notices) == 1
    else:
        assert open(os.path.join(home, "engine")).read() == before and not notices


def test_custom_ladder_order_and_status(home, tmp_path):
    cfg = {"engine_policy": {"ladder": [{"model": "gpt6", "accounts": ["codex"]},
                                        {"model": "opus5.5", "accounts": ["claude-l"]}]}}
    fk, sup = setup(home, tmp_path, engine={"acc": "codex", "model": ASTRA}, config=cfg,
                    table={f"codex|{ASTRA}": "usage_limit"})
    assert policy_line(home).startswith("engine policy: floor claude-opus-5-5; active tier 1/2 codex (gpt-6-astra); "
                                        "ladder gpt-6-astra [codex] > claude-opus-5-5 [claude-l]")
    queue_event(home, "A")
    assert sup.run_once() is True
    assert fk.invoked() == [("codex", ASTRA), ("claude-l", OPUS)]
    r = hydra(home, "engine", "acc=claude-l", "model=fable5.1")
    assert r.returncode == 2 and "allowed: gpt-6-astra on codex; claude-opus-5-5 on claude-l; nothing changed" in r.stderr
    no_downgrade(home, {ASTRA, OPUS})


@pytest.mark.parametrize("policy,needle", [
    ({"ladder": [{"model": FABLE, "accounts": ["nope"]}]}, "nope"),
    ({"ladder": [{"model": ASTRA, "accounts": ["claude-l"]}]}, "family"),
    ({"ladder": [{"model": "claude-sonnet-5", "accounts": ["claude-l"]}]}, "claude-sonnet-5"),
    ({"ladder": []}, "empty"),
])
def test_invalid_policy_falls_back_to_the_default_ladder(home, tmp_path, policy, needle):
    setup(home, tmp_path, config={"engine_policy": policy})
    line = policy_line(home)
    assert "floor claude-opus-5-5; active tier 1/3 claude-r2d2 (claude-fable-5-1)" in line
    assert "(engine_policy ignored: " in line and needle in line.split("engine_policy ignored: ", 1)[1]
    assert len(line) <= 400


def test_explicit_reselection_closes_the_episode_with_tier_wording(home, tmp_path):
    fk, sup = setup(home, tmp_path, table={f"claude-r2d2|{FABLE}": "credits"})
    sup.run_engines("one", [])
    episode = S.read_engine_runtime(home)["episode_id"]
    r = hydra(home, "engine", "acc=claude-r2d2", "model=fable5.1")
    assert r.returncode == 0
    fk.table({})
    sup.run_engines("two", [])
    ended = [it["text"] for it in S.read_outbox(home) if "fallback ended" in it["text"]]
    assert ended == [f"fallback ended (episode {episode}): back on tier 1/3 claude-r2d2 ({FABLE})"]


def test_help_and_readme_name_the_policy():
    out = subprocess.run([sys.executable, HYDRA, "--help"], capture_output=True, text=True, timeout=60).stdout
    for alias in ("fable5.1", "opus5.5", "gpt6", "gpt6.1", "sol6.1"):
        assert alias in out
    assert "sonnet5" not in out and "haiku4.5" not in out and "coder" in out.lower()
    readme = open(os.path.join(os.path.dirname(HYDRA), "README.md")).read()
    for topic in ("floor", "engine_policy", "engine policy:", "all allowed engines failed", "gpt6.1", "sol6.1", "gpt-6.1-sol"):
        assert topic in readme
