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


ALLOWED_GIT = {'fetch', 'rebase', 'push', 'rev-parse', 'merge-base', 'status', 'add', 'diff', 'commit'}


def check_trace(calls):
    """Requirement 7i: only plain, argument-exact persistence commands; nothing destructive anywhere."""
    for call in calls:
        args = tuple(a for a in call if a != '-q')
        name = args[0] if args else ''
        assert name in ALLOWED_GIT, f'forbidden git command issued by persistence: {call}'
        assert not any(str(a).startswith('--force') or a in ('-f', '--hard') for a in args), f'forbidden flag: {call}'
        if name == 'push':
            assert args == ('push', 'origin', 'HEAD'), f'push must be plain: {call}'
        elif name == 'fetch':
            assert args == ('fetch', 'origin'), f'fetch must be plain: {call}'
        elif name == 'rebase':
            assert args == ('rebase', '--abort') or (len(args) == 2 and args[1].startswith('origin/')), f'rebase shape: {call}'


def tick(sup, git_allowed=True):
    calls = []
    real = sup._git

    def wrapped(*args, **kw):
        calls.append(tuple(args))
        return real(*args, **kw)
    with patch.object(sup, 'compaction_due', return_value=(False, '')), patch.object(sup, '_git', wrapped):
        result = sup.run_once()
    assert git_allowed or not calls, f'this tick must not touch git: {calls}'
    check_trace(calls)
    return result


def status(h):
    return json.loads(Path(h, 'logs', 'persistence.json').read_text())


def trace(sup, fn):
    """Run fn() and return the list of git argument tuples the supervisor issued meanwhile."""
    calls = []
    real = sup._git

    def wrapped(*args, **kw):
        calls.append(tuple(args))
        return real(*args, **kw)
    with patch.object(sup, '_git', wrapped):
        result = fn()
    check_trace(calls)
    return result, calls


def fetch_right_before_push(calls):
    names = [c[0] for c in calls]
    assert 'push' in names, names
    push = names.index('push')
    fetches = [i for i, n in enumerate(names[:push]) if n == 'fetch']
    assert fetches, f'no fetch before the push: {names}'
    last_fetch = fetches[-1]
    commits = [i for i, n in enumerate(names[:push]) if n == 'commit']
    assert not commits or commits[-1] < last_fetch, f'the fetch must follow the commit: {names}'
    between = set(names[last_fetch + 1:push])
    assert between <= {'rev-parse', 'merge-base', 'rebase', 'status'}, f'between fetch and push: {names}'


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
    ok, calls = trace(sup, lambda: sup.persist(1))
    assert ok is True, 'a remote that moved during the turn must not fail the push'
    fetch_right_before_push(calls)  # requirement 7i: first attempt
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
    _, calls = trace(sup, sup.reconcile_persistence)
    fetch_right_before_push(calls)  # requirement 7i: retry
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
    blocked_error = st['error']
    ok, calls = trace(sup, lambda: sup.persist(2))  # requirement 7h: persist after a blocked sync
    assert ok is False and calls == [], f'persist after a blocked sync must not touch git: {calls}'
    st = status(h)
    assert st['status'] == 'blocked' and st['error'] == blocked_error and st['since'] == 2000.0, st
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
    assert sup.persist(3) is True
    st = status(h)
    assert st['status'] == 'synced' and head(repo) == head(remote), st
    no_merges(remote)
    notices = S.read_outbox(h)
    assert len(notices) == 2, notices
    assert notices[1]['text'] == f'persistence recovered: synced at {head(repo)[:12]} after 6 min', notices[1]['text']
    print('conflict blocks: ok')


def dirty_sync_then_persist():
    """Requirement 7h: unmanaged dirt blocks the sync; the following persist touches nothing."""
    remote, repo, other = make_repo()
    h = home()
    write_state(h, {'tracks': []})
    (repo / 'unrelated').write_text('dirty')
    now = [4000.0]
    sup = make_sup(h, repo, now)
    initial = head(repo)
    sup.sync_repo_before()
    st = status(h)
    assert st['status'] == 'blocked' and st['error'].startswith('dirty: ') and st['since'] == 4000.0, st
    ok, calls = trace(sup, lambda: sup.persist(1))
    assert ok is False and calls == [], calls
    assert status(h)['error'] == st['error'] and head(repo) == initial and (repo / 'unrelated').read_text() == 'dirty'
    assert S.read_outbox(h) == []
    print('dirty sync then persist: ok')


def rebase_in_progress():
    """Requirements 1 and 7j: an unfinished rebase blocks before any git call."""
    remote, repo, other = make_repo()
    h = home()
    write_state(h, {'tracks': []})
    now = [4500.0]
    sup = make_sup(h, repo, now)
    (repo / '.git' / 'rebase-merge').mkdir()
    _, calls = trace(sup, sup.sync_repo_before)
    st = status(h)
    assert calls == [], f'sync must not touch git during a rebase: {calls}'
    assert st['status'] == 'blocked' and st['error'].startswith('rebase: rebase in progress'), st
    ok, calls = trace(sup, lambda: sup.persist(1))
    assert ok is False and calls == [], calls
    assert status(h)['error'].startswith('rebase: rebase in progress')
    assert S.read_outbox(h) == []
    (repo / '.git' / 'rebase-merge').rmdir()
    now[0] = 4600.0
    sup.sync_repo_before()
    assert sup.persist(2) is True and status(h)['status'] == 'synced'
    assert S.read_outbox(h) == []
    print('rebase in progress: ok')


def legacy_records():
    """Requirements 2 and 7g: b7-era pending records are adopted, never crashed on or double-noticed."""
    # (a) no legacy notice: adopted, noticed five minutes after adoption, recovered line once.
    remote, repo, other = make_repo()
    h = home()
    write_state(h, {'tracks': []})
    hook = reject(remote)
    legacy = {'status': 'pending', 'error': "push: error: failed to push some refs to 'x'", 'episode_id': 'legacy-a',
              'retry_after': 0, 'last_turn': 3, 'pending_local_sha': None}
    Path(h, 'logs', 'persistence.json').write_text(json.dumps(legacy))
    now = [5000.0]
    sup = make_sup(h, repo, now)
    tick(sup)  # retries (deadline passed), fails again on the hook; must not crash
    st = status(h)
    assert st['status'] == 'pending' and st['episode_id'] == 'legacy-a' and st['since'] == 5000.0, st
    assert st['notice_at'] is None and S.read_outbox(h) == []
    for t in (5061.0, 5200.0, 5299.0):
        now[0] = t
        tick(sup)
        assert S.read_outbox(h) == []
    now[0] = 5300.0
    tick(sup)
    notices = S.read_outbox(h)
    assert len(notices) == 1 and notices[0]['id'] == 'persistence-legacy-a-notice', notices
    assert notices[0]['text'].startswith('persistence pending for 5 min: push: '), notices[0]['text']
    one_line(notices[0]['text'])
    hook.unlink()
    now[0] = 5400.0
    tick(sup)
    notices = S.read_outbox(h)
    assert status(h)['status'] == 'synced' and len(notices) == 2 and notices[1]['id'] == 'persistence-legacy-a-recovered', notices
    # (b) legacy notice already queued, (c) legacy notice already delivered (receipt only): one recovered line, no notice.
    for variant in ('queued', 'receipted'):
        remote, repo, other = make_repo()
        h = home()
        write_state(h, {'tracks': []})
        hook = reject(remote)
        ep = f'legacy-{variant}'
        legacy = {'status': 'pending', 'error': 'push: error: failed to push some refs', 'episode_id': ep,
                  'retry_after': 0, 'last_turn': 3}
        Path(h, 'logs', 'persistence.json').write_text(json.dumps(legacy))
        if variant == 'queued':
            S._queue_notice_once(h, 'C_FIXTURE', f'persistence pending (episode {ep}): push: x', f'persistence-{ep}-failed')
            baseline = 1
        else:
            S._write_receipt(h, f'persistence-{ep}-failed', {'status': 'posted', 'ts': '1.0'})
            baseline = 0
        now = [6000.0]
        sup = make_sup(h, repo, now)
        tick(sup)
        st = status(h)
        assert st['episode_id'] == ep and st['since'] == 6000.0 and st['notice_at'] == 6000.0, st
        assert len(S.read_outbox(h)) == baseline
        for t in (6061.0, 6300.0, 6400.0):
            now[0] = t
            tick(sup)
            assert len(S.read_outbox(h)) == baseline, f'{variant}: no second notice'
        hook.unlink()
        now[0] = 6461.0
        restarted = make_sup(h, repo, now)
        tick(restarted)
        notices = S.read_outbox(h)
        assert status(h)['status'] == 'synced' and len(notices) == baseline + 1, (variant, notices)
        assert notices[-1]['id'] == f'persistence-{ep}-recovered', notices[-1]
        assert notices[-1]['text'] == f'persistence recovered: synced at {head(repo)[:12]} after 7 min', notices[-1]['text']
        now[0] = 6600.0
        tick(restarted)
        assert len(S.read_outbox(h)) == baseline + 1
    print('legacy records: ok')


def abort_failure():
    """Requirement 7k: a rebase whose abort also fails blocks with both outputs and builds on nothing."""
    import subprocess as sp
    for entry in ('sync', 'persist'):
        remote, repo, other = make_repo()
        h = home()
        write_state(h, {'tracks': []})
        hook = reject(remote)
        now = [7000.0]
        sup = make_sup(h, repo, now)
        assert sup.persist(1) is False
        local = head(repo)
        hook.unlink()
        push_other(other, 'external', 'keep')  # the retry must rebase
        real = sup._git
        calls = []

        def fake(*args, **kw):
            calls.append(tuple(args))
            if args and args[0] == 'rebase':
                if '--abort' in args:
                    return sp.CompletedProcess(args, 1, '', 'fake abort failure')
                return sp.CompletedProcess(args, 1, '', 'CONFLICT (fake) could not apply')
            return real(*args, **kw)
        now[0] = 7061.0
        with patch.object(sup, '_git', fake):
            if entry == 'sync':
                sup.sync_repo_before()
                assert sup.persist(2) is False
            else:
                assert sup.persist(2) is False
        names = [c[0] for c in calls]
        assert 'push' not in names, names
        i = names.index('rebase')
        assert names[i + 1] == 'rebase' and '--abort' in calls[i + 1], names
        assert names[i + 2:] == [], f'nothing may follow the failed abort: {names[i + 2:]}'
        st = status(h)
        assert st['status'] == 'blocked' and st['error'].startswith('rebase: '), st
        assert 'CONFLICT (fake)' in st['error'] and 'abort: fake abort failure' in st['error'], st['error']
        assert head(repo) == local
        (repo / '.git' / 'rebase-merge').mkdir()  # the metadata a failed abort leaves behind
        ok, calls2 = trace(sup, lambda: sup.persist(3))
        assert ok is False and calls2 == [], calls2
        assert status(h)['status'] == 'blocked' and S.read_outbox(h) == []
    print('abort failure: ok')


def notice_from_failed_attempt():
    """Requirement 7l: the failed attempt itself crossing the threshold queues the notice, no tick needed."""
    remote, repo, other = make_repo()
    h = home()
    write_state(h, {'tracks': []})
    hook = reject(remote)
    now = [8000.0]
    sup = make_sup(h, repo, now)
    assert sup.persist(1) is False and S.read_outbox(h) == []
    now[0] = 8300.0
    ok, calls = trace(sup, lambda: sup.persist(2))
    assert ok is False and [c[0] for c in calls].count('push') == 1, calls
    notices = S.read_outbox(h)
    assert len(notices) == 1, notices
    assert notices[0]['text'] == f"persistence pending for 5 min: {S.notice_error_summary(status(h)['error'])}", notices[0]['text']
    assert status(h)['notice_at'] == 8300.0 and status(h)['failures'] == 2
    now[0] = 8361.0
    assert sup.persist(3) is False and len(S.read_outbox(h)) == 1
    print('notice from failed attempt: ok')


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
    dirty_sync_then_persist()
    rebase_in_progress()
    legacy_records()
    abort_failure()
    notice_from_failed_attempt()
    print('[b10.acceptance] PASS')
