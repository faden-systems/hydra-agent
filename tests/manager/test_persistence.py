"""requirement 19 (truthful Git persistence), loops/b7.md. Local bare Git repositories only, never GitHub."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from conftest import S


def git(path, *args, check=True):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=fixture", "-c", "user.email=fixture@example.test",
                          *args], check=check, capture_output=True, text=True)


def make_repo(tmp_path):
    root = Path(tempfile.mkdtemp(dir=str(tmp_path)))
    remote = root / "remote.git"
    repo = root / "repo"
    git(root, "init", "--bare", str(remote))
    git(root, "clone", str(remote), str(repo))
    (repo / "seed").write_text("seed")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "seed")
    git(repo, "push", "-u", "origin", "HEAD")
    return root, remote, repo


def read_status(home):
    return json.loads(Path(home, "logs", "persistence.json").read_text())


def test_missing_repo_is_an_explicit_no_repo_status(home):
    sup = S.Supervisor(home=home, repo=os.path.join(home, "does-not-exist"), poster=lambda *a: None)
    assert sup.persist(1) is False
    assert read_status(home)["status"] == "no_repo"


def test_clean_push_reports_synced_with_the_pushed_sha(home, tmp_path):
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    sup = S.Supervisor(home=home, repo=str(repo), poster=lambda *a: None)
    sup.sync_repo_before()
    assert sup.persist(1) is True
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(remote, "rev-parse", "HEAD").stdout.strip() == head
    status = read_status(home)
    assert status["status"] == "synced" and status["last_successful_push_sha"] == head


def test_rejected_push_is_pending_not_synced_and_retries_the_same_commit(home, tmp_path):
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    now = [1000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0], poster=lambda *a: None)
    assert sup.persist(1) is False
    status = read_status(home)
    assert status["status"] == "pending" and status["error"]
    pending_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert status["pending_local_sha"] == pending_sha
    # before the backoff deadline: no retry (even with no new changes to stage)
    now[0] += 1
    assert sup.persist(2) is False
    assert git(remote, "rev-parse", "HEAD").returncode != 0 or git(remote, "rev-parse", "HEAD").stdout.strip() != pending_sha
    hook.unlink()
    now[0] += 61
    assert sup.persist(3) is True
    assert git(remote, "rev-parse", "HEAD").stdout.strip() == pending_sha
    assert read_status(home)["status"] == "synced"


def test_failure_then_recovery_sends_exactly_two_deduplicated_notices(home, tmp_path):
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    now = [1000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0],
                       config={"dev_channel": "C_FIXTURE"}, poster=lambda *a: None)
    sup.persist(1)
    assert len(S.read_outbox(home)) == 1
    hook.unlink()
    now[0] += 61
    sup.persist(2)
    notices = S.read_outbox(home)
    assert len(notices) == 2
    assert "recovered" in notices[1]["text"].lower()
    assert all(n["channel"] == "C_FIXTURE" and not n.get("thread_ts") for n in notices)
    # a further successful persist (nothing changed, nothing failing) must not add more notices
    now[0] += 61
    sup.persist(3)
    assert len(S.read_outbox(home)) == 2


def test_diverged_histories_reconcile_with_an_ordinary_merge(home, tmp_path):
    root, remote, repo = make_repo(tmp_path)
    other = root / "other"
    git(root, "clone", str(remote), str(other))
    (other / "external").write_text("keep")
    git(other, "add", ".")
    git(other, "commit", "-m", "external")
    git(other, "push", "origin", "HEAD")
    (repo / "factory").mkdir()
    (repo / "factory" / "local").write_text("keep local")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "local")
    local = git(repo, "rev-parse", "HEAD").stdout.strip()
    external = git(other, "rev-parse", "HEAD").stdout.strip()
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    sup = S.Supervisor(home=home, repo=str(repo), poster=lambda *a: None)
    sup.sync_repo_before()
    assert git(repo, "merge-base", "--is-ancestor", local, "HEAD", check=False).returncode == 0
    assert git(repo, "merge-base", "--is-ancestor", external, "HEAD", check=False).returncode == 0
    assert (repo / "external").read_text() == "keep"
    assert (repo / "factory" / "local").read_text() == "keep local"


def test_merge_conflict_aborts_and_blocks_retaining_local_work(home, tmp_path):
    root, remote, repo = make_repo(tmp_path)
    other = root / "other"
    git(root, "clone", str(remote), str(other))
    (other / "unrelated").write_text("remote")
    git(other, "add", ".")
    git(other, "commit", "-m", "remote")
    git(other, "push", "origin", "HEAD")
    (repo / "unrelated").write_text("local")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "local")
    local = git(repo, "rev-parse", "HEAD").stdout.strip()
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    sup = S.Supervisor(home=home, repo=str(repo), poster=lambda *a: None)
    sup.sync_repo_before()
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == local
    assert (repo / "unrelated").read_text() == "local"
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert read_status(home)["status"] == "blocked"
    assert sup.persist(2) is False, "persist must not proceed after an aborted merge"
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == local


def test_dirty_tree_outside_managed_paths_blocks_without_touching_git(home, tmp_path):
    root, remote, repo = make_repo(tmp_path)
    initial = git(repo, "rev-parse", "HEAD").stdout
    (repo / "unrelated").write_text("dirty")
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    sup = S.Supervisor(home=home, repo=str(repo), poster=lambda *a: None)
    assert sup.persist(1) is False
    assert git(repo, "rev-parse", "HEAD").stdout == initial
    assert (repo / "unrelated").read_text() == "dirty"
    assert read_status(home)["status"] == "blocked"


def test_managed_paths_alone_do_not_block(home, tmp_path):
    root, remote, repo = make_repo(tmp_path)
    (repo / "factory").mkdir()
    (repo / "factory" / "log").mkdir()
    (repo / "factory" / "log" / "existing.jsonl").write_text("old\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "pre-existing log")
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    sup = S.Supervisor(home=home, repo=str(repo), poster=lambda *a: None)
    assert sup._repo_unsafe_dirty() is False


def test_reconcile_persistence_respects_backoff_and_a_new_turn_does_not_reset_it(home, tmp_path):
    root, remote, repo = make_repo(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    now = [1000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0], poster=lambda *a: None)
    sup.persist(1)
    assert read_status(home)["status"] == "pending"
    hook.unlink()
    calls = []
    real_git = sup._git

    def wrapped(*a, **kw):
        calls.append(a)
        return real_git(*a, **kw)
    sup._git = wrapped
    sup.reconcile_persistence()
    assert not calls, "reconciliation must not touch git before the backoff deadline"
    now[0] += 61
    sup.reconcile_persistence()
    assert calls, "reconciliation must retry once the deadline has passed"
    assert read_status(home)["status"] == "synced"


def test_reaction_reconciliation_runs_on_a_tick_with_no_events(home):
    from types import SimpleNamespace
    added = []
    reactor = SimpleNamespace(add=lambda *a: added.append(a), remove=lambda *a: None)
    sup = S.Supervisor(home=home, poster=lambda *a: None, reactor=reactor)
    sup._save_work_reaction({"channel": "C", "ts": "m", "name": "timer_clock", "action": "add", "confirmed": False})
    assert sup.run_once() is False  # nothing else to do this tick
    assert added == [("C", "m", "timer_clock")]
