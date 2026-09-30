"""Shared memory across engines (loops/b2.md): the preamble from a synthetic ledger, the Claude snapshot, the handoff
header, the ledger line per turn (files written from hashes, model), the manager's own reply in the mirror, the JSON
engine file (parse, legacy upgrade, aliases, family check, rejection) and the model carry-over on an automatic switch."""
import json
import os
import re

import pytest

from conftest import S, calls, engines, fake_engine, queue_event


def ledger(sup):
    return S.read_ledger(sup.memory_dir)


def recording_engine(dir_, name="claude", shared_line=None, notes_line=None, memory_line=None, behaviour="ok"):
    """A fake engine that also records HYDRA_* env and, like the rule tells the engines to, writes into
    $HYDRA_MEMORY_DIR (MEMORY.md, codex/NOTES.md) and into Claude's own memory folder under CLAUDE_CONFIG_DIR."""
    p = os.path.join(dir_, name)
    body = ["import sys, os, json", "msg=sys.stdin.read()",
            f"open({dir_!r}+'/calls.jsonl','a').write(json.dumps({{'argv': sys.argv[1:], 'env': {{k: v for k, v in os.environ.items() if k.startswith(('CLAUDE','CODEX','HYDRA'))}}, 'stdin': msg}})+'\\n')"]
    if memory_line:
        body += ["d=os.environ['CLAUDE_CONFIG_DIR']; md=os.path.join(d,'projects',os.getcwd().replace('/','-'),'memory'); os.makedirs(md,exist_ok=True)",
                 f"open(os.path.join(md,'MEMORY.md'),'a').write({memory_line!r}+'\\n')"]
    if shared_line:
        body += [f"open(os.path.join(os.environ['HYDRA_MEMORY_DIR'],'MEMORY.md'),'a').write({shared_line!r}+'\\n')"]
    if notes_line:
        body += ["os.makedirs(os.path.join(os.environ['HYDRA_MEMORY_DIR'],'codex'),exist_ok=True)",
                 f"open(os.path.join(os.environ['HYDRA_MEMORY_DIR'],'codex','NOTES.md'),'a').write({notes_line!r}+'\\n')"]
    if behaviour == "quota":
        body += ["print('usage limit reached', file=sys.stderr); sys.exit(1)"]
    else:
        body += ["print('REPLY: ok ' + str(msg.count('source:')))", "print('---HANDOFF---')",
                 "print('tracks: t1\\nwaiting on: nobody\\nlast decision: none\\nnext action: none\\nopen question: none')"]
    open(p, "w").write("#!/usr/bin/env python3\n" + "\n".join(body) + "\n")
    os.chmod(p, 0o755)
    return p


# ----------------------------------------------------------------------------------------------- preamble (pure)

def write_ledger(mem, rows):
    os.makedirs(mem, exist_ok=True)
    with open(os.path.join(mem, "LEDGER.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_preamble_lists_other_engine_files_since_this_family_last_turn(tmp_path):
    mem = str(tmp_path / "mem")
    write_ledger(mem, [
        {"turn": 1, "at": "T1", "engine": "claude-r2d2", "model": "m", "files_written": ["claude/MEMORY.md", "MEMORY.md"]},
        {"turn": 2, "at": "T2", "engine": "codex", "model": "m", "files_written": ["codex/NOTES.md", "MEMORY.md"]},
        {"turn": 3, "at": "T3", "engine": "claude-l", "model": "m", "files_written": ["MANAGER-HANDOFF.md"]},
        {"turn": 4, "at": "T4", "engine": "codex", "model": "m", "files_written": ["codex/NOTES.md"]},
    ])
    S.write_text(os.path.join(mem, "MANAGER-HANDOFF.md"), S.stamp_handoff("tracks: x", "codex", at="T4"))
    lines = S.memory_preamble(mem, "claude").splitlines()
    assert len(lines) == 3 and all(l.startswith("[memory] ") for l in lines)
    assert lines[0] == "[memory] last turn: T4 on codex (turn 4). your last turn on claude: turn 3 at T3."
    # only turn 4 is after claude's last turn (3); turn 2's files are before the cutoff and not listed
    assert lines[1] == "[memory] changed by other engines since then: codex/NOTES.md (T4), MANAGER-HANDOFF.md (T4)."
    assert "MEMORY.md (T2)" not in lines[1]
    assert lines[2] == "[memory] MEMORY.md last written T2 by codex. Read the changed files before acting."


def test_preamble_same_family_says_none_and_first_turn_says_never(tmp_path):
    mem = str(tmp_path / "mem")
    assert S.memory_preamble(mem, "claude").splitlines() == [
        "[memory] last turn: none. your last turn on claude: none.",
        "[memory] changed by other engines since then: none (same engine since your last turn).",
        "[memory] MEMORY.md last written never. Read the changed files before acting."]
    write_ledger(mem, [{"turn": 1, "at": "T1", "engine": "claude-r2d2", "model": "m", "files_written": ["MEMORY.md", "claude/MEMORY.md"]},
                       {"turn": 2, "at": "T2", "engine": "claude-l", "model": "m", "files_written": ["MANAGER-HANDOFF.md"]}])
    S.write_text(os.path.join(mem, "MANAGER-HANDOFF.md"), S.stamp_handoff("tracks: x", "claude-l", at="T2"))
    lines = S.memory_preamble(mem, "claude").splitlines()
    assert lines[0] == "[memory] last turn: T2 on claude-l (turn 2). your last turn on claude: turn 2 at T2."
    assert lines[1] == "[memory] changed by other engines since then: none (same engine since your last turn)."
    assert lines[2].startswith("[memory] MEMORY.md last written T1 by claude-r2d2.")
    # the two Claude accounts are one family: codex sees everything they wrote
    cx = S.memory_preamble(mem, "codex").splitlines()
    assert cx[0].endswith("your last turn on codex: none.")
    assert "MEMORY.md (T1)" in cx[1] and "claude/MEMORY.md (T1)" in cx[1] and "MANAGER-HANDOFF.md (T2)" in cx[1]


def test_preamble_lists_handoff_on_header_mismatch_even_without_a_ledger_entry(tmp_path):
    mem = str(tmp_path / "mem")
    write_ledger(mem, [{"turn": 1, "at": "T1", "engine": "claude-r2d2", "model": "m", "files_written": []}])
    S.write_text(os.path.join(mem, "MANAGER-HANDOFF.md"), S.stamp_handoff("tracks: x", "codex", at="T9"))
    lines = S.memory_preamble(mem, "claude").splitlines()
    assert lines[1] == "[memory] changed by other engines since then: MANAGER-HANDOFF.md (T9)."
    assert S.memory_preamble(mem, "codex").splitlines()[1].endswith("none (same engine since your last turn).")


# ----------------------------------------------------------------------------------------------- header, snapshot, hashes

def test_stamp_and_strip_handoff_header():
    text = S.stamp_handoff("tracks: t1\nwaiting on: x\n", "claude-l", at="2026-09-30T10:00:00Z")
    assert text == "updated_at: 2026-09-30T10:00:00Z\nengine: claude-l\n\ntracks: t1\nwaiting on: x\n"
    assert S.handoff_header(text) == {"updated_at": "2026-09-30T10:00:00Z", "engine": "claude-l"}
    assert S.strip_handoff_header(text) == "tracks: t1\nwaiting on: x"
    again = S.stamp_handoff(text, "codex", at="T2")
    assert again.count("updated_at:") == 1 and again.splitlines()[1] == "engine: codex", "restamping does not nest headers"
    assert S.handoff_header("tracks: plain") == {"updated_at": "", "engine": ""}


def test_snapshot_copies_claude_memory_files_and_mirrors_deletions(tmp_path):
    cfg = str(tmp_path / "cfg"); cwd = "/srv/hydra/manager"
    src = os.path.join(cfg, "projects", "-srv-hydra-manager", "memory")
    os.makedirs(src)
    open(os.path.join(src, "MEMORY.md"), "w").write("- [a](a.md)\n"); open(os.path.join(src, "a.md"), "w").write("fact a\n")
    open(os.path.join(src, "notes.txt"), "w").write("ignored\n")
    dest = str(tmp_path / "mem" / "claude")
    assert S.snapshot_claude_memory(cfg, cwd, dest) == ["MEMORY.md", "a.md"]
    assert open(os.path.join(dest, "a.md")).read() == "fact a\n" and not os.path.exists(os.path.join(dest, "notes.txt"))
    os.remove(os.path.join(src, "a.md")); open(os.path.join(src, "b.md"), "w").write("fact b\n")
    S.snapshot_claude_memory(cfg, cwd, dest)
    assert sorted(os.listdir(dest)) == ["MEMORY.md", "b.md"], "the snapshot mirrors the source"
    assert S.snapshot_claude_memory(str(tmp_path / "nocfg"), cwd, str(tmp_path / "x")) == [] and not os.path.exists(tmp_path / "x")


def test_dir_hashes_and_files_changed(tmp_path):
    root = str(tmp_path / "m"); os.makedirs(os.path.join(root, "codex"))
    open(os.path.join(root, "MEMORY.md"), "w").write("a"); open(os.path.join(root, "LEDGER.jsonl"), "w").write("{}\n")
    before = S.dir_hashes(root)
    assert set(before) == {"MEMORY.md"}, "the ledger itself is not a written file"
    open(os.path.join(root, "MEMORY.md"), "a").write("b"); open(os.path.join(root, "codex", "NOTES.md"), "w").write("n")
    assert S.files_changed(before, S.dir_hashes(root)) == ["MEMORY.md", "codex/NOTES.md"]
    assert S.files_changed(before, before) == []


# ----------------------------------------------------------------------------------------------- the engine file and models

def test_parse_engine_command_aliases_legacy_and_full_ids():
    assert S.parse_engine_command("acc=claude-l model=sonnet5") == {"acc": "claude-l", "model": "claude-sonnet-5"}
    assert S.parse_engine_command("acc=codex") == {"acc": "codex", "model": "gpt-6-astra"}
    assert S.parse_engine_command("acc=codex model=sol") == {"acc": "codex", "model": "gpt-5.6-sol"}
    assert S.parse_engine_command("acc=codex model=astra") == S.parse_engine_command("acc=codex model=gpt6")
    assert S.parse_engine_command("claude-r2d2") == {"acc": "claude-r2d2", "model": "claude-fable-5-1"}
    assert S.parse_engine_command("acc=claude-l model=claude-opus-5") == {"acc": "claude-l", "model": "claude-opus-5"}
    assert S.parse_engine_command("acc=claude-l model=claude-opus-5-5")["model"] == "claude-opus-5-5", "a full id of the family passes as-is"
    assert S.parse_engine_command("model=haiku4.5", current={"acc": "claude-l", "model": "x"}) == {"acc": "claude-l", "model": "claude-haiku-4-5-20251001"}
    assert S.parse_engine_command("ACC=Codex") == {"acc": "codex", "model": "gpt-6-astra"}


@pytest.mark.parametrize("bad,needle", [
    ("acc=claude-l model=sol", "codex family"),
    ("acc=codex model=fable5.1", "claude family"),
    ("acc=nope", "unknown engine"),
    ("acc=claude-l model=zzz", "unknown model"),
    ("model=sonnet5", "missing account"),
    ("acc=claude-l foo=bar", "unknown parameter"),
    ("claude-l codex", "unexpected word"),
    ("", "missing account"),
])
def test_parse_engine_command_rejects_with_a_reason(bad, needle):
    with pytest.raises(S.BadEngine) as e:
        S.parse_engine_command(bad)
    assert needle in str(e.value)
    if "model" in bad and "acc=c" in bad:
        assert "sonnet5" in str(e.value) or "gpt6" in str(e.value), "the rejection lists the valid aliases"


def test_read_engine_legacy_json_and_upgrade(home, ok_engine, poster):
    assert open(os.path.join(home, "engine")).read().strip() == "claude-r2d2"
    assert S.read_engine(home) == {"acc": "claude-r2d2", "model": "claude-fable-5-1"}
    assert S.engine_file_is_legacy(home)
    os.remove(os.path.join(home, "engine"))
    assert S.read_engine(home) == {"acc": "claude-r2d2", "model": "claude-fable-5-1"}, "no file: the first engine"
    open(os.path.join(home, "engine"), "w").write("codex\n")
    assert S.read_engine(home) == {"acc": "codex", "model": "gpt-6-astra"}
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "claude-l"}))
    assert S.read_engine(home) == {"acc": "claude-l", "model": "claude-fable-5-1"}, "missing model: family default"
    # a turn upgrades a legacy file to JSON without changing the account
    open(os.path.join(home, "engine"), "w").write("claude-l\n")
    queue_event(home, "hi")
    assert S.Supervisor(home=home, engines=engines(ok_engine), poster=poster).run_once() is True
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "claude-l", "model": "claude-fable-5-1"}
    assert S.set_engine(home, "codex") == {"acc": "codex", "model": "gpt-6-astra"}
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "codex", "model": "gpt-6-astra"}


def test_models_json_is_extendable_from_home(home):
    open(os.path.join(home, "models.json"), "w").write(json.dumps({"claude": {"aliases": {"next": "claude-next-1"}}}))
    models = S.load_models(home)
    assert models["claude"]["aliases"]["next"] == "claude-next-1" and models["claude"]["aliases"]["sonnet5"] == "claude-sonnet-5"
    assert S.family_default_model("claude", models) == "claude-fable-5-1" and S.family_default_model("codex") == "gpt-6-astra"
    assert S.resolve_model("claude", "next", models) == "claude-next-1"


def test_carry_model_on_switch():
    assert S.carry_model("claude-sonnet-5", "claude", "claude") == "claude-sonnet-5"
    assert S.carry_model("claude-sonnet-5", "claude", "codex") == "gpt-6-astra", "no such alias in codex: family default"
    assert S.carry_model("gpt-5.6-sol", "codex", "claude") == "claude-fable-5-1"
    assert S.carry_model("", "claude", "claude") == "claude-fable-5-1"


# ----------------------------------------------------------------------------------------------- the turn

def test_turn_exports_memory_dir_stamps_header_snapshots_and_ledgers(home, poster, tmp_path):
    d = str(tmp_path / "cl"); os.makedirs(d)
    cl = recording_engine(d, memory_line="the founder prefers small boxes", shared_line="2026-09-30 12:00Z claude: VM is hydra-manager")
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-sonnet-5"}))
    sup = S.Supervisor(home=home, engines=engines(cl), poster=poster)
    assert sup.memory_dir == os.path.join(home, "manager-memory"), "no repo: the memory folder lives in HYDRA_HOME"
    queue_event(home, "first")
    assert sup.run_once() is True
    c = calls(d)[-1]
    assert c["env"]["HYDRA_HOME"] == home and c["env"]["HYDRA_MEMORY_DIR"] == sup.memory_dir
    assert c["argv"][c["argv"].index("--model") + 1] == "claude-sonnet-5", "the model id comes from the JSON engine file"
    assert c["stdin"].startswith("[memory] last turn: none. your last turn on claude: none.\n")
    assert c["stdin"].count("[memory]") == 3 and "Manager turn. 1 event" in c["stdin"]
    mem = sup.memory_dir
    assert open(os.path.join(mem, "MEMORY.md")).read().startswith("# Manager memory") and "VM is hydra-manager" in open(os.path.join(mem, "MEMORY.md")).read()
    assert "## Conflicts" in open(os.path.join(mem, "MEMORY.md")).read()
    assert "small boxes" in open(os.path.join(mem, "claude", "MEMORY.md")).read(), "Claude's memory is snapshotted"
    hand = open(os.path.join(mem, "MANAGER-HANDOFF.md")).read()
    assert re.match(r"updated_at: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\nengine: claude-r2d2\n\ntracks: t1\n", hand)
    assert os.path.islink(os.path.join(home, "MANAGER-HANDOFF.md")) and open(os.path.join(home, "MANAGER-HANDOFF.md")).read() == hand
    rows = ledger(sup)
    assert len(rows) == 1 and rows[0]["turn"] == 1 and rows[0]["engine"] == "claude-r2d2" and rows[0]["model"] == "claude-sonnet-5"
    assert rows[0]["files_written"] == ["MANAGER-HANDOFF.md", "MEMORY.md", "claude/MEMORY.md"], "files written from the hash diff"
    assert rows[0]["handoff_sha"] and re.match(r"\d{4}-\d\d-\d\dT", rows[0]["at"])
    turn = S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))[-1]
    assert turn["model"] == "claude-sonnet-5" and turn["engine"] == "claude-r2d2"
    # the manager's own reply is mirrored into the channel log
    mirror = S.read_jsonl(os.path.join(home, "mirror", "C_DEV.jsonl"))
    assert mirror[-1]["user"] == "manager" and mirror[-1]["turn"] == 1 and "REPLY: ok" in mirror[-1]["text"] and mirror[-1]["thread_ts"] == "1.0"
    # same family again: the preamble says none, and a turn that writes nothing new lists only what changed
    queue_event(home, "second")
    assert sup.run_once() is True
    lines = [l for l in calls(d)[-1]["stdin"].splitlines() if l.startswith("[memory]")]
    assert re.match(r"^\[memory\] last turn: \S+ on claude-r2d2 \(turn 1\)\. your last turn on claude: turn 1 at \S+\.$", lines[0])
    assert lines[1] == "[memory] changed by other engines since then: none (same engine since your last turn)."
    assert re.match(r"^\[memory\] MEMORY\.md last written \S+ by claude-r2d2\. Read the changed files before acting\.$", lines[2])


def test_switch_to_codex_and_back_lists_the_other_familys_files(home, poster, tmp_path):
    dcl = str(tmp_path / "cl"); os.makedirs(dcl); dcx = str(tmp_path / "cx"); os.makedirs(dcx)
    cl = recording_engine(dcl, memory_line="claude private note", shared_line="claude shared")
    cx = recording_engine(dcx, name="codex", shared_line="codex shared", notes_line="codex note")
    sup = S.Supervisor(home=home, engines=engines(cl, cl, cx), poster=poster)
    queue_event(home, "one"); assert sup.run_once() is True
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "codex", "model": "gpt-5.6-sol"}))
    queue_event(home, "two"); assert sup.run_once() is True
    c = calls(dcx)[-1]
    assert c["argv"][c["argv"].index("-m") + 1] == "gpt-5.6-sol" and c["argv"][0] == "exec"
    assert c["env"]["HYDRA_MEMORY_DIR"] == sup.memory_dir and "CLAUDE_CODE_OAUTH_TOKEN" not in c["env"]
    lines = [l for l in c["stdin"].splitlines() if l.startswith("[memory]")]
    assert len(lines) == 3 and lines[0].endswith("(turn 1). your last turn on codex: none.")
    assert "claude/MEMORY.md (" in lines[1] and "MEMORY.md (" in lines[1] and "MANAGER-HANDOFF.md (" in lines[1]
    assert c["stdin"].index("[memory]") < c["stdin"].index("# MANAGER-HANDOFF.md") < c["stdin"].index("Manager turn.")
    assert "engine: claude-r2d2" in c["stdin"], "codex gets the stamped handoff on stdin"
    rows = ledger(sup)
    assert rows[-1]["engine"] == "codex" and rows[-1]["model"] == "gpt-5.6-sol"
    assert set(rows[-1]["files_written"]) == {"MANAGER-HANDOFF.md", "MEMORY.md", "codex/NOTES.md"}, "no claude snapshot on a codex turn"
    assert open(os.path.join(sup.memory_dir, "MANAGER-HANDOFF.md")).read().splitlines()[1] == "engine: codex"
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "claude-l", "model": "claude-fable-5-1"}))
    queue_event(home, "three"); assert sup.run_once() is True
    c = calls(dcl)[-1]
    lines = [l for l in c["stdin"].splitlines() if l.startswith("[memory]")]
    assert re.match(r"^\[memory\] last turn: \S+ on codex \(turn 2\)\. your last turn on claude: turn 1 at \S+\.$", lines[0])
    assert "codex/NOTES.md (" in lines[1] and "MANAGER-HANDOFF.md (" in lines[1] and "MEMORY.md (" in lines[1]
    assert "claude/MEMORY.md" not in lines[1], "this family's own earlier snapshot is before the cutoff"
    assert re.match(r"^\[memory\] MEMORY\.md last written \S+ by codex\.", lines[2])
    assert c["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-claude-l"


def test_auto_switch_carries_the_model_alias_or_uses_the_family_default(home, poster, tmp_path):
    dq = str(tmp_path / "q"); os.makedirs(dq); dcx = str(tmp_path / "cx"); os.makedirs(dcx)
    quota = fake_engine(dq, "quota"); cx = recording_engine(dcx, name="codex")
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-sonnet-5"}))
    sup = S.Supervisor(home=home, engines=engines(quota, quota, cx), poster=poster)
    queue_event(home, "hi"); assert sup.run_once() is True
    assert "engine: codex (gpt-6-astra)" in poster.texts, "sonnet5 has no codex twin: the family default, said in the note"
    assert json.load(open(os.path.join(home, "engine"))) == {"acc": "codex", "model": "gpt-6-astra"}
    assert calls(dcx)[-1]["argv"][calls(dcx)[-1]["argv"].index("-m") + 1] == "gpt-6-astra"
    assert ledger(sup)[-1]["model"] == "gpt-6-astra" and S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))[-1]["model"] == "gpt-6-astra"
    # within the family the alias is kept
    home2 = str(tmp_path / "home2"); from conftest import make_home; make_home(home2)
    dok = str(tmp_path / "ok"); os.makedirs(dok); ok = recording_engine(dok)
    open(os.path.join(home2, "engine"), "w").write(json.dumps({"acc": "claude-r2d2", "model": "claude-opus-5"}))
    p2 = poster.__class__(); sup2 = S.Supervisor(home=home2, engines=engines(quota, ok), poster=p2)
    queue_event(home2, "hi"); assert sup2.run_once() is True
    assert "engine: claude-l (claude-opus-5)" in p2.texts
    assert json.load(open(os.path.join(home2, "engine"))) == {"acc": "claude-l", "model": "claude-opus-5"}
    assert calls(dok)[-1]["argv"][calls(dok)[-1]["argv"].index("--model") + 1] == "claude-opus-5"


def test_existing_handoff_at_the_old_path_moves_into_the_memory_folder(home, ok_engine, poster):
    open(os.path.join(home, "MANAGER-HANDOFF.md"), "w").write("tracks: before\n")
    sup = S.Supervisor(home=home, engines=engines(ok_engine), poster=poster)
    sup.ensure_memory_layout()
    assert os.path.islink(os.path.join(home, "MANAGER-HANDOFF.md"))
    assert open(sup.handoff_path()).read() == "tracks: before\n" and open(os.path.join(home, "MANAGER-HANDOFF.md")).read() == "tracks: before\n"
    sup.ensure_memory_layout()  # idempotent
    assert os.path.realpath(os.path.join(home, "MANAGER-HANDOFF.md")) == os.path.realpath(sup.handoff_path())
    assert open(os.path.join(sup.memory_dir, "MEMORY.md")).read() == S.MEMORY_SKELETON
    assert os.path.isdir(os.path.join(sup.memory_dir, "codex"))


def test_failed_turn_writes_no_ledger_line_but_logs_the_model(home, quota_engine, poster):
    queue_event(home, "hello")
    sup = S.Supervisor(home=home, engines=engines(quota_engine, quota_engine), poster=poster)
    assert sup.run_once() is True
    assert ledger(sup) == []
    turn = S.read_jsonl(os.path.join(home, "logs", "turns.jsonl"))[-1]
    assert turn["error"] and turn["model"] == "claude-fable-5-1"


def test_status_shows_acc_and_model(home):
    open(os.path.join(home, "engine"), "w").write(json.dumps({"acc": "claude-l", "model": "claude-sonnet-5"}))
    assert "engine: claude-l (claude-sonnet-5)" in S.status_text(home)


def test_claude_md_carries_the_memory_rule():
    text = open(os.path.join(os.path.dirname(S.__file__), "CLAUDE.md")).read()
    for needle in ("Newer wins", "## Conflicts", "codex/NOTES.md", "The transcript is not memory", "HYDRA_MEMORY_DIR", "LEDGER.jsonl"):
        assert needle in text
    models = json.load(open(os.path.join(os.path.dirname(S.__file__), "models.json")))
    assert models["claude"]["aliases"]["fable5.1"] == "claude-fable-5-1" and models["codex"]["aliases"]["gpt6"] == "gpt-6-astra"
