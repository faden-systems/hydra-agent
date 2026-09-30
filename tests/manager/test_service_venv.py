"""Installed services must use the environment populated by bootstrap."""
import configparser
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_units_use_runtime_venv_and_bounded_starts():
    for name, entry in (("hydra-manager", "supervisor"), ("hydra-bridge", "bridge")):
        unit = configparser.ConfigParser(interpolation=None, strict=False)
        unit.read(ROOT / "manager/systemd" / (name + ".service"))
        assert unit["Service"]["ExecStart"].split()[:2] == [
            "/srv/hydra/manager/venv/bin/python",
            f"/srv/hydra/manager/app/manager/{entry}.py",
        ]
        assert unit["Unit"]["StartLimitIntervalSec"] == "300"
        assert unit["Unit"]["StartLimitBurst"] == "3"


def test_bootstrap_installs_both_packages_into_runtime_venv(tmp_path):
    manager = tmp_path / "manager"
    bin_dir = manager / "venv/bin"
    bin_dir.mkdir(parents=True)
    calls = tmp_path / "calls"
    python = bin_dir / "python"
    python.write_text('#!/bin/bash\nif [[ $1 == -c ]]; then exit 1; fi\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    python.chmod(0o755)
    # Unavoidable privileged/network boundaries mocked; all file copies and shell flow are real.
    script = r'''
set -euo pipefail
source "$1/setup/manager-vm.sh"
MANAGER=$2/manager
STATE=$2/state
UNIT_DIR=$2/units
HYDRA_MANAGER_SRC=$1/manager
export CALLS=$2/calls
mkdir -p "$STATE" "$UNIT_DIR"
python3.12() { printf 'venv %s\n' "$*" >> "$CALLS"; }
chown() { printf 'chown %s\n' "$*" >> "$CALLS"; }
install() { :; }
ln() { :; }
systemctl() { printf 'systemctl %s\n' "$*" >> "$CALLS"; }
install_manager
'''
    result = subprocess.run(["bash", "-c", script, "test", str(ROOT), str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text().splitlines()
    assert f"venv -m venv {manager}/venv" in lines
    pip = next(line for line in lines if line.startswith("-m pip install"))
    assert "slack_bolt" in pip.split()
    assert "slack_sdk" in pip.split()
    assert f"chown -R hydra:hydra {manager}/venv" in lines
    assert not any("--now" in line or "restart" in line for line in lines)
