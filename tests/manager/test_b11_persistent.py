"""loops/b11.md requirement 7 (r): Claude persistent mode with a fake stream-json child: the digest and
`message_bytes` on every persistent turn record, the child reused across ordinary turns, a 3x context compacting
before the next queued turn with the child retired and the session id replaced, the verifying turn settling ok,
and a CLI boundary in the new session's transcript flagged."""
import contextlib
import json
import time
from pathlib import Path

from b11_helpers import S, calls, cstate, event, fakes, home, make, session_id, status_line, transcript_dir, turns
from test_b11_digest import big_state, digest_of


def test_r_persistent_mode_is_covered():
    h, d = home(), fakes(context=100000, context_after=90000)
    Path(h, "engine").write_text(json.dumps({"acc": "claude-r2d2", "mode": "persistent"}) + "\n")
    Path(h, "state.json").write_text(json.dumps(big_state(), indent=1))
    sup = make(h, d)
    try:
        event(h, "persistent turn A: éè", thread="1791000005.000001")
        assert sup.run_once() is True
        c = calls(d)[-1]
        assert c["persistent"] is True
        digest = digest_of(c["stdin"])
        assert len(digest.encode()) <= 36000 and "MARKER-t05-DEEP" in c["stdin"] and c["stdin"].count("# factory/state.json") == 1
        assert turns(h)[-1]["message_bytes"] == len(c["stdin"].encode("utf-8"))
        pid_a = c["pid"]
        event(h, "persistent turn B"); sup.run_once()
        assert calls(d)[-1]["pid"] == pid_a, "the child is reused across ordinary turns"
        assert turns(h)[-1]["message_bytes"] == len(calls(d)[-1]["stdin"].encode("utf-8"))
        (d / "context").write_text("940000")
        event(h, "persistent turn C"); sup.run_once()
        assert turns(h)[-1]["context_tokens"] == 940000
        event(h, "persistent turn D"); sup.run_once()
        ks = [(x["kind"], x["pid"]) for x in calls(d)]
        assert [k for k, _ in ks] == ["turn", "turn", "turn", "compaction", "turn"], ks
        assert session_id(h) != "sess-test" and ks[-1][1] != pid_a, "the old child is retired by the rollover"
        gone = False
        for _ in range(50):
            try:
                gone = Path(f"/proc/{pid_a}/stat").read_text().split(")")[-1].split()[0] == "Z"
            except FileNotFoundError:
                gone = True
            if gone:
                break
            time.sleep(0.1)
        assert gone, "the retired child must have exited"
        crec = [r for r in turns(h) if r.get("kind") == "compaction"]
        assert crec[-1]["message_bytes"] == len(calls(d)[-2]["stdin"].encode("utf-8"))
        last = cstate(h)["last"]
        assert last["ok"] is True and last["after_tokens"] == 90000
        tdir = transcript_dir(h)
        (tdir / f"{session_id(h)}.jsonl").write_text(json.dumps({"type": "system", "subtype": "compact_boundary", "timestamp": "2026-10-08T13:00:00.000Z"}) + "\n")
        event(h, "persistent turn E"); sup.run_once()
        assert turns(h)[-1]["cli_compacted"] is True and "(cli)" in status_line(h)
    finally:
        with contextlib.suppress(Exception):
            sup._retire_persistent("planned", "test done")
