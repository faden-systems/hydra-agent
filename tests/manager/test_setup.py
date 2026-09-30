"""setup/manager-vm.sh install_manager, sourced with stubs: never runs main, never touches the system."""
import os
import shutil
import subprocess
import tempfile

from conftest import MANAGER, ROOT

SCRIPT = os.path.join(ROOT, "setup", "manager-vm.sh")

PRELUDE = r'''
source "$SCRIPT"
ROOT="$T"; MANAGER="$ROOT/manager"; REPOS="$ROOT/repos"; STATE="$ROOT/state"; UNIT_DIR="$ROOT/units"
mkdir -p "$STATE" "$MANAGER" "$REPOS/hydra-agent" "$UNIT_DIR"
chown() { :; }
systemctl() { printf '%s\n' "$*" >> "$ROOT/systemctl"; }
ln() { printf 'ln %s\n' "$*" >> "$ROOT/ln"; }
install() { cp -- "${@: -2}"; }
python3.12() { if [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"; printf '#!/bin/sh\nexit 0\n' > "$3/bin/python"; chmod +x "$3/bin/python"; else command python3.12 "$@"; fi; }
'''


def run(body, src=True):
    t = tempfile.mkdtemp()
    if src:
        shutil.copytree(MANAGER, os.path.join(t, "repos", "hydra-agent", "manager"))
    r = subprocess.run(["bash", "-eu", "-o", "pipefail", "-c", PRELUDE + body],
                       env={**os.environ, "SCRIPT": SCRIPT, "T": t}, capture_output=True, text=True, timeout=120)
    return r, t


def test_install_manager_installs_code_rules_venv_units_and_cli():
    r, t = run('''install_manager
test -x "$MANAGER/app/manager/hydra"; test -f "$MANAGER/app/manager/supervisor.py"; test -f "$MANAGER/app/manager/bridge.py"
test -f "$MANAGER/CLAUDE.md"; test -x "$MANAGER/venv/bin/python"
test -f "$UNIT_DIR/hydra-manager.service"; test -f "$UNIT_DIR/hydra-bridge.service"
grep -q 'ExecStart=/usr/bin/python3 /srv/hydra/manager/app/manager/supervisor.py' "$UNIT_DIR/hydra-manager.service"
''')
    assert r.returncode == 0, r.stdout + r.stderr
    assert "units enabled, not (re)started" in r.stdout
    systemctl = open(os.path.join(t, "systemctl")).read()
    assert "daemon-reload" in systemctl and "enable hydra-manager.service hydra-bridge.service" in systemctl
    assert "start" not in systemctl and "restart" not in systemctl
    assert "/usr/local/bin/hydra" in open(os.path.join(t, "ln")).read()


def test_install_manager_is_idempotent_and_keeps_the_venv():
    r, t = run('''install_manager
printf 'keep' > "$MANAGER/venv/marker"
install_manager
test "$(cat "$MANAGER/venv/marker")" = keep
test -f "$MANAGER/app/manager/hydra"
''')
    assert r.returncode == 0, r.stdout + r.stderr


def test_install_manager_pending_without_source():
    r, t = run("HYDRA_MANAGER_SRC=/nonexistent install_manager; test ! -e \"$MANAGER/app\"", src=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PENDING: manager source not found" in r.stdout


def test_main_order_and_no_start():
    s = open(SCRIPT).read()
    main = s[s.index("main() {"):]
    assert main.index("clone_repositories") < main.index("install_manager")
    body = s[s.index("install_manager() {"):s.index("main() {")]
    assert "systemctl start" not in body and "systemctl restart" not in body and "--now" not in body
    assert "prepare_layout" in s and '"$MANAGER/mirror"' in s
