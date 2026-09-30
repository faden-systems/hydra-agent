"""The entry points in dry mode and the unit files."""
import os
import subprocess
import sys

from conftest import MANAGER, ROOT, make_home


def test_supervisor_once_dry(tmp_path):
    h = make_home(str(tmp_path / "h"))
    r = subprocess.run([sys.executable, os.path.join(MANAGER, "supervisor.py"), "--once"],
                       env={**os.environ, "HYDRA_HOME": h, "HYDRA_NO_REEXEC": "1"}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "nothing to do" in r.stdout
    assert os.path.exists(os.path.join(h, "logs", "heartbeat"))


def test_bridge_check(tmp_path):
    h = make_home(str(tmp_path / "h"))
    env = {**os.environ, "HYDRA_HOME": h, "HYDRA_NO_REEXEC": "1"}
    r = subprocess.run([sys.executable, os.path.join(MANAGER, "bridge.py"), "--check"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "missing" in r.stdout
    open(os.path.join(h, "allowlist.json"), "w").write("{}")
    open(os.path.join(h, "credentials", "slack.env"), "w").write("")
    r = subprocess.run([sys.executable, os.path.join(MANAGER, "bridge.py"), "--check"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "xoxb" not in r.stdout


def test_units_name_the_entry_points():
    for unit, entry in (("hydra-manager.service", "supervisor.py"), ("hydra-bridge.service", "bridge.py")):
        text = open(os.path.join(MANAGER, "systemd", unit)).read()
        exec_line = next(l for l in text.splitlines() if l.startswith("ExecStart="))
        assert exec_line.endswith(f"/manager/{entry}")
        assert "User=hydra" in text and "Restart=always" in text and "HYDRA_HOME=/srv/hydra/manager" in text
        assert os.path.exists(os.path.join(MANAGER, entry))


def test_claude_md_rules():
    text = open(os.path.join(MANAGER, "CLAUDE.md")).read()
    for needle in ("---HANDOFF---", "state.json", "MANAGER-HANDOFF.md", "assignee:", "instructs", "screenshot",
                   "four rounds", "token"):
        assert needle in text
