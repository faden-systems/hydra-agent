"""requirement 19 (truthful Git persistence), loops/b7.md. Local bare Git repositories only, never GitHub."""
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

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


def head(path):
    return git(path, "rev-parse", "HEAD").stdout.strip()


def reject(remote):
    """Install a pre-receive hook that rejects every push until `.unlink()`."""
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    return hook


def push_other(root, remote, name, content):
    """A second clone commits on top of the remote's current tip -- never the supervisor's job -- and pushes."""
    other = Path(tempfile.mkdtemp(dir=str(root)))
    git(root, "clone", str(remote), str(other))
    (other / name).write_text(content)
    git(other, "add", ".")
    git(other, "commit", "-m", name)
    git(other, "push", "origin", "HEAD")
    return other


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


def test_quiet_retry_recovers_within_five_minutes_without_a_notice(home, tmp_path):
    """loops/b10.md requirement 7a: a collision lifted before the five-minute deadline posts nothing, ever."""
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    now = [1000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0],
                       config={"dev_channel": "C_FIXTURE"}, poster=lambda *a: None)
    sup.persist(1)
    assert len(S.read_outbox(home)) == 0, "a failure must queue nothing immediately"
    assert read_status(home)["status"] == "pending"
    hook.unlink()
    now[0] += 61
    sup.persist(2)
    assert read_status(home)["status"] == "synced"
    assert len(S.read_outbox(home)) == 0, "a recovery within five minutes posts nothing"
    # a further successful persist (nothing changed, nothing failing) must not add more notices either
    now[0] += 61
    sup.persist(3)
    assert len(S.read_outbox(home)) == 0


def test_diverged_histories_reconcile_with_a_rebase_not_a_merge(home, tmp_path):
    """loops/b10.md requirement 1: `sync_repo_before` reconciles a diverged clean clone by rebasing the local
    commit onto the remote's, never `git merge` -- a linear history with both contents present."""
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
    external = git(other, "rev-parse", "HEAD").stdout.strip()
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    sup = S.Supervisor(home=home, repo=str(repo), poster=lambda *a: None)
    sup.sync_repo_before()
    assert git(repo, "merge-base", "--is-ancestor", external, "HEAD", check=False).returncode == 0, \
        "remote history absent"
    assert (repo / "external").read_text() == "keep"
    assert (repo / "factory" / "local").read_text() == "keep local"
    assert git(repo, "rev-list", "--merges", "HEAD").stdout.strip() == "", "a merge commit was created"
    assert not (repo / ".git" / "MERGE_HEAD").exists() and not (repo / ".git" / "rebase-merge").exists()
    assert sup.persist(1) is True
    assert git(remote, "rev-parse", "HEAD").stdout.strip() == git(repo, "rev-parse", "HEAD").stdout.strip()
    assert git(remote, "rev-list", "--merges", "HEAD").stdout.strip() == ""


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


# --------------------------------------------------------------------------------------- loops/b10.md requirement 7

def test_notice_after_five_minutes_survives_restart_and_posts_one_recovered_line(home, tmp_path):
    """requirements 7b and 7e: exactly one notice at the five-minute deadline, independent of the 60-second
    retry deadline, one recovered line after it, and a restart never repeats either."""
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    hook = reject(remote)
    now = [1000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0],
                       config={"dev_channel": "C_FIXTURE"}, poster=lambda *a: None)
    assert sup.persist(1) is False
    episode = read_status(home)["episode_id"]
    for t in (1061.0, 1183.0, 1299.0):
        now[0] = t
        sup.reconcile_persistence()
        assert S.read_outbox(home) == [], f"nothing before five minutes (t={t})"
    now[0] = 1300.0
    sup.reconcile_persistence()
    notices = S.read_outbox(home)
    assert len(notices) == 1
    n = notices[0]
    assert n["id"] == f"persistence-{episode}-notice" and n["channel"] == "C_FIXTURE" and not n.get("thread_ts")
    assert "\n" not in n["text"] and "hint:" not in n["text"] and episode not in n["text"]
    assert n["text"].startswith("persistence pending for 5 min: push: ")
    assert read_status(home)["notice_at"] == 1300.0
    restarted = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0],
                             config={"dev_channel": "C_FIXTURE"}, poster=lambda *a: None)
    now[0] = 1340.0
    restarted.reconcile_persistence()
    assert len(S.read_outbox(home)) == 1, "a restart must not repeat the notice"
    hook.unlink()
    now[0] = 1360.0
    restarted.reconcile_persistence()
    notices = S.read_outbox(home)
    sha = head(repo)
    assert read_status(home)["status"] == "synced" and sha == git(remote, "rev-parse", "HEAD").stdout.strip()
    assert len(notices) == 2 and notices[1]["id"] == f"persistence-{episode}-recovered"
    assert notices[1]["text"] == f"persistence recovered: synced at {sha[:12]} after 6 min"


def test_rebase_conflict_aborts_blocks_and_gets_a_notice_at_the_deadline(home, tmp_path):
    """requirements 1 and 7d: a rebase conflict aborts cleanly, retains the local commit, blocks, and gets its
    one notice at the five-minute deadline."""
    root, remote, repo = make_repo(tmp_path)
    push_other(root, remote, "unrelated", "remote")
    (repo / "unrelated").write_text("local")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "local")
    local = head(repo)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    now = [2000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0],
                       config={"dev_channel": "C_FIXTURE"}, poster=lambda *a: None)
    sup.sync_repo_before()
    st = read_status(home)
    assert st["status"] == "blocked" and st["error"].startswith("rebase: ")
    assert head(repo) == local, "the local commit must be retained"
    assert not (repo / ".git" / "rebase-merge").exists() and not (repo / ".git" / "rebase-apply").exists()
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert (repo / "unrelated").read_text() == "local"
    assert sup.persist(2) is False, "persist must not proceed after an aborted rebase"
    assert head(repo) == local
    for t in (2061.0, 2200.0, 2299.0):
        now[0] = t
        sup.reconcile_persistence()
        assert S.read_outbox(home) == [], "blocked is never retried by a tick, and no early notice"
    now[0] = 2300.0
    sup.reconcile_persistence()
    notices = S.read_outbox(home)
    assert len(notices) == 1
    assert notices[0]["text"].startswith("persistence blocked for 5 min: rebase: ")
    assert "\n" not in notices[0]["text"]


def test_notice_error_summary_strips_hints_and_secrets_and_truncates():
    """requirement 4: the one-line summary of a stored persistence error."""
    f = S.notice_error_summary
    rejection = ("push: //github.com/faden-systems/faden.git\n ! [rejected]        HEAD -> main (fetch first)\n"
                 "error: failed to push some refs to 'https://github.com/faden-systems/faden.git'\n"
                 "hint: Updates were rejected because the remote contains work that you do not\n"
                 "hint: have locally.")
    assert f(rejection) == "push: failed to push some refs to 'https://github.com/faden-systems/faden.git'"
    secret = f("push: error: failed to push some refs to 'https://hydra:ghp_secret123@github.com/x/y.git'")
    assert secret == "push: failed to push some refs to 'https://<redacted>@github.com/x/y.git'"
    assert "ghp_secret123" not in secret
    assert f("push: error: failed to push some refs to 'https://hydra@github.com/x/y.git'") == \
        "push: failed to push some refs to 'https://<redacted>@github.com/x/y.git'"
    assert f("push: ") == "push: unknown error"
    assert f("push: error: failed; hint: retry later") == "push: failed;"
    assert f("push: error: hint: only a hint") == "push: unknown error"
    long = f("commit: error: " + "x" * 500)
    assert long.startswith("commit: ") and len(long) == len("commit: ") + 200
    for text in (rejection, "push: error: a\nhint: b\n"):
        out = f(text)
        assert "\n" not in out and "hint:" not in out
        assert out.split(": ", 1)[0] == text.split(": ", 1)[0]


def test_legacy_pending_record_is_adopted_without_crashing_and_notices_once(home, tmp_path):
    """requirements 2 and 7g: a b7-era pending record (no `since`) is adopted at first observation, never
    crashed on, and gets its own notice five minutes after adoption."""
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    hook = reject(remote)
    legacy = {"status": "pending", "error": "push: error: failed to push some refs to 'x'",
              "episode_id": "legacy-a", "retry_after": 0, "last_turn": 3}
    Path(home, "logs", "persistence.json").write_text(json.dumps(legacy))
    now = [5000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0],
                       config={"dev_channel": "C_FIXTURE"}, poster=lambda *a: None)
    sup.reconcile_persistence()  # retries (deadline already passed) and fails again; must not crash
    st = read_status(home)
    assert st["status"] == "pending" and st["episode_id"] == "legacy-a" and st["since"] == 5000.0
    assert st["notice_at"] is None and S.read_outbox(home) == []
    for t in (5061.0, 5200.0, 5299.0):
        now[0] = t
        sup.reconcile_persistence()
        assert S.read_outbox(home) == []
    now[0] = 5300.0
    sup.reconcile_persistence()
    notices = S.read_outbox(home)
    assert len(notices) == 1 and notices[0]["id"] == "persistence-legacy-a-notice"
    hook.unlink()
    now[0] = 5400.0
    sup.reconcile_persistence()
    notices = S.read_outbox(home)
    assert read_status(home)["status"] == "synced"
    assert len(notices) == 2 and notices[1]["id"] == "persistence-legacy-a-recovered"


def test_legacy_record_whose_notice_already_posted_never_notices_twice(home, tmp_path):
    """requirements 2 and 7g: a legacy record whose `persistence-<id>-failed` notice already went out is
    adopted with `notice_at` set immediately, so it never gets a second notice and posts exactly one recovered
    line."""
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    hook = reject(remote)
    legacy = {"status": "pending", "error": "push: error: failed to push some refs",
              "episode_id": "legacy-sent", "retry_after": 0, "last_turn": 3}
    Path(home, "logs", "persistence.json").write_text(json.dumps(legacy))
    S._queue_notice_once(home, "C_FIXTURE", "persistence pending (episode legacy-sent): push: x",
                         "persistence-legacy-sent-failed")
    now = [6000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0],
                       config={"dev_channel": "C_FIXTURE"}, poster=lambda *a: None)
    sup.reconcile_persistence()
    st = read_status(home)
    assert st["episode_id"] == "legacy-sent" and st["since"] == 6000.0 and st["notice_at"] == 6000.0
    assert len(S.read_outbox(home)) == 1, "the pre-existing legacy notice only, nothing new queued"
    for t in (6061.0, 6300.0):
        now[0] = t
        sup.reconcile_persistence()
        assert len(S.read_outbox(home)) == 1, "no second notice"
    hook.unlink()
    now[0] = 6400.0
    sup.reconcile_persistence()
    notices = S.read_outbox(home)
    assert read_status(home)["status"] == "synced"
    assert len(notices) == 2 and notices[1]["id"] == "persistence-legacy-sent-recovered"


def test_persist_after_a_blocked_sync_touches_nothing(home, tmp_path):
    """requirement 7h: unmanaged dirt blocks the sync; the following persist touches no git and leaves the
    recorded step and error untouched."""
    root, remote, repo = make_repo(tmp_path)
    (repo / "unrelated").write_text("dirty")
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    sup = S.Supervisor(home=home, repo=str(repo), poster=lambda *a: None)
    initial = head(repo)
    sup.sync_repo_before()
    st = read_status(home)
    assert st["status"] == "blocked" and st["error"].startswith("dirty: ")
    calls = []
    real_git = sup._git
    sup._git = lambda *a, **kw: (calls.append(a), real_git(*a, **kw))[1]
    assert sup.persist(1) is False
    assert calls == [], f"persist after a blocked sync must not touch git: {calls}"
    assert read_status(home)["error"] == st["error"]
    assert head(repo) == initial and (repo / "unrelated").read_text() == "dirty"


def test_every_push_is_preceded_by_its_own_fetch_and_no_destructive_command_is_issued(home, tmp_path):
    """requirement 7i: the fetch (and any rebase) always sit immediately before the push, after the commit;
    every push/fetch/rebase is argument-exact; nothing destructive is ever issued."""
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    now = [9000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0], poster=lambda *a: None)
    calls = []
    real_git = sup._git

    def wrapped(*a, **kw):
        calls.append(a)
        return real_git(*a, **kw)
    sup._git = wrapped
    push_other(root, remote, "external", "keep")
    assert sup.persist(1) is True
    hook = reject(remote)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": [], "turn": 2}))
    assert sup.persist(2) is False
    hook.unlink()
    now[0] += 61
    assert sup.persist(3) is True
    names = [c[0] for c in calls]
    for c in calls:
        assert c[0] in {"fetch", "rebase", "push", "rev-parse", "merge-base", "status", "add", "diff", "commit"}, c
        assert not any(str(a).startswith("--force") or a in ("-f", "--hard") for a in c), c
        if c[0] == "push":
            assert tuple(a for a in c if a != "-q") == ("push", "origin", "HEAD"), c
        if c[0] == "fetch":
            assert tuple(a for a in c if a != "-q") == ("fetch", "origin"), c
        if c[0] == "rebase":
            assert c == ("rebase", "--abort") or (len(c) == 3 and c[2].startswith("origin/")), c
    pushes = [i for i, nm in enumerate(names) if nm == "push"]
    assert pushes
    start = 0
    for p in pushes:
        window = names[start:p]
        fetches = [start + i for i, nm in enumerate(window) if nm == "fetch"]
        assert fetches, f"push without a fresh fetch since the previous push: {names}"
        between = set(names[fetches[-1] + 1:p])
        assert between <= {"rev-parse", "merge-base", "rebase", "status"}, names
        start = p + 1


def test_rebase_in_progress_blocks_both_entry_points_before_any_git_call(tmp_path):
    """requirement 7j: an unfinished rebase (either metadata directory) blocks each entry point before any git
    call, on a fresh supervisor with no preceding blocked call."""
    for marker in ("rebase-merge", "rebase-apply"):
        for entry in ("sync", "persist"):
            root, remote, repo = make_repo(tmp_path)
            h = str(Path(tmp_path) / f"home-{marker}-{entry}")
            sup = S.Supervisor(home=h, repo=str(repo), poster=lambda *a: None)
            S.write_text(os.path.join(h, "state.json"), json.dumps({"tracks": []}))
            (repo / ".git" / marker).mkdir()
            calls = []
            real_git = sup._git
            sup._git = lambda *a, **kw: (calls.append(a), real_git(*a, **kw))[1]
            if entry == "sync":
                sup.sync_repo_before()
            else:
                assert sup.persist(1) is False
            assert calls == [], f"{entry} must not touch git during a rebase ({marker}): {calls}"
            st = read_status(h)
            assert st["status"] == "blocked" and st["error"].startswith("rebase: rebase in progress")


def test_rebase_abort_failure_blocks_with_both_outputs_and_builds_on_nothing(home, tmp_path):
    """requirement 7k: a rebase whose abort also fails blocks with both outputs in the detail, issues no push,
    and leaves metadata the next call refuses to build on."""
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    hook = reject(remote)
    now = [7000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0], poster=lambda *a: None)
    assert sup.persist(1) is False
    local = head(repo)
    hook.unlink()
    push_other(root, remote, "external", "keep")
    real = sup._git

    def fake(*args, **kw):
        if args and args[0] == "rebase":
            if "--abort" in args:
                return subprocess.CompletedProcess(args, 1, "", "fake abort failure")
            return subprocess.CompletedProcess(args, 1, "", "CONFLICT (fake) could not apply")
        return real(*args, **kw)
    now[0] += 61
    with patch.object(sup, "_git", fake):
        assert sup.persist(2) is False
    st = read_status(home)
    assert st["status"] == "blocked" and st["error"].startswith("rebase: ")
    assert "CONFLICT (fake)" in st["error"] and "abort: fake abort failure" in st["error"]
    assert head(repo) == local
    (repo / ".git" / "rebase-merge").mkdir()  # the metadata a failed abort leaves behind
    calls = []
    sup._git = lambda *a, **kw: (calls.append(a), real(*a, **kw))[1]
    assert sup.persist(3) is False
    assert calls == []
    assert read_status(home)["status"] == "blocked"


def test_a_failed_attempt_crossing_the_threshold_queues_its_notice_without_a_tick(home, tmp_path):
    """requirement 7l: the failed attempt itself crossing the five-minute threshold queues the notice, with no
    tick needed, carrying the current (different) error."""
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    hook = reject(remote)
    now = [8000.0]
    sup = S.Supervisor(home=home, repo=str(repo), clock=lambda: now[0],
                       config={"dev_channel": "C_FIXTURE"}, poster=lambda *a: None)
    assert sup.persist(1) is False and S.read_outbox(home) == []
    first_error = read_status(home)["error"]
    missing = remote.parent / "missing.git"
    git(repo, "remote", "set-url", "origin", str(missing))
    now[0] = 8300.0
    assert sup.persist(2) is False
    st = read_status(home)
    assert st["error"].startswith("fetch: ") and st["error"] != first_error and "missing.git" in st["error"]
    notices = S.read_outbox(home)
    assert len(notices) == 1
    assert notices[0]["text"].startswith("persistence pending for 5 min: fetch: ") and "missing.git" in notices[0]["text"]
    assert st["notice_at"] == 8300.0 and st["failures"] == 2


def test_fast_forward_sync_with_no_unpushed_commits_has_no_failure_episode(home, tmp_path):
    """requirements 1 and 7m: a remote ahead of a clone with no unpushed commits is fast-forwarded, with no
    merge commit and no failure episode."""
    root, remote, repo = make_repo(tmp_path)
    S.write_text(os.path.join(home, "state.json"), json.dumps({"tracks": []}))
    external_clone = push_other(root, remote, "external", "keep")
    external = head(external_clone)
    sup = S.Supervisor(home=home, repo=str(repo), poster=lambda *a: None)
    sup.sync_repo_before()
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head(repo) == external == git(repo, "rev-parse", f"origin/{branch}").stdout.strip(), "not fast-forwarded"
    assert (repo / "external").read_text() == "keep"
    assert git(repo, "rev-list", "--merges", "HEAD").stdout.strip() == ""
    assert not os.path.exists(os.path.join(home, "logs", "persistence.json")) or \
        read_status(home).get("status") not in ("pending", "blocked")
    assert S.read_outbox(home) == []
    assert sup.persist(1) is True
    assert head(remote) == head(repo)
