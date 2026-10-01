"""The bridge with a fake client: allowlist, instructs tag, files, mirror, the commands, threads, bots.
Since loops/b4.md a message without a mention is queued only in a thread the manager has joined (see test_threads.py)."""
import json
import os

import pytest

from conftest import B, S, FakePoster


def read_queue(home):
    return S.read_jsonl(os.path.join(home, "inbox", "events.jsonl"))


def msg(text, user="U_FOUNDER", ts="10.0", thread=None, **extra):
    ev = {"channel": "C_DEV", "user": user, "text": text, "ts": ts}
    if thread:
        ev["thread_ts"] = thread
    ev.update(extra)
    return ev


@pytest.fixture
def bridge(home, poster):
    return B.Bridge(home=home, allowlist={"U_FOUNDER": {"instructs": True}, "U_OPERATOR": {"instructs": False},
                                          "B_CODER": {"instructs": False}},
                    poster=poster, token_env={}, bot_user_id="U_MANAGER", downloader=lambda url, dest: open(dest, "w").write(url))


def test_founder_message_is_queued_with_instructs(bridge, home):
    bridge.note_own_post("C_DEV", "1.0")  # a thread the manager is part of
    bridge.handle_message(msg("please launch b2", ts="10.1", thread="1.0"))
    q = read_queue(home)
    assert q[-1]["id"] == "10.1" and q[-1]["source"] == "slack"
    p = q[-1]["payload"]
    assert p["instructs"] is True and p["user"] == "U_FOUNDER" and p["channel"] == "C_DEV" and p["thread_ts"] == "1.0"
    assert p["addressed"] is False


def test_operator_is_information(bridge, home):
    bridge.note_own_post("C_DEV", "1.0")
    bridge.handle_message(msg("done: loop y", user="U_OPERATOR", ts="10.2", thread="1.0"))
    p = read_queue(home)[-1]["payload"]
    assert p["instructs"] is False


def test_top_level_message_starts_its_own_thread(bridge, home):
    bridge.handle_message(msg("<@U_MANAGER> top level", ts="10.3"))
    assert read_queue(home)[-1]["payload"]["thread_ts"] == "10.3"


def test_stranger_is_mirrored_not_queued(bridge, home):
    bridge.handle_message(msg("launch everything", user="U_STRANGER", ts="10.4"))
    assert not any(e["id"] == "10.4" for e in read_queue(home))
    mirror = S.read_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"))
    assert mirror[-1]["text"] == "launch everything" and mirror[-1]["user"] == "U_STRANGER"


def test_bot_not_addressed_is_ignored_but_addressed_bot_is_queued(bridge, home, poster):
    bridge.handle_message(msg("PR opened", user="U_X", bot_id="B_CODER", ts="10.5"))
    assert not any(e["id"] == "10.5" for e in read_queue(home))
    bridge.handle_message(msg("<@U_MANAGER> PR ready for review", user="U_X", bot_id="B_CODER", ts="10.6"))
    assert any(e["id"] == "10.6" and e["payload"]["instructs"] is False for e in read_queue(home))
    bridge.handle_message(msg("<@U_MANAGER> hi", user="U_Y", bot_id="B_UNKNOWN", ts="10.7"))
    assert not any(e["id"] == "10.7" for e in read_queue(home)), "an unknown bot is never queued, even when addressing us"
    assert poster.posted == [], "no reply to a bot unless it addresses us with a command"


def test_own_messages_are_never_queued(bridge, home):
    bridge.handle_message(msg("my own reply", user="U_MANAGER", ts="10.8"))
    assert not any(e["id"] == "10.8" for e in read_queue(home))


def test_mention_of_another_bot_is_not_addressing_us(bridge, home, poster):
    bridge.note_own_post("C_DEV", "1.0")
    bridge.handle_message(msg("<@U_CODER> status", ts="10.9", thread="1.0"))
    assert poster.posted == []
    assert read_queue(home)[-1]["payload"]["addressed"] is False


def test_edits_are_skipped(bridge, home):
    bridge.handle_message(msg("edited", ts="11.0", subtype="message_changed"))
    assert not any(e["id"] == "11.0" for e in read_queue(home))


def test_files_are_downloaded_and_pathed(bridge, home):
    bridge.handle_message(msg("<@U_MANAGER> see attached", ts="11.1", files=[{"id": "F1", "name": "shot.png", "url_private_download": "https://x/shot.png"}]))
    p = read_queue(home)[-1]["payload"]
    path = os.path.join(home, "inbox", "files", "11.1-shot.png")
    assert p["files"][0]["path"] == path and open(path).read() == "https://x/shot.png"
    assert path in S.event_header(read_queue(home)[-1])
    mirror = S.read_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"))[-1]
    assert mirror["files"] == [path]


def test_file_shared_event_uses_file_info(bridge, home):
    files = bridge.handle_file({"channel_id": "C_DEV", "file_id": "F2", "user_id": "U_FOUNDER", "event_ts": "11.2"},
                               file_info=lambda fid: {"id": fid, "name": "notes.md", "url_private": "https://x/notes.md"})
    assert files[0]["path"].endswith("11.2-notes.md")


def test_reaction_is_mirrored_only(bridge, home):
    bridge.handle_reaction({"user": "U_FOUNDER", "reaction": "white_check_mark", "item": {"channel": "C_DEV", "ts": "1.0"}, "event_ts": "11.3"})
    mirror = S.read_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"))[-1]
    assert mirror["type"] == "reaction" and mirror["reaction"] == "white_check_mark"
    assert not any(e["id"] == "11.3" for e in read_queue(home))


def test_status_for_any_allowlisted_sender(bridge, home, poster):
    open(os.path.join(home, "state.json"), "w").write(json.dumps({"tracks": {"t1": "building"}}))
    bridge.handle_message(msg("<@U_MANAGER> status", user="U_OPERATOR", ts="12.0", thread="1.0"))
    assert poster.posted[-1][:2] == ("C_DEV", "1.0")
    assert "engine: claude-r2d2" in poster.posted[-1][2] and "t1: building" in poster.posted[-1][2]
    assert not any(e["id"] == "12.0" for e in read_queue(home)), "a command never queues a turn"


def test_pause_resume_authority(bridge, home, poster):
    bridge.handle_message(msg("<@U_MANAGER> pause", user="U_OPERATOR", ts="12.1"))
    assert "not authorized" in poster.posted[-1][2] and not os.path.exists(os.path.join(home, "PAUSE"))
    bridge.handle_message(msg("<@U_MANAGER> pause", ts="12.2"))
    assert open(os.path.join(home, "PAUSE")).read().strip() == "U_FOUNDER"
    bridge.handle_message(msg("<@U_MANAGER> status", ts="12.3"))
    assert poster.posted[-1][2].startswith("paused (by U_FOUNDER)")
    bridge.handle_message(msg("<@U_MANAGER> what now?", ts="12.4"))
    assert "paused (by U_FOUNDER)" in poster.posted[-1][2] and any(e["id"] == "12.4" for e in read_queue(home))
    bridge.handle_message(msg("<@U_MANAGER> resume", user="U_OPERATOR", ts="12.5"))
    assert os.path.exists(os.path.join(home, "PAUSE"))
    bridge.handle_message(msg("<@U_MANAGER> resume", ts="12.6"))
    assert not os.path.exists(os.path.join(home, "PAUSE")) and "resumed" in poster.posted[-1][2]


def test_engine_command(bridge, home, poster):
    bridge.handle_message(msg("<@U_MANAGER> engine codex", user="U_OPERATOR", ts="13.0"))
    assert open(os.path.join(home, "engine")).read().strip() == "claude-r2d2", "an operator cannot switch"
    bridge.handle_message(msg("<@U_MANAGER> engine bogus", ts="13.1"))
    assert "unknown engine" in poster.posted[-1][2] and open(os.path.join(home, "engine")).read().strip() == "claude-r2d2"
    bridge.handle_message(msg("<@U_MANAGER> engine codex", ts="13.2"))
    assert S.read_engine(home) == {"acc": "codex", "model": "gpt-6-astra"}
    assert poster.posted[-1][2] == "engine: codex (gpt-6-astra)", "the legacy form means the family default model"
    bridge.handle_message(msg("<@U_MANAGER> engine", ts="13.3"))
    assert poster.posted[-1][2] == "engine: codex (gpt-6-astra)"


def test_engine_command_acc_and_model(bridge, home, poster):
    bridge.handle_message(msg("<@U_MANAGER> engine acc=claude-l model=sonnet5", ts="14.0"))
    assert S.read_engine(home) == {"acc": "claude-l", "model": "claude-sonnet-5"}
    assert poster.posted[-1][2] == "engine: claude-l (claude-sonnet-5)"
    bridge.handle_message(msg("<@U_MANAGER> engine model=opus5", ts="14.1"))
    assert S.read_engine(home) == {"acc": "claude-l", "model": "claude-opus-5"}, "model alone keeps the account"
    # wrong family and unknown alias: rejected with the valid list, nothing changes
    before = open(os.path.join(home, "engine")).read()
    bridge.handle_message(msg("<@U_MANAGER> engine acc=claude-l model=sol", ts="14.2"))
    assert "codex family" in poster.posted[-1][2] and "sonnet5" in poster.posted[-1][2] and "nothing changed" in poster.posted[-1][2]
    bridge.handle_message(msg("<@U_MANAGER> engine acc=codex model=zzz", ts="14.3"))
    assert "unknown model" in poster.posted[-1][2] and "gpt6" in poster.posted[-1][2]
    assert open(os.path.join(home, "engine")).read() == before
    # the operator is refused for the new form too
    bridge.handle_message(msg("<@U_MANAGER> engine acc=codex model=gpt6", user="U_OPERATOR", ts="14.4"))
    assert "not authorized" in poster.posted[-1][2] and open(os.path.join(home, "engine")).read() == before
    # a full id of the right family and codex aliases
    bridge.handle_message(msg("<@U_MANAGER> engine acc=codex model=gpt-5.6-sol", ts="14.5"))
    assert S.read_engine(home) == {"acc": "codex", "model": "gpt-5.6-sol"}
    bridge.handle_message(msg("<@U_MANAGER> status", ts="14.6"))
    assert "engine: codex (gpt-5.6-sol)" in poster.posted[-1][2]


def test_digest_now_queues_a_timer_event(bridge, home, poster):
    bridge.handle_message(msg("<@U_MANAGER> digest now", user="U_OPERATOR", ts="14.0"))
    assert "not authorized" in poster.posted[-1][2]
    bridge.handle_message(msg("<@U_MANAGER> digest now", ts="14.1", thread="1.0"))
    ev = read_queue(home)[-1]
    assert ev["source"] == "timer" and ev["payload"]["digest"] is True and ev["payload"]["thread_ts"] == "1.0"
    assert "digest: true" in S.event_header(ev)


def test_non_exact_command_text_is_conversation(bridge, home, poster):
    bridge.handle_message(msg("<@U_MANAGER> status of track t1?", ts="15.0"))
    assert poster.posted == [] and read_queue(home)[-1]["id"] == "15.0"
    assert read_queue(home)[-1]["payload"]["addressed"] is True


def test_console_session_answer(bridge, home, poster):
    with S.acquire_writer(home, "hydra@console"):
        bridge.handle_message(msg("<@U_MANAGER> are you there?", ts="16.0"))
    assert "manager in console session" in poster.posted[-1][2] and read_queue(home)[-1]["id"] == "16.0"


def test_unknown_bot_id_means_any_mention_addresses(home, poster):
    br = B.Bridge(home=home, allowlist={"U_FOUNDER": {"instructs": True}}, poster=poster, token_env={})
    br.handle_message(msg("<@BOT> status", ts="17.0"))
    assert "engine" in poster.posted[-1][2]


def test_allowlist_reload_from_file(home, poster):
    path = os.path.join(home, "allowlist.json")
    open(path, "w").write(json.dumps({"U_A": {"instructs": True}}))
    br = B.Bridge(home=home, allowlist=B.load_allowlist(home), poster=poster, token_env={}, allowlist_path=path)
    br.handle_message(msg("<@BOT> hi", user="U_B", ts="18.0"))
    assert not any(e["id"] == "18.0" for e in read_queue(home))
    open(path, "w").write(json.dumps({"U_A": {"instructs": True}, "U_B": {"instructs": False}}))
    os.utime(path, (1, 1))
    br.handle_message(msg("<@BOT> hi again", user="U_B", ts="18.1"))
    assert any(e["id"] == "18.1" for e in read_queue(home))


def test_check_validates_allowlist_and_slack_env(home, capsys):
    assert B.check(home) is False  # no allowlist, no slack.env
    open(os.path.join(home, "allowlist.json"), "w").write(json.dumps({"U_A": {"instructs": "yes"}}))
    open(os.path.join(home, "credentials", "slack.env"), "w").write("")
    assert B.check(home) is False
    open(os.path.join(home, "allowlist.json"), "w").write(json.dumps({"U_A": {"instructs": True}, "B_X": {"instructs": False}}))
    assert B.check(home) is True
    out = capsys.readouterr().out
    assert "2 sender(s), 1 instruct" in out
