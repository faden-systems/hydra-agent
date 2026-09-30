"""Exercise bootstrap config writes only in temporary homes; never provision a host."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from conftest import ROOT
from supervisor import Supervisor

SCRIPT = Path(ROOT) / "setup/manager-vm.sh"


def configure(tmp_path):
    home = tmp_path / "manager"
    home.mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", "-eu", "-o", "pipefail", "-c", '''
source "$SCRIPT"
MANAGER="$T/manager"; REPOS="$T/repos"
# Privilege boundary only: tests already run as the temporary home's owner.
as_hydra() { "$@"; }
configure_manager_repo
'''],
        env={**os.environ, "SCRIPT": str(SCRIPT), "T": str(tmp_path)},
        capture_output=True, text=True, timeout=30,
    )
    return result, home / "config.json"


def test_absent_config_defaults_to_faden_and_supervisor_reads_it(tmp_path):
    result, config = configure(tmp_path)
    assert result.returncode == 0, result.stderr
    repo = str(tmp_path / "repos" / "faden")
    assert json.loads(config.read_text()) == {"repo": repo}
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.stat().st_uid == os.getuid()
    # Constructor only: no turns, engines, commits or clone writes.
    assert str(Supervisor(home=str(config.parent)).repo) == repo


def test_missing_repo_preserves_all_other_keys_and_replaces_atomically(tmp_path):
    home = tmp_path / "manager"
    home.mkdir()
    config = home / "config.json"
    original = {"dev_channel": "C_TEST", "engines": {"codex": {"bin": "/custom"}},
                "extra": [1, {"enabled": False}]}
    config.write_text(json.dumps(original))
    with config.open() as old_file:
        result, _ = configure(tmp_path)
        assert result.returncode == 0, result.stderr
        assert json.load(old_file) == original  # old inode was not truncated
    assert json.loads(config.read_text()) == {**original, "repo": str(tmp_path / "repos/faden")}
    assert list(home.glob(".config.json.*")) == []


@pytest.mark.parametrize("repo", ["/custom/repo", "", None])
def test_existing_repo_key_and_reruns_are_byte_for_byte_noops(tmp_path, repo):
    home = tmp_path / "manager"
    home.mkdir()
    config = home / "config.json"
    content = json.dumps({"repo": repo, "unknown": {"keep": True}}, indent=4) + "\n"
    config.write_text(content)
    before = config.stat()
    for _ in range(2):
        result, _ = configure(tmp_path)
        assert result.returncode == 0, result.stderr
        assert config.read_text() == content
        assert config.stat().st_ino == before.st_ino
        assert config.stat().st_mtime_ns == before.st_mtime_ns


def test_generated_config_is_unchanged_on_rerun(tmp_path):
    result, config = configure(tmp_path)
    assert result.returncode == 0, result.stderr
    before = config.stat()
    contents = config.read_bytes()
    result, _ = configure(tmp_path)
    assert result.returncode == 0, result.stderr
    assert config.read_bytes() == contents
    assert config.stat().st_ino == before.st_ino
    assert config.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("content", ["not json", "[]", "null"])
def test_invalid_config_fails_without_overwriting(tmp_path, content):
    home = tmp_path / "manager"
    home.mkdir()
    config = home / "config.json"
    config.write_text(content)
    result, _ = configure(tmp_path)
    assert result.returncode == 1, result.stderr
    assert config.read_text() == content
    assert list(home.glob(".config.json.*")) == []


def test_symlink_config_is_rejected_without_touching_target(tmp_path):
    home = tmp_path / "manager"
    home.mkdir()
    target = tmp_path / "target.json"
    target.write_text("{}")
    (home / "config.json").symlink_to(target)
    result, config = configure(tmp_path)
    assert result.returncode == 1, result.stderr
    assert config.is_symlink()
    assert target.read_text() == "{}"
