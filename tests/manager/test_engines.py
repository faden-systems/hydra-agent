"""Engine rotation on quota, the thread note, budgets, the engine contract, the codex fallback, the handoff."""
import json
import os

from conftest import S, calls, engine_acc, engines, fake_engine, queue_event


def test_claude_engine_contract(home, ok_engine, poster):
    queue_event(home, "hello")
    S.Supervisor(home=home, engines=engines(ok_engine), poster=poster).run_once()
    c = calls(os.path.dirname(ok_engine))[-1]
    argv = c["argv"]
    assert argv[argv.index("--resume") + 1] == "sess-test"
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert "-p" in argv and "--dangerously-skip-permissions" in argv
    assert c["env"]["CLAUDE_CONFIG_DIR"] == os.path.join(home, ".claude")
    assert c["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-claude-r2d2"
    assert not any(k.startswith("CLAUDE") and k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN") for k in c["env"]), \
        "only the config dir and the chosen token are passed"


def test_fresh_session_when_no_session_id(home, ok_engine, poster):
    os.remove(os.path.join(home, "session-id"))
    queue_event(home, "hello")
    S.Supervisor(home=home, engines=engines(ok_engine), poster=poster).run_once()
    argv = calls(os.path.dirname(ok_engine))[-1]["argv"]
    assert "--session-id" in argv and "--resume" not in argv
    sid = open(os.path.join(home, "session-id")).read().strip()
    assert argv[argv.index("--session-id") + 1] == sid and len(sid) >= 32


def test_absolute_cred_path_and_token_from_file(home, ok_engine, poster, tmp_path):
    cred = tmp_path / "elsewhere.env"
    cred.write_text("# comment\nexport CLAUDE_CODE_OAUTH_TOKEN='fake-abs'\n")
    queue_event(home, "hello")
    S.Supervisor(home=home, engines={"claude-r2d2": {"bin": ok_engine, "cred": str(cred)}}, poster=poster).run_once()
    assert calls(os.path.dirname(ok_engine))[-1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-abs"


def test_quota_rotates_persists_and_notes(home, quota_engine, ok_engine, poster):
    queue_event(home, "hello")
    sup = S.Supervisor(home=home, engines=engines(quota_engine, ok_engine), poster=poster)
    assert sup.run_once() is True
    assert "engine: claude-l (claude-fable-5-1)" in poster.texts and "handled 1 events" in poster.texts
    assert S.read_engine(home) == {"acc": "claude-l", "model": "claude-fable-5-1"}, "the switch is persisted as JSON"
    assert calls(os.path.dirname(ok_engine))[-1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-claude-l"
    # the next turn starts on claude-l and carries no note
    poster.posted.clear()
    queue_event(home, "again")
    assert sup.run_once() is True
    assert "engine:" not in poster.texts
    assert len(calls(os.path.dirname(quota_engine))) == 1, "the exhausted engine is not retried first"


def test_non_quota_failure_tries_next_without_persisting(home, ok_engine, poster, tmp_path):
    d = tmp_path / "crash"; d.mkdir()
    crash = fake_engine(str(d), "crash")
    queue_event(home, "hello")
    assert S.Supervisor(home=home, engines=engines(crash, ok_engine), poster=poster).run_once() is True
    assert "handled 1 events" in poster.texts and "engine: claude-l" in poster.texts
    assert engine_acc(home) == "claude-r2d2", "a crash is not a quota switch"


def test_codex_fallback_gets_handoff_and_state_on_stdin(home, quota_engine, poster, tmp_path):
    d = tmp_path / "cdx"; d.mkdir()
    cdx = fake_engine(str(d), "ok", name="codex")
    open(os.path.join(home, "MANAGER-HANDOFF.md"), "w").write("tracks: t9\nwaiting on: L\n")
    open(os.path.join(home, "state.json"), "w").write('{"tracks": {"t9": "review"}}')
    queue_event(home, "still there?")
    sup = S.Supervisor(home=home, engines=engines(quota_engine, quota_engine, cdx), poster=poster)
    assert sup.run_once() is True
    c = calls(str(d))[-1]
    assert c["argv"][:2] == ["exec", "--skip-git-repo-check"] or c["argv"][0] == "exec"
    assert "resume" not in c["argv"], "the first codex turn is fresh"
    assert "tracks: t9" in c["stdin"] and '"t9"' in c["stdin"] and "source: slack" in c["stdin"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in c["env"], "no Claude token reaches codex"
    assert "engine: codex (gpt-6-astra)" in poster.texts, "a Claude-only alias falls back to the codex default"
    assert S.read_engine(home) == {"acc": "codex", "model": "gpt-6-astra"}
    assert c["argv"][c["argv"].index("-m") + 1] == "gpt-6-astra"
    queue_event(home, "and now?")
    assert sup.run_once() is True
    c2 = calls(str(d))[-1]
    assert c2["argv"][:3] == ["exec", "resume", "--last"], "later codex turns resume the last session"


def test_all_engines_fail_keeps_events_and_notes_once(home, quota_engine, poster):
    eid = queue_event(home, "hello")
    sup = S.Supervisor(home=home, engines=engines(quota_engine, quota_engine), poster=poster)
    assert sup.run_once() is True
    assert eid not in S.handled_ids(home)
    turn = S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))[-1]
    assert "error" in turn and "claude-r2d2" in turn["error"]
    assert len(poster.posted) == 1 and "unavailable" in poster.posted[0][2]
    os.remove(os.path.join(home, "logs", "retry-after"))
    assert sup.run_once() is True
    assert len(poster.posted) == 1, "the unavailable note is posted at most once per hour per thread"
    assert not os.path.exists(os.path.join(home, "MANAGER-HANDOFF.md"))


def test_failed_turn_backs_off(home, quota_engine, poster):
    queue_event(home, "hello")
    sup = S.Supervisor(home=home, engines=engines(quota_engine, quota_engine), poster=poster)
    assert sup.run_once() is True
    assert sup.run_once() is False, "no immediate retry while retry-after is in the future"


def test_budget_skips_engine_with_one_note(home, ok_engine, poster):
    open(os.path.join(home, "budgets.json"), "w").write(json.dumps(
        {"turns_per_hour": 100, "claude_turns_per_day": {"claude-r2d2": 1, "claude-l": 100}}))
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    queue_event(home, "first")
    assert sup.run_once() is True
    assert "budget" not in poster.texts
    queue_event(home, "second")
    poster.posted.clear()
    assert sup.run_once() is True
    assert "budget: claude-r2d2" in poster.texts and "engine: claude-l" in poster.texts
    assert S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))[-1]["engine"] == "claude-l"


def test_turns_per_hour_budget(home, ok_engine, poster, tmp_path):
    open(os.path.join(home, "budgets.json"), "w").write(json.dumps({"turns_per_hour": 1}))
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    queue_event(home, "first"); sup.run_once()
    queue_event(home, "second"); sup.run_once()
    assert [t["engine"] for t in S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))] == ["claude-r2d2", "claude-l"]


def test_handoff_rewrite_and_reply_split(home, ok_engine, poster):
    queue_event(home, "hello")
    S.Supervisor(home=home, engines=engines(ok_engine), poster=poster).run_once()
    handoff = open(os.path.join(home, "MANAGER-HANDOFF.md")).read()
    assert handoff.startswith("updated_at: ") and handoff.splitlines()[1] == "engine: claude-r2d2"
    assert S.strip_handoff_header(handoff).startswith("tracks: t1") and handoff.rstrip().endswith("open question: none")
    assert "---HANDOFF---" not in poster.texts and "tracks: t1" not in poster.texts


def test_no_handoff_keeps_previous_file(home, poster, tmp_path):
    d = tmp_path / "nh"; d.mkdir()
    eng = fake_engine(str(d), "nohandoff")
    open(os.path.join(home, "MANAGER-HANDOFF.md"), "w").write("tracks: keep\n")
    queue_event(home, "hello")
    S.Supervisor(home=home, engines=engines(eng), poster=poster).run_once()
    assert open(os.path.join(home, "MANAGER-HANDOFF.md")).read() == "tracks: keep\n"
    assert "just a reply" in poster.texts


def test_json_output_is_parsed_for_reply_tokens_and_session(home, poster, tmp_path):
    d = tmp_path / "js"; d.mkdir()
    eng = fake_engine(str(d), "json")
    queue_event(home, "hello")
    S.Supervisor(home=home, engines=engines(eng), poster=poster).run_once()
    assert poster.posted[-1][2].strip() == "json reply"
    assert S.strip_handoff_header(open(os.path.join(home, "MANAGER-HANDOFF.md")).read()).strip() == "tracks: j1"
    turn = S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))[-1]
    assert turn["tokens"] == 12
    assert open(os.path.join(home, "session-id")).read().strip() == "sess-json"


def test_long_reply_goes_to_a_file_and_is_linked(home, poster, tmp_path):
    d = tmp_path / "long"; d.mkdir()
    eng = fake_engine(str(d), "long")
    queue_event(home, "hello")
    S.Supervisor(home=home, engines=engines(eng), poster=poster).run_once()
    text = poster.posted[-1][2]
    assert "line 0" in text and "line 59" not in text and "full reply (60 lines)" in text
    path = os.path.join(home, "logs", "replies", "turn-1.md")
    assert path in text and "line 59" in open(path).read()


def test_split_handoff():
    assert S.split_handoff("reply\n---HANDOFF---\ntracks: x\n") == ("reply", "tracks: x")
    assert S.split_handoff("no marker") == ("no marker", None)


def test_broadcast_mentions_are_stripped(home, poster, tmp_path):
    d = tmp_path / "bc"; d.mkdir()
    eng = fake_engine(str(d), "ok", reply="<!channel> hey @all and <!here|here> and mail me@example.com")
    queue_event(home, "hello")
    S.Supervisor(home=home, engines=engines(eng), poster=poster).run_once()
    assert poster.posted[-1][2].startswith("channel hey all and here and mail me@example.com")
    assert S.sanitize_reply("@channel now") == "channel now" and S.sanitize_reply("a@b.c") == "a@b.c"
