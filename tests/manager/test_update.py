"""`hydra update` against a temp hydra-agent clone: pull main, redeploy manager/ into HYDRA_APP, refresh the AGENTS.md
link and the CLAUDE.md copy, restart both services through a recording `systemctl` found on PATH, print the commit."""
import json
import os
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
    os.chmod(os.path.join(fakebin, "systemctl"), 0o755)
    return {"bare": bare, "src": src, "clone": clone, "head": head, "app": app, "home": home, "fakebin": fakebin}


def run_update(w, **env):
    return subprocess.run([sys.executable, HYDRA, "update"], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "HYDRA_HOME": w["home"], "HYDRA_REPO": w["clone"], "HYDRA_APP": w["app"],
                               "PATH": w["fakebin"] + os.pathsep + os.environ["PATH"], **env})


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
