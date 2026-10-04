"""requirement 5 (track now/history_file migration), loops/b7.md."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from conftest import S


def write_state(home, state):
    S.write_text(os.path.join(home, "state.json"), json.dumps(state))


def read_state(home):
    return json.loads(S.read_text(os.path.join(home, "state.json")))


def test_no_repo_is_a_byte_for_byte_noop(home):
    state = {"tracks": [{"id": "a", "stage": "verbose\nhistory"}]}
    write_state(home, state)
    before = Path(home, "state.json").read_bytes()
    S.migrate_track_history(home, None)
    assert Path(home, "state.json").read_bytes() == before


def test_existing_now_is_preserved_and_stage_collapses_to_it(home, tmp_path):
    repo = tempfile.mkdtemp(dir=str(tmp_path))
    write_state(home, {"tracks": [{"id": "flow", "stage": "line1\nline2", "now": "short summary", "owner": "m"}]})
    S.migrate_track_history(home, repo)
    tr = read_state(home)["tracks"][0]
    assert tr["now"] == "short summary" and tr["stage"] == "short summary"
    assert tr["history_file"] == "factory/log/tracks/flow.md"
    assert "line1\nline2" in Path(repo, tr["history_file"]).read_text()
    assert tr["owner"] == "m"


def test_derives_now_from_last_nonblank_line_with_ellipsis_truncation(home, tmp_path):
    repo = tempfile.mkdtemp(dir=str(tmp_path))
    long_line = "word " * 100
    write_state(home, {"tracks": [{"id": "r1", "stage": f"first\n{long_line}"}]})
    S.migrate_track_history(home, repo)
    tr = read_state(home)["tracks"][0]
    assert len(tr["now"]) <= 240 and tr["now"].endswith(("…", "..."))
    assert "\n" not in tr["now"]


def test_rerun_is_idempotent_no_duplicate_archive_content(home, tmp_path):
    repo = tempfile.mkdtemp(dir=str(tmp_path))
    write_state(home, {"tracks": [{"id": "a", "stage": "verbose\nhistory"}]})
    S.migrate_track_history(home, repo)
    archive = Path(repo, "factory/log/tracks/a.md")
    first = archive.read_text()
    state_bytes = Path(home, "state.json").read_bytes()
    S.migrate_track_history(home, repo)
    assert archive.read_text() == first
    assert Path(home, "state.json").read_bytes() == state_bytes


def test_new_unseen_history_is_appended_not_duplicated(home, tmp_path):
    repo = tempfile.mkdtemp(dir=str(tmp_path))
    history = "header line\nfirst chunk"
    write_state(home, {"tracks": [{"id": "a", "stage": history}]})
    S.migrate_track_history(home, repo)
    archive = Path(repo, "factory/log/tracks/a.md")
    assert history in archive.read_text()
    state = read_state(home)
    state["tracks"][0]["stage"] = "second chunk"
    write_state(home, state)
    S.migrate_track_history(home, repo)
    text = archive.read_text()
    assert history in text and text.count("second chunk") == 1
    S.migrate_track_history(home, repo)
    assert archive.read_text() == text


def test_unsafe_id_rejected_before_any_write(home, tmp_path):
    repo = tempfile.mkdtemp(dir=str(tmp_path))
    write_state(home, {"tracks": [{"id": "../escape", "stage": "secret"}]})
    before = Path(home, "state.json").read_bytes()
    try:
        S.migrate_track_history(home, repo)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for an unsafe track id")
    assert Path(home, "state.json").read_bytes() == before
    assert not list(Path(repo).rglob("*"))


def test_archive_written_before_state_replace_and_recovers_after_crash(home, tmp_path):
    repo = tempfile.mkdtemp(dir=str(tmp_path))
    write_state(home, {"tracks": [{"id": "a", "stage": "header\nverbose history"}]})
    old_bytes = Path(home, "state.json").read_bytes()
    real_replace = os.replace
    hit = []

    def crash(src, dst, *a, **kw):
        if Path(dst) == Path(home, "state.json"):
            hit.append(True)
            assert "header\nverbose history" in Path(repo, "factory/log/tracks/a.md").read_text()
            assert Path(home, "state.json").read_bytes() == old_bytes
            raise OSError("injected")
        return real_replace(src, dst, *a, **kw)
    with patch.object(S.os, "replace", side_effect=crash):
        try:
            S.migrate_track_history(home, repo)
        except OSError:
            pass
    assert hit and Path(home, "state.json").read_bytes() == old_bytes
    S.migrate_track_history(home, repo)  # retry after the crash: no duplicate, state now updated
    assert read_state(home)["tracks"][0]["now"] == "verbose history"
    assert Path(repo, "factory/log/tracks/a.md").read_text().count("verbose history") == 1


def test_dict_shaped_tracks_also_migrate(home, tmp_path):
    repo = tempfile.mkdtemp(dir=str(tmp_path))
    write_state(home, {"other": {"k": 1}, "tracks": {"b": {"now": "running", "stage": "old\nhistory"}}})
    S.migrate_track_history(home, repo)
    state = read_state(home)
    assert state["other"] == {"k": 1}
    assert state["tracks"]["b"]["now"] == state["tracks"]["b"]["stage"] == "running"
    assert "old\nhistory" in Path(repo, state["tracks"]["b"]["history_file"]).read_text()


def test_tracks_summary_now_first_dict_and_nondict_values():
    assert S.tracks_summary({"tracks": {"t1": "building"}}) == ["t1: building"]
    assert S.tracks_summary({"tracks": {"b": {"now": "running", "stage": "old\nhistory"}}}) == ["b: running"]
    summary = S.tracks_summary({"tracks": [{"id": "x", "stage": "a\n" + "z " * 100}]})
    assert len(summary) == 1 and "\n" not in summary[0] and len(summary[0]) <= 250


def test_status_text_emits_one_line_per_track_not_joined(home, tmp_path):
    repo = tempfile.mkdtemp(dir=str(tmp_path))
    write_state(home, {"tracks": [{"id": "a", "now": "first", "stage": "x"}, {"id": "b", "now": "second", "stage": "y"}]})
    text = S.status_text(home, repo)
    lines = text.splitlines()
    assert "a: first" in lines and "b: second" in lines
