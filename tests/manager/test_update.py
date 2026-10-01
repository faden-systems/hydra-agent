"""`hydra update` against a temp hydra-agent clone: pull main, redeploy manager/ into HYDRA_APP, refresh the AGENTS.md
link and the CLAUDE.md copy, restart both services through a recording `systemctl` found on PATH, print the commit.
Since loops/b6.md the deploy is the root run: `run_update` is root by default (HYDRA_FAKE_UID=0, the acceptance
harness's injection) with a `sudo` on PATH that drops its options and runs the git command; the privilege-split
cases at the end override the uid and record what `sudo` and `git` were asked to do."""
import json
import os
import shutil
import subprocess
import sys

from conftest import MANAGER

HYDRA = os.path.join(MANAGER, "hydra")


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


def make_world(tmp_path):
    bare = str(tmp_path / "bare.git"); subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    src = str(tmp_path / "src"); subprocess.run(["git", "clone", "-q", bare, src], check=True)
    os.makedirs(os.path.join(src, "manager", "systemd"))
    open(os.path.join(src, "manager", "CLAUDE.md"), "w").write("rules v1\n")
    open(os.path.join(src, "manager", "supervisor.py"), "w").write("# v1\n")
    open(os.path.join(src, "manager", "hydra"), "w").write("#!/bin/sh\n")
    git(src, "add", "-A"); git(src, "commit", "-qm", "v1"); git(src, "push", "-q", "-u", "origin", "HEAD:main")
    clone = str(tmp_path / "clone"); subprocess.run(["git", "clone", "-q", "-b", "main", bare, clone], check=True)
    open(os.path.join(src, "manager", "CLAUDE.md"), "w").write("rules v2\n")
    open(os.path.join(src, "manager", "models.json"), "w").write("{}\n")
    git(src, "add", "-A"); git(src, "commit", "-qm", "v2"); git(src, "push", "-q", "origin", "HEAD:main")
    head = git(src, "rev-parse", "HEAD")
    app = str(tmp_path / "deploy" / "manager"); os.makedirs(app)
    open(os.path.join(app, "CLAUDE.md"), "w").write("rules v1\n"); open(os.path.join(app, "stale.pyc"), "w").write("")
    home = str(tmp_path / "home"); os.makedirs(os.path.join(home, "inbox"))
    open(os.path.join(home, "CLAUDE.md"), "w").write("rules v1\n")
    fakebin = str(tmp_path / "bin"); os.makedirs(fakebin)
    open(os.path.join(fakebin, "systemctl"), "w").write("#!/usr/bin/env python3\nimport sys\nopen(%r,'a').write(' '.join(sys.argv[1:])+'\\n')\n" % os.path.join(fakebin, "calls.log"))
    open(os.path.join(fakebin, "sudo"), "w").write("#!/usr/bin/env python3\nimport subprocess, sys\na = sys.argv[1:]\n"
                                                  "while a and a[0].startswith('-'):\n    a = a[2:] if a[0] == '-u' else a[1:]\n"
                                                  "sys.exit(subprocess.call(a) if a else 0)\n")
    for tool in ("systemctl", "sudo"):
        os.chmod(os.path.join(fakebin, tool), 0o755)
    return {"bare": bare, "src": src, "clone": clone, "head": head, "app": app, "home": home, "fakebin": fakebin}


def run_update(w, **env):
    return subprocess.run([sys.executable, HYDRA, "update"], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "HYDRA_HOME": w["home"], "HYDRA_REPO": w["clone"], "HYDRA_APP": w["app"],
                               "PATH": w["fakebin"] + os.pathsep + os.environ["PATH"], "HYDRA_FAKE_UID": "0", **env})


def test_update_pulls_redeploys_relinks_restarts_and_prints_the_commit(tmp_path):
    w = make_world(tmp_path)
    r = run_update(w)
    assert r.returncode == 0, r.stdout + r.stderr
    assert git(w["clone"], "rev-parse", "HEAD") == w["head"], "the clone is fast-forwarded to origin/main"
    assert open(os.path.join(w["app"], "CLAUDE.md")).read() == "rules v2\n"
    assert os.path.exists(os.path.join(w["app"], "supervisor.py")) and os.path.exists(os.path.join(w["app"], "models.json"))
    assert not os.path.exists(os.path.join(w["app"], "stale.pyc")), "the deploy dir is replaced, not merged"
    assert os.access(os.path.join(w["app"], "hydra"), os.X_OK)
    assert not os.path.exists(w["app"] + ".new") and not os.path.exists(w["app"] + ".old")
    agents = os.path.join(w["home"], "AGENTS.md")
    assert os.path.islink(agents) and os.path.realpath(agents) == os.path.realpath(os.path.join(w["app"], "CLAUDE.md"))
    assert open(os.path.join(w["home"], "CLAUDE.md")).read() == "rules v2\n", "Claude's copy of the rules is refreshed too"
    assert open(os.path.join(w["fakebin"], "calls.log")).read().splitlines() == ["restart hydra-bridge hydra-manager"]
    assert r.stdout.startswith(f"updated to {w['head'][:12]}"), r.stdout


def test_update_relative_link_matches_the_installed_layout(tmp_path):
    w = make_world(tmp_path)
    w["app"] = os.path.join(w["home"], "app", "manager"); os.makedirs(w["app"])
    assert run_update(w).returncode == 0
    assert os.readlink(os.path.join(w["home"], "AGENTS.md")) == "app/manager/CLAUDE.md"


def test_update_is_idempotent_and_refuses_a_diverged_clone(tmp_path):
    w = make_world(tmp_path)
    assert run_update(w).returncode == 0
    r = run_update(w)
    assert r.returncode == 0 and w["head"][:12] in r.stdout, "nothing new: still succeeds and restarts"
    assert open(os.path.join(w["fakebin"], "calls.log")).read().count("restart") == 2
    open(os.path.join(w["clone"], "local.txt"), "w").write("x\n"); git(w["clone"], "add", "-A"); git(w["clone"], "commit", "-qm", "local")
    open(os.path.join(w["src"], "manager", "CLAUDE.md"), "w").write("rules v3\n"); git(w["src"], "commit", "-qam", "v3"); git(w["src"], "push", "-q", "origin", "HEAD:main")
    r = run_update(w)
    assert r.returncode == 1 and "fast-forward" in r.stderr
    assert open(os.path.join(w["app"], "CLAUDE.md")).read() == "rules v2\n", "a refused update deploys nothing"
    assert open(os.path.join(w["fakebin"], "calls.log")).read().count("restart") == 2, "and restarts nothing"


def test_update_reports_a_failed_restart(tmp_path):
    w = make_world(tmp_path)
    open(os.path.join(w["fakebin"], "systemctl"), "w").write("#!/bin/sh\necho 'Failed to restart' >&2\nexit 1\n")
    r = run_update(w)
    assert r.returncode == 1 and "systemctl restart failed" in r.stderr
    assert open(os.path.join(w["app"], "CLAUDE.md")).read() == "rules v2\n"


def test_update_needs_a_clone(tmp_path):
    w = make_world(tmp_path)
    r = run_update(w, HYDRA_REPO=str(tmp_path / "nope"))
    assert r.returncode == 1 and "not a git clone" in r.stderr and not os.path.exists(os.path.join(w["fakebin"], "calls.log"))


def test_engine_json_survives_update(tmp_path):
    w = make_world(tmp_path)
    open(os.path.join(w["home"], "engine"), "w").write(json.dumps({"acc": "claude-l", "model": "claude-sonnet-5"}))
    assert run_update(w).returncode == 0
    assert json.load(open(os.path.join(w["home"], "engine"))) == {"acc": "claude-l", "model": "claude-sonnet-5"}


# ----------------------------------------------------------------------------------------------- the privilege split (loops/b6.md)

import re  # noqa: E402


def fake_sudo_and_git(fakebin):
    """A `sudo` that records its arguments and runs the git part with FAKE_SUDO_USER set; a `git` that records the
    user it ran as, then runs the real git."""
    real_git = shutil.which("git")
    calls = os.path.join(fakebin, "calls.log")
    open(os.path.join(fakebin, "sudo"), "w").write(
        "#!/usr/bin/env python3\nimport os, sys, subprocess\n"
        f"open({calls!r},'a').write('sudo ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "a = sys.argv[1:]\nuser = a[a.index('-u') + 1] if '-u' in a else 'root'\n"
        "env = dict(os.environ, FAKE_SUDO_USER=user)\n"
        "i = next((k for k, x in enumerate(a) if x == 'git'), None)\n"
        "sys.exit(subprocess.call(a[i:], env=env) if i is not None else 0)\n")
    open(os.path.join(fakebin, "git"), "w").write(
        "#!/usr/bin/env python3\nimport os, sys\n"
        f"open({calls!r},'a').write('git[user=' + os.environ.get('FAKE_SUDO_USER', '-') + '] ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        f"os.execv({real_git!r}, [{real_git!r}] + sys.argv[1:])\n")
    for tool in ("sudo", "git"):
        os.chmod(os.path.join(fakebin, tool), 0o755)
    return calls


def git_lines(log):
    return [(m.group(1), m.group(2)) for m in re.finditer(r"^git\[user=([^\]]*)\] (.*)$", log, re.M)]


def test_update_as_root_runs_every_git_step_as_hydra_then_deploys_and_restarts(tmp_path):
    w = make_world(tmp_path)
    calls = fake_sudo_and_git(w["fakebin"])
    r = run_update(w, HYDRA_FAKE_UID="0")
    assert r.returncode == 0, r.stdout + r.stderr
    log = open(calls).read()
    g = git_lines(log)
    assert len(g) >= 2 and all(u == "hydra" for u, _ in g), ("every git step runs as hydra", log)
    assert all(l.startswith("sudo -n -u hydra -H git -C ") for l in log.splitlines() if l.startswith("sudo ")), log
    assert any("fetch" in a for _, a in g) and any("merge" in a for _, a in g)
    assert git(w["clone"], "rev-parse", "HEAD") == w["head"]
    assert open(os.path.join(w["app"], "CLAUDE.md")).read() == "rules v2\n"
    assert os.path.islink(os.path.join(w["home"], "AGENTS.md")) and open(os.path.join(w["home"], "AGENTS.md")).read() == "rules v2\n"
    assert log.splitlines()[-1] == "restart hydra-bridge hydra-manager", ("the restart is the last step", log)
    assert r.stdout.startswith(f"updated to {w['head'][:12]}")


def test_update_unprivileged_stops_after_git_and_prints_the_root_command(tmp_path):
    w = make_world(tmp_path)
    calls = fake_sudo_and_git(w["fakebin"])
    open(os.path.join(w["app"], "sentinel"), "w").write("untouched\n")
    r = run_update(w, HYDRA_FAKE_UID="1001")
    assert r.returncode == 0, r.stdout + r.stderr
    log = open(calls).read()
    g = git_lines(log)
    assert len(g) >= 2 and all(u == "-" for u, _ in g), ("the git steps run directly", log)
    assert "sudo" not in log.replace("[user=-]", "") and "systemctl" not in log, log
    assert git(w["clone"], "rev-parse", "HEAD") == w["head"], "the clone is updated"
    assert open(os.path.join(w["app"], "CLAUDE.md")).read() == "rules v1\n" and os.path.exists(os.path.join(w["app"], "sentinel")), "app/ is untouched"
    assert open(os.path.join(w["home"], "CLAUDE.md")).read() == "rules v1\n" and not os.path.lexists(os.path.join(w["home"], "AGENTS.md"))
    assert re.search(r"sudo(\s+-n)?\s+hydra\s+update", r.stdout), r.stdout
    assert w["head"][:12] in r.stdout


def test_update_as_the_real_unprivileged_user_also_stops_after_git(tmp_path):
    """Without HYDRA_FAKE_UID the real uid decides: this test user is not root."""
    w = make_world(tmp_path)
    calls = fake_sudo_and_git(w["fakebin"])
    r = run_update(w, HYDRA_FAKE_UID="")
    assert r.returncode == 0, r.stdout + r.stderr
    log = open(calls).read()
    assert git_lines(log) and all(u == "-" for u, _ in git_lines(log)) and "sudo" not in log.replace("[user=-]", "")
    assert open(os.path.join(w["app"], "CLAUDE.md")).read() == "rules v1\n" and "restart" not in log
    assert "sudo -n hydra update" in r.stdout


def test_update_as_root_reports_a_failed_git_step_and_restarts_nothing(tmp_path):
    w = make_world(tmp_path)
    calls = fake_sudo_and_git(w["fakebin"])
    shutil.rmtree(w["bare"])
    r = run_update(w, HYDRA_FAKE_UID="0")
    assert r.returncode == 1 and "fetch failed" in r.stderr, r.stdout + r.stderr
    log = open(calls).read()
    assert "systemctl" not in log and open(os.path.join(w["app"], "CLAUDE.md")).read() == "rules v1\n"


def test_update_rejects_a_malformed_fake_uid(tmp_path):
    w = make_world(tmp_path)
    r = run_update(w, HYDRA_FAKE_UID="root")
    assert r.returncode == 2 and "HYDRA_FAKE_UID" in r.stderr
