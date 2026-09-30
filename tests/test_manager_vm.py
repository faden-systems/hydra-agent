"""Portable unit/contract tests. NOT Ubuntu integration; never invoke bootstrap main."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'setup/manager-vm.sh'


class ManagerVMTests(unittest.TestCase):
    def run_shell(self, body):
        self.assertTrue(SCRIPT.is_file(), 'manager bootstrap not implemented')
        with tempfile.TemporaryDirectory() as tmp:
            prelude = f'''source "{SCRIPT}"
ROOT="$TEST_TMP"
MANAGER="$ROOT/manager"
REPOS="$ROOT/repos"
TOOLS="$ROOT/tools"
UNIT="$ROOT/hydra-manager.service"
STATE="$ROOT/versions"
mkdir -p "$STATE" "$MANAGER" "$REPOS"
chown() {{ :; }}
'''
            return subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', prelude + body],
                                  env={**os.environ, 'TEST_TMP': tmp}, text=True,
                                  capture_output=True)

    def ok(self, body):
        result = self.run_shell(body)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_source_has_no_side_effects(self):
        self.ok('test ! -e "$UNIT"; test ! -e "$MANAGER/supervisor.sh"')

    def test_platform_guard_rejects_mac_before_root_check(self):
        out = self.run_shell('uname() { printf Darwin; }; id() { exit 88; }; check_platform')
        self.assertEqual(out.returncode, 1)
        self.assertIn('Ubuntu 24.04', out.stderr)

    def test_root_guard(self):
        out = self.run_shell('id() { printf 501; }; check_root')
        self.assertEqual(out.returncode, 1)
        self.assertIn('root', out.stderr)

    def test_state_empty_private_then_preserved(self):
        self.ok('''prepare_layout
 test -z "$(command ls -A "$MANAGER/.claude")"
 test -z "$(command ls -A "$MANAGER/credentials")"
 printf preserved > "$MANAGER/credentials/test"
 printf login > "$MANAGER/.claude/test"
 prepare_layout
 test "$(< "$MANAGER/credentials/test")" = preserved
 test "$(< "$MANAGER/.claude/test")" = login
 python3 -c 'import os,stat; from pathlib import Path; p=Path(os.environ["TEST_TMP"])/"manager"; assert all(stat.S_IMODE((p/n).stat().st_mode)==0o700 for n in ("credentials", ".claude"))'
''')

    def test_state_symlink_rejected(self):
        result = self.run_shell('ln -s "$STATE" "$MANAGER/credentials"; prepare_layout')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlink', result.stderr)

    def test_service_heartbeat_and_replacement_preserved(self):
        self.ok('''systemctl() { printf '%s\n' "$*" >> "$ROOT/systemctl"; }
 install_service
 bash -n "$MANAGER/supervisor.sh"
 python3 -c 'import os; from pathlib import Path; p=Path(os.environ["TEST_TMP"]); u=(p/"hydra-manager.service").read_text(); s=(p/"manager/supervisor.sh").read_text(); assert "User=hydra" in u and "NoNewPrivileges=true" in u and "StandardOutput=journal" in u and "CLAUDE_CONFIG_DIR=" in u; assert "sleep 300" in s and "heartbeat" in s; assert "enable --now hydra-manager.service" in (p/"systemctl").read_text()'
 printf '#!/bin/bash\n# future supervisor\n' > "$MANAGER/supervisor.sh"
 install_service
 test "$(command wc -l < "$MANAGER/supervisor.sh" | tr -d ' ')" = 2
''')

    def test_unauthenticated_private_clone_pending_public_cloned(self):
        out = self.ok('''as_hydra() {
 printf '%s\n' "$*" >> "$ROOT/calls"
 case "$*" in *'auth status'*) return 1;; *'git '*'clone '*) mkdir -p "${@: -1}/.git";; esac
 }
 clone_repositories || code=$?
 test "${code:-0}" = 2
 test -d "$REPOS/hydra-agent/.git"
 test ! -e "$REPOS/faden"
''')
        self.assertIn('PENDING', out)

    def test_authenticated_clone_uses_gh_helper_noninteractive(self):
        self.ok('''as_hydra() { printf '%s\n' "$*" >> "$ROOT/calls"; case "$*" in *'clone '*) mkdir -p "${@: -1}/.git";; esac; }
 clone_repositories
 python3 -c 'import os; from pathlib import Path; s=(Path(os.environ["TEST_TMP"])/"calls").read_text(); assert "credential.helper=!/usr/bin/gh auth git-credential" in s; assert "https://github.com/faden-systems/faden.git" in s'
''')

    def test_existing_dirty_clones_not_touched(self):
        self.ok('''mkdir -p "$REPOS/faden/.git" "$REPOS/hydra-agent/.git"
 printf dirty > "$REPOS/faden/work"
 as_hydra() { return 99; }
 clone_repositories
 test "$(< "$REPOS/faden/work")" = dirty
''')

    def test_existing_nonrepo_not_overwritten(self):
        result = self.run_shell('mkdir "$REPOS/faden"; clone_repositories')
        self.assertEqual(result.returncode, 1)

    def test_public_clone_error_not_auth_pending(self):
        result = self.run_shell('as_hydra() { return 1; }; clone_repositories')
        self.assertEqual(result.returncode, 1)

    def test_apt_only_installs_missing_packages(self):
        self.ok('''dpkg-query() { case "$*" in *present*) printf 'install ok installed';; *) return 1;; esac; }
 apt-get() { printf '%s\n' "$*" >> "$ROOT/apt"; }
 apt_missing present missing
 test "$(< "$ROOT/apt")" = 'install -y --no-upgrade missing'
''')

    def test_resolved_npm_version_is_reused(self):
        self.ok('''npm() { case "$1" in view) printf '1.2.3\n';; *) printf '%s\n' "$*" >> "$ROOT/npm";; esac; }
 install_npm_tool '@openai/codex' codex
 npm() { case "$1" in view) return 99;; *) printf '%s\n' "$*" >> "$ROOT/npm";; esac; }
 install_npm_tool '@openai/codex' codex
 test "$(< "$STATE/codex.version")" = 1.2.3
 test "$(command wc -l < "$ROOT/npm" | tr -d ' ')" = 1
''')

    def test_failed_npm_install_retries_same_version(self):
        self.ok('''npm() { case "$1" in view) printf '1.2.3\\n';; *) return 23;; esac; }
 # Use a fresh errexit subprocess, as production does, rather than an if/|| function call.
 export -f npm
 export STATE TOOLS
 bash -eu -c 'source "$1"; STATE="$2"; TOOLS="$3"; install_npm_tool @openai/codex codex' bash "''' + str(SCRIPT) + '''" "$STATE" "$TOOLS" && exit 99
 test ! -e "$STATE/codex.installed"
 test "$(< "$STATE/codex.version")" = 1.2.3
 npm() { case "$1" in view) return 99;; *) :;; esac; }
 install_npm_tool @openai/codex codex
 test -f "$STATE/codex.installed"
''')

    def test_heartbeat_executes_only_log_and_sleep(self):
        out = self.ok('''systemctl() { :; }
 install_service
 date() { printf 'fixture-time'; }
 sleep() { test "$1" = 300 || exit 99; exit 0; }
 export -f date sleep
 bash "$MANAGER/supervisor.sh"
''')
        self.assertEqual(out, 'hydra-manager heartbeat fixture-time\n')

    def test_hydra_command_environment_is_clean(self):
        out = self.ok('''runuser() { printf '%s\\n' "$*"; }
 export GH_TOKEN=should-not-cross-root-boundary
 as_hydra /usr/bin/gh auth status
''')
        self.assertIn('-u hydra -- env -i HOME=/home/hydra', out)
        self.assertIn('GIT_TERMINAL_PROMPT=0', out)
        self.assertNotIn('should-not-cross-root-boundary', out)

    def test_user_refuses_uid_zero(self):
        result = self.run_shell('id() { printf 0; }; prepare_user')
        self.assertEqual(result.returncode, 1)
        self.assertIn('UID 0', result.stderr)

    def test_npm_config_paths_are_distinct(self):
        # npm aborts before resolving config when user/global paths both equal /dev/null.
        self.assertNotIn('npm_config_userconfig=/dev/null npm_config_globalconfig=/dev/null', SCRIPT.read_text())

    def test_contract(self):
        self.assertTrue(SCRIPT.is_file(), 'manager bootstrap not implemented')
        s = SCRIPT.read_text()
        for token in ('fonts-inter', 'fonts-noto-core', 'fonts-noto-color-emoji', 'python3.12-venv',
                      'rclone', 'tmux', 'tailscale', 'https://nodejs.org/dist/',
                      'https://pkgs.tailscale.com/stable/ubuntu/', 'SHASUMS256.txt',
                      'GIT_TERMINAL_PROMPT=0', 'env -i', 'usermod -G', '/bin/bash',
                      '!ALL', 'install-deps', 'BASH_SOURCE[0]'):
            self.assertIn(token, s)
        for forbidden in ('curl |', 'apt-get upgrade', 'tailscale up', 'gh auth login --',
                          'pip install hermes', 'npm install -g openclaw'):
            self.assertNotIn(forbidden, s)
        main = s[s.index('main() {'):]
        self.assertLess(main.index('check_platform'), main.index('prepare_layout'))
        self.assertLess(main.index('check_root'), main.index('prepare_layout'))
        self.assertLess(main.index('install_service'), main.index('clone_repositories'))


if __name__ == '__main__':
    unittest.main()
