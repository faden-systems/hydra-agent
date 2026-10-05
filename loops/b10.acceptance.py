#!/usr/bin/env python3
"""Exit-owned offline b10 contracts (loops/b10.md). Local bare Git repositories, a controlled clock and a fake
poster only: no GitHub, no Slack, no credentials. `--b7-subset` runs every loops/b7.acceptance.py check except the
two that pin the merge ancestry and the immediate notice pair (requirement 7)."""
import ast
import json
import runpy
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'manager'))
import supervisor as S  # noqa: E402

B7_SUPERSEDED = {'persistence_reconciliation', 'persistence_tick_retry'}
REAL_REJECTION = ("push: //github.com/faden-systems/faden.git\n ! [rejected]        HEAD -> main (fetch first)\n"
                  "error: failed to push some refs to 'https://github.com/faden-systems/faden.git'\n"
                  "hint: Updates were rejected because the remote contains work that you do not\n"
                  "hint: have locally. This is usually caused by another repository pushing to\n"
                  "hint: the same ref. If you want to integrate the remote changes, use\n"
                  "hint: 'git pull' before pushing again.\n"
                  "hint: See the 'Note about fast-forwards' in 'git push --help' for details.")


def git(path, *args, check=True):
    result = subprocess.run(['git', '-C', str(path), '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test',
                             *args], check=False, capture_output=True, text=True)
    assert not check or result.returncode == 0, f'fixture git {args} failed: {result.stderr.strip()[-300:]}'
    return result


def make_repo():
    root = Path(tempfile.mkdtemp(prefix='b10-git-'))
    remote, repo, other = root / 'remote.git', root / 'manager', root / 'other'
    git(root, 'init', '--bare', str(remote))
    git(root, 'clone', str(remote), str(repo))
    (repo / 'seed').write_text('seed')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'seed')
    git(repo, 'push', '-u', 'origin', 'HEAD')
    git(root, 'clone', str(remote), str(other))
    return remote, repo, other


def home():
    p = Path(tempfile.mkdtemp(prefix='b10-home-'))
    for name in ('logs', 'inbox', 'mirror'):
        (p / name).mkdir()
    return str(p)


def write_state(h, value):
    Path(h, 'state.json').write_text(json.dumps(value))


def push_other(other, name, content, message=None):
    """The fixture's second clone commits on top of the remote's current tip (never the supervisor's job)."""
    branch = git(other, 'rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    git(other, 'fetch', '-q', 'origin')
    git(other, 'reset', '-q', '--hard', f'origin/{branch}')
    path = other / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(other, 'add', '-A')
    git(other, 'commit', '-m', message or name)
    git(other, 'push', 'origin', 'HEAD')
    return git(other, 'rev-parse', 'HEAD').stdout.strip()


def reject(remote):
    hook = remote / 'hooks' / 'pre-receive'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o755)
    return hook


def make_sup(h, repo, now):
    return S.Supervisor(home=h, repo=str(repo), clock=lambda: now[0], config={'dev_channel': 'C_FIXTURE'},
                        poster=lambda *a: None)


def tick(sup, git_allowed=True):
    with patch.object(sup, 'compaction_due', return_value=(False, '')), \
            patch.object(sup, '_git', wraps=sup._git) as wrapped:
        result = sup.run_once()
    assert git_allowed or not wrapped.called, 'this tick must not touch git'
    return result


def status(h):
    return json.loads(Path(h, 'logs', 'persistence.json').read_text())


def head(path):
    return git(path, 'rev-parse', 'HEAD').stdout.strip()


def no_merges(remote):
    assert git(remote, 'rev-list', '--merges', '--all').stdout.strip() == '', 'merge commit on the remote'


def one_line(text):
    assert '\n' not in text and 'hint:' not in text, text
    assert len(text) <= 240, text


def constants():
    assert S.PERSISTENCE_NOTICE_AFTER_S == 300, 'PERSISTENCE_NOTICE_AFTER_S must be 300 (requirement 3)'
    assert S.PERSISTENCE_RETRY_AFTER_S == 60, 'the 60-second retry deadline is unchanged (requirement 6)'
    assert callable(getattr(S, 'notice_error_summary', None)), 'notice_error_summary missing (requirement 4)'
    print('constants: ok')


def quiet_recovery():
    """Requirement 7a: a collision lifted within five minutes posts nothing, before, during or after."""
    remote, repo, other = make_repo()
    h = home()
    write_state(h, {'tracks': []})
    hook = reject(remote)
    now = [1000.0]
    sup = make_sup(h, repo, now)
    assert sup.persist(1) is False
    st = status(h)
    assert st['status'] == 'pending' and st['since'] == 1000.0 and st['failures'] == 1 and st['notice_at'] is None, st
    assert st['error'].startswith('push: '), st
    assert S.read_outbox(h) == [], 'a failure must queue nothing'
    now[0] = 1061.0
    tick(sup)
    st = status(h)
    assert st['status'] == 'pending' and st['since'] == 1000.0 and st['failures'] == 2, st
    assert S.read_outbox(h) == []
    hook.unlink()
    now[0] = 1122.0
    tick(sup)
    st = status(h)
    assert st['status'] == 'synced' and st['last_successful_push_sha'] == head(repo) == head(remote), st
    assert st['since'] is None and st['notice_at'] is None and st['failures'] is None, st
    assert S.read_outbox(h) == [], 'a recovery without a notice must queue nothing'
    now[0] = 1200.0
    tick(sup, git_allowed=True)
    assert S.read_outbox(h) == [] and status(h)['status'] == 'synced'
    print('quiet recovery: ok')


def deadline_notice():
    """Requirements 7b and 7e: one notice at or after since+300 (independent of the retry deadline), one recovered
    line after it, nothing else, across a restart."""
    remote, repo, other = make_repo()
    h = home()
    write_state(h, {'tracks': []})
    hook = reject(remote)
    now = [1000.0]
    sup = make_sup(h, repo, now)
    assert sup.persist(1) is False
    episode = status(h)['episode_id']
    assert episode
    for t in (1061.0, 1122.0, 1183.0, 1244.0):
        now[0] = t
        tick(sup)
        assert S.read_outbox(h) == [], f'nothing before five minutes (tick at {t})'
        assert status(h)['since'] == 1000.0 and status(h)['episode_id'] == episode
    now[0] = 1299.0
    tick(sup, git_allowed=False)  # before the retry deadline (1244 + 60) and before the notice deadline
    assert S.read_outbox(h) == []
    now[0] = 1300.0
    tick(sup, git_allowed=False)  # still before the retry deadline: the notice check must not need git
    notices = S.read_outbox(h)
    assert len(notices) == 1, notices
    n = notices[0]
    assert n['channel'] == 'C_FIXTURE' and not n.get('thread_ts'), n
    assert n['id'] == f'persistence-{episode}-notice', n
    one_line(n['text'])
    assert n['text'].startswith("persistence pending for 5 min: push: failed to push some refs to '"), n['text']
    assert episode not in n['text'] and 'rejected' not in n['text'], n['text']
    assert status(h)['notice_at'] == 1300.0, status(h)
    now[0] = 1305.0
    tick(sup)  # retry fails again: no second notice
    assert len(S.read_outbox(h)) == 1 and status(h)['status'] == 'pending'
    restarted = make_sup(h, repo, now)  # requirement 7e
    now[0] = 1370.0
    tick(restarted)
    assert len(S.read_outbox(h)) == 1, 'a restart must not repeat the notice'
    assert status(h)['since'] == 1000.0 and status(h)['notice_at'] == 1300.0 and status(h)['episode_id'] == episode
    hook.unlink()
    now[0] = 1431.0
    tick(restarted)
    st = status(h)
    assert st['status'] == 'synced' and st['last_successful_push_sha'] == head(repo) == head(remote), st
    notices = S.read_outbox(h)
    assert len(notices) == 2, notices
    r = notices[1]
    assert r['id'] == f'persistence-{episode}-recovered' and r['channel'] == 'C_FIXTURE' and not r.get('thread_ts'), r
    one_line(r['text'])
    assert r['text'] == f'persistence recovered: synced at {head(repo)[:12]} after 7 min', r['text']
    now[0] = 1500.0
    tick(restarted)
    again = make_sup(h, repo, now)
    now[0] = 1570.0
    tick(again)
    assert len(S.read_outbox(h)) == 2, 'nothing after the recovered line'
    print('deadline notice: ok')


def rebase_before_push():
    """Requirements 1 and 7c, plus the b7 persistence_reconciliation scenario under the new contract."""
    remote, repo, other = make_repo()
    h = home()
    write_state(h, {'tracks': [], 'turn': 1})
    now = [3000.0]
    sup = make_sup(h, repo, now)
    push_other(other, 'external', 'keep')  # the remote moves after the supervisor's turn began
    assert sup.persist(1) is True, 'a remote that moved during the turn must not fail the push'
    st = status(h)
    assert st['status'] == 'synced' and st.get('since') is None and st.get('failures') in (None, 0), st
    assert head(repo) == head(remote)
    no_merges(remote)
    assert (repo / 'external').read_text() == 'keep'
    assert git(remote, 'show', 'HEAD:factory/state.json').stdout == Path(h, 'state.json').read_text()
    assert S.read_outbox(h) == []
    # A rejected push whose remote moves again before the retry: rebased, linear, both contents present.
    hook = reject(remote)
    write_state(h, {'tracks': [], 'turn': 2})
    assert sup.persist(2) is False
    pending = status(h)['pending_local_sha']
    assert pending == head(repo)
    hook.unlink()
    push_other(other, 'external2', 'keep2')  # the remote moves before the retry
    now[0] += 61
    sup.reconcile_persistence()
    st = status(h)
    assert st['status'] == 'synced' and st['pending_local_sha'] is None and st['last_successful_push_sha'] == head(repo), st
    assert head(repo) == head(remote) and head(repo) != pending, 'the retried commit must be rebased, not merged'
    no_merges(remote)
    assert git(repo, 'log', '-1', '--format=%s').stdout.strip() == 'manager: turn 2'
    assert (repo / 'external2').read_text() == 'keep2'
    assert git(remote, 'show', 'HEAD:factory/state.json').stdout == Path(h, 'state.json').read_text()
    assert git(repo, 'status', '--porcelain').stdout.strip() == ''
    assert S.read_outbox(h) == []
    # Diverged start (b7 persistence_reconciliation): sync_repo_before reconciles by rebase, then persist pushes.
    push_other(other, 'external3', 'keep3')
    (repo / 'factory' / 'local').write_text('keep local')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'local')
    external = head(other)
    sup.sync_repo_before()
    assert git(repo, 'merge-base', '--is-ancestor', external, 'HEAD', check=False).returncode == 0, 'remote history absent'
    assert (repo / 'external3').read_text() == 'keep3' and (repo / 'factory' / 'local').read_text() == 'keep local'
    assert not (repo / '.git' / 'MERGE_HEAD').exists()
    write_state(h, {'tracks': [], 'turn': 3})
    assert sup.persist(3) is True
    assert head(repo) == head(remote)
    no_merges(remote)
    assert git(remote, 'show', 'HEAD:factory/local').stdout == 'keep local'
    # No new state: the retry still publishes the previous local commit (requirement 19 unchanged).
    hook = reject(remote)
    write_state(h, {'tracks': [], 'turn': 4})
    assert sup.persist(4) is False
    pending = head(repo)
    hook.unlink()
    now[0] += 61
    assert sup.persist(5) is True
    assert head(remote) == pending and status(h)['status'] == 'synced'
    assert S.read_outbox(h) == []
    print('rebase before push: ok')


def conflict_blocks():
    """Requirements 1 and 7d: a rebase conflict aborts, retains the local commit, blocks, and gets its single notice
    at the deadline; the recovered line follows once the remote is repaired by hand."""
    remote, repo, other = make_repo()
    h = home()
    write_state(h, {'tracks': [], 'owner': 'local'})
    hook = reject(remote)
    now = [2000.0]
    sup = make_sup(h, repo, now)
    assert sup.persist(1) is False
    local = head(repo)
    hook.unlink()
    before_conflict = head(other)
    push_other(other, 'factory/state.json', json.dumps({'tracks': [], 'owner': 'remote'}), 'conflicting state')
    now[0] = 2061.0
    tick(sup)
    st = status(h)
    assert st['status'] == 'blocked' and st['error'].startswith('rebase: '), st
    assert st['since'] == 2000.0 and st['notice_at'] is None, st
    assert head(repo) == local, 'the local commit must be retained'
    assert not (repo / '.git' / 'rebase-merge').exists() and not (repo / '.git' / 'rebase-apply').exists()
    assert not (repo / '.git' / 'MERGE_HEAD').exists()
    assert json.loads((repo / 'factory' / 'state.json').read_text())['owner'] == 'local'
    assert git(repo, 'status', '--porcelain').stdout.strip() == ''
    assert S.read_outbox(h) == []
    for t in (2122.0, 2200.0, 2299.0):
        now[0] = t
        tick(sup, git_allowed=False)  # blocked is never retried by a tick
        assert S.read_outbox(h) == [] and status(h)['status'] == 'blocked'
    now[0] = 2300.0
    tick(sup, git_allowed=False)
    notices = S.read_outbox(h)
    assert len(notices) == 1, notices
    one_line(notices[0]['text'])
    assert notices[0]['text'].startswith('persistence blocked for 5 min: rebase: '), notices[0]['text']
    assert notices[0]['channel'] == 'C_FIXTURE' and not notices[0].get('thread_ts')
    now[0] = 2350.0
    tick(sup, git_allowed=False)
    assert len(S.read_outbox(h)) == 1
    # The fixture repairs the remote by hand (never the supervisor): back to the pre-conflict tip.
    git(other, 'reset', '-q', '--hard', before_conflict)
    git(other, 'push', '--force', 'origin', 'HEAD')
    now[0] = 2400.0
    sup.sync_repo_before()
    assert sup.persist(2) is True
    st = status(h)
    assert st['status'] == 'synced' and head(repo) == head(remote), st
    no_merges(remote)
    notices = S.read_outbox(h)
    assert len(notices) == 2, notices
    assert notices[1]['text'] == f'persistence recovered: synced at {head(repo)[:12]} after 6 min', notices[1]['text']
    print('conflict blocks: ok')


def summary_contract():
    """Requirement 4: the one-line summary of a stored error."""
    f = S.notice_error_summary
    assert f(REAL_REJECTION) == "push: failed to push some refs to 'https://github.com/faden-systems/faden.git'", f(REAL_REJECTION)
    assert f("fetch: fatal: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com") == \
        "fetch: unable to access 'https://github.com/x/y.git/': Could not resolve host: github.com"
    secret = f("push: error: failed to push some refs to 'https://hydra:ghp_secret123@github.com/x/y.git'")
    assert secret == "push: failed to push some refs to 'https://<redacted>@github.com/x/y.git'", secret
    assert 'ghp_secret123' not in secret
    assert f("push: error: failed to push some refs to 'https://hydra@github.com/x/y.git'") == \
        "push: failed to push some refs to 'https://<redacted>@github.com/x/y.git'"
    assert f("push: remote: nope\n ! [remote rejected] HEAD -> main (pre-receive hook declined)\nhint: x") == 'push: remote: nope'
    assert f('push: ') == 'push: unknown error'
    assert f('push: hint: only hints\nhint: more hints') == 'push: unknown error'
    assert f('push: error:   many   spaces\there \n') == 'push: many spaces here'
    long = f('commit: error: ' + 'x' * 500)
    assert long.startswith('commit: ') and len(long) == len('commit: ') + 200, len(long)
    for text in (REAL_REJECTION, 'push: error: a\nhint: b\n', 'rebase: CONFLICT (content): Merge conflict in factory/state.json\nerror: could not apply 1234567... manager: turn 9\nhint: Resolve all conflicts manually'):
        out = f(text)
        one_line(out)
        assert out.split(': ', 1)[0] == text.split(': ', 1)[0]
    print('summary contract: ok')


def b7_subset():
    source = ROOT / 'loops' / 'b7.acceptance.py'
    tree = ast.parse(source.read_text())
    names = None
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple) and \
                all(isinstance(e, ast.Name) for e in node.iter.elts) and len(node.iter.elts) > 10:
            names = [e.id for e in node.iter.elts]
    assert names, 'could not read the b7 check list'
    module = runpy.run_path(str(source), run_name='b10_inherited')
    failures = []
    for name in names:
        if name in B7_SUPERSEDED:
            print(f'[b7 subset] SKIP {name} (superseded by b10, requirement 7)', flush=True)
            continue
        try:
            module[name]()
        except (AssertionError, Exception) as exc:  # noqa: BLE001
            failures.append(name)
            print(f'[b7 subset] FAIL {name}: {type(exc).__name__}: {exc}', flush=True)
        else:
            print(f'[b7 subset] OK {name}', flush=True)
    if failures:
        sys.exit(1)
    print('[b7 subset] PASS')


if __name__ == '__main__':
    if '--b7-subset' in sys.argv:
        b7_subset()
        sys.exit(0)
    constants()
    summary_contract()
    quiet_recovery()
    deadline_notice()
    rebase_before_push()
    conflict_blocks()
    print('[b10.acceptance] PASS')
