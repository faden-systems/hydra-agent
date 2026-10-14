#!/usr/bin/env python3
"""Exit-owned offline b11 contracts (loops/b11.md). A temporary home, recording fake engines that speak the
supervisor's stream-json, a controlled clock and fake posters only: no real engine, GitHub or Slack. Every check
raises (no `assert` in the contracts, so `python -O` cannot skip one). `--only <name>` runs one scenario."""
import hashlib
import contextlib
import http.server
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'manager'))
import supervisor as S  # noqa: E402

HYDRA = str(ROOT / 'manager' / 'hydra')
THRESHOLD = 300000
FADEN_REPO, HYDRA_REPO = 'faden-systems/faden', 'faden-systems/hydra-agent'


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


# ---------------------------------------------------------------------------------------------- fixtures
def home():
    h = tempfile.mkdtemp(prefix='b11-home-')
    for d in ('inbox', 'inbox/files', 'logs', 'credentials', '.claude', 'mirror'):
        os.makedirs(os.path.join(h, d), exist_ok=True)
    for name in ('claude-r2d2', 'claude-l'):
        token = f'fake-{name}'
        Path(h, 'credentials', f'{name}.env').write_text(f'CLAUDE_CODE_OAUTH_TOKEN={token}\n')
        Path(h, 'credentials', f'{name}.env.identity.json').write_text(json.dumps(
            {'schema_version': 1, 'label': name, 'account_id': f'fixture-{name}',
             'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'verified_at': '2026-10-04T20:00:00Z',
             'method': 'private-window-and-usage-bar', 'evidence': 'https://example.test/evidence'}))
    Path(h, 'engine').write_text('claude-r2d2\n')
    Path(h, 'session-id').write_text('sess-test\n')
    Path(h, 'config.json').write_text('{}')
    return h


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import sys, os, json
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]
assert '/compact' not in argv, 'obsolete compaction invocation'
sid = argv[argv.index('--session-id') + 1] if '--session-id' in argv else (argv[argv.index('--resume') + 1] if '--resume' in argv else '')
PERSISTENT = '--input-format' in argv


def record(msg):
    with open(D / 'calls.jsonl', 'a') as f:
        f.write(json.dumps({'argv': argv, 'stdin': msg, 'kind': 'compaction' if '[compaction]' in msg else 'turn',
                            'persistent': PERSISTENT, 'pid': os.getpid()}) + '\n')


def answer(msg):
    """The reply records for one request; None when the compaction turn must fail (the process exits 1)."""
    if '[compaction]' in msg:
        if (D / 'fail_compact').exists():
            return None
        m = Path(os.environ['HYDRA_MEMORY_DIR']) / 'MEMORY.md'
        m.write_text(m.read_text() + '\nfixture memory flushed ' + sid + '\n')
        reply = 'compacted\n---HANDOFF---\ntracks: t1\nwaiting on: none\nlast decision: flush\nnext action: resume\nopen question: none'
        ctx = 20000
    else:
        reply = 'REPLY: ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none'
        ctx = int((D / ('context_after' if sid != 'sess-test' and (D / 'context_after').exists() else 'context')).read_text())
    usage = {'input_tokens': 100, 'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': ctx - 1100, 'output_tokens': 50}
    return [json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'usage': usage, 'content': [{'type': 'text', 'text': reply}]}}),
            json.dumps({'type': 'result', 'subtype': 'success', 'result': reply, 'session_id': sid,
                        'usage': {'input_tokens': usage['input_tokens'] * 3, 'cache_creation_input_tokens': usage['cache_creation_input_tokens'] * 3,
                                  'cache_read_input_tokens': usage['cache_read_input_tokens'] * 3, 'output_tokens': 150}})]


if not PERSISTENT:
    msg = sys.stdin.read() if not sys.stdin.isatty() else ''
    record(msg)
    out = answer(msg)
    if out is None:
        print('usage limit reached', file=sys.stderr); sys.exit(1)
    print('\n'.join(out))
else:
    # the persistent stream-json child (b8): one startup record, then one user envelope per request on stdin
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        env = json.loads(line)
        content = env.get('message', {}).get('content')
        msg = content if isinstance(content, str) else ''.join(c.get('text', '') for c in content or [])
        record(msg)
        out = answer(msg)
        if out is None:
            print('usage limit reached', file=sys.stderr); sys.exit(1)
        print('\n'.join(out), flush=True)
'''

FAKE_CODEX = r'''#!/usr/bin/env python3
import sys, json
from pathlib import Path
D = Path(%(dir)r)
msg = sys.stdin.read() if not sys.stdin.isatty() else ''
argv = sys.argv[1:]
with open(D / 'calls.jsonl', 'a') as f:
    f.write(json.dumps({'argv': argv, 'stdin': msg, 'kind': 'codex'}) + '\n')
out = argv[argv.index('-o') + 1]
Path(out).write_text('REPLY: codex ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none\n')
print('codex done')
'''


def fakes(context=940000, context_after=90000, fail_compact=False):
    d = Path(tempfile.mkdtemp(prefix='b11-engine-'))
    (d / 'context').write_text(str(context))
    (d / 'context_after').write_text(str(context_after))
    if fail_compact:
        (d / 'fail_compact').write_text('1')
    claude = d / 'claude'
    claude.write_text(FAKE_CLAUDE % {'dir': str(d)})
    claude.chmod(0o755)
    codex = d / 'codex'
    codex.write_text(FAKE_CODEX % {'dir': str(d)})
    codex.chmod(0o755)
    return d


def engines(d):
    return {'claude-r2d2': {'bin': str(d / 'claude'), 'cred': 'claude-r2d2.env'},
            'claude-l': {'bin': str(d / 'claude'), 'cred': 'claude-l.env'},
            'codex': {'bin': str(d / 'codex'), 'cred': None}}


def calls(d):
    p = d / 'calls.jsonl'
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


class Poster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        self.now = self.now + dt.timedelta(seconds=1)
        return self.now

    def advance(self, seconds):
        self.now = self.now + dt.timedelta(seconds=seconds)


def make(h, d, clock=None, blog=None, poster=None, sleep=None):
    return S.Supervisor(home=h, engines=engines(d), poster=poster or Poster(), buildlog_poster=blog or Poster(),
                        clock=clock or Clock(dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc)), sleep=sleep)


def config(h, **cfg):
    Path(h, 'config.json').write_text(json.dumps({'compaction': cfg} if cfg else {}))


def event(h, text, thread='1.0', source='slack', ts=None, instructs=True, continuation=False):
    ev = {'id': ts or f'ev-{time.time_ns()}', 'source': source, 'at': time.time(),
          'payload': {'channel': 'C_DEV', 'thread_ts': thread, 'user': 'U_FOUNDER', 'text': text, 'instructs': instructs}}
    if continuation:
        ev['payload']['continuation'] = True
    S.append_event(h, ev)
    return ev['id']


def turns(h):
    return S.read_jsonl(os.path.join(h, 'logs', 'turns.jsonl'))


def cstate(h):
    return S.read_compaction_state(h)


def session_id(h):
    return Path(h, 'session-id').read_text().strip()


def status_line(h):
    return S.compaction_status_line(h)


def kinds(d):
    return [c['kind'] for c in calls(d)]


def run_ticks(sup, n):
    for _ in range(n):
        sup.run_once()


class LiveLoop:
    """A ticking supervisor in a thread plus a pid file naming this process, so `hydra compact` sees a live loop."""
    def __init__(self, sup, h):
        self.sup, self.h, self.stop = sup, h, threading.Event()
        Path(h, 'logs', 'supervisor.pid').write_text(str(os.getpid()))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                self.sup.run_once()
            except Exception as e:  # noqa: BLE001 - surfaced by the contract's own checks
                print('live loop tick error:', e)
            time.sleep(0.2)

    def close(self):
        self.stop.set(); self.thread.join(5)
        with open(os.devnull, 'w'):
            pass
        try:
            Path(self.h, 'logs', 'supervisor.pid').unlink()
        except FileNotFoundError:
            pass


def hydra_compact(h, timeout=120):
    env = {**os.environ, 'HYDRA_HOME': h, 'HYDRA_COMPACT_TIMEOUT': str(timeout)}
    r = subprocess.run([sys.executable, HYDRA, 'compact'], env=env, capture_output=True, text=True, timeout=timeout + 30)
    return r.returncode, (r.stdout or '') + (r.stderr or '')


# ---------------------------------------------------------------------------------------------- scenarios
def threshold_compacts_before_next_turn():
    """Requirement 1 (and 2's default): 3x over with no compaction config compacts on the next tick, before the
    queued event's turn; the session id is replaced; the following measured turn verifies the outcome."""
    h, d = home(), fakes(context=940000, context_after=90000)
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(turns(h)[-1].get('context_tokens') == 940000, f'turn A context not measured: {turns(h)[-1]}')
    check(cstate(h).get('pending'), 'no pending record after a 3x measurement')
    due, reason = sup.compaction_due()
    check(due and reason == 'tokens', f'compaction_due at 3x over must be (True, "tokens"), got {(due, reason)}')
    # a tick with no queued event compacts anyway (queued events do not gate it); while the verification is
    # outstanding no second rollover is due and no second compaction runs
    sup.run_once()
    check(kinds(d) == ['turn', 'compaction'], f'a tick without events must compact once: {kinds(d)}')
    check(cstate(h).get('verify'), 'verify must be set right after the rollover')
    due2, _ = sup.compaction_due()
    check(not due2, 'nothing is due while the rollover awaits its verifying turn')
    sup.run_once()
    check(kinds(d) == ['turn', 'compaction'], f'no second rollover while verification is outstanding: {kinds(d)}')
    event(h, 'B')
    check(sup.run_once() is True, 'tick with B did not run')
    ks = kinds(d)
    check(ks == ['turn', 'compaction', 'turn'], f'expected [turn A, compaction, turn B] engine calls, got {ks}')
    check(session_id(h) != 'sess-test', 'session id not replaced by the rollover')
    recs = turns(h)
    check([r.get('kind') for r in recs][-2:] == ['compaction', None] or recs[-2].get('kind') == 'compaction',
          f'compaction turn record must precede B: {[(r.get("n"), r.get("kind")) for r in recs]}')
    last = cstate(h).get('last') or {}
    check(last.get('ok') is True and last.get('after_tokens') == 90000,
          f'the verifying turn must settle ok=True with the new context: {last}')
    check(not cstate(h).get('pending'), f'pending must clear after the verified rollover: {cstate(h).get("pending")}')
    check('held' not in json.dumps(last), f'rollover must not be held by default: {last}')


def threshold_boundaries():
    """Requirement 1 (amendment 2026-10-14): due strictly over threshold_tokens, not at it; a configured over_ratio
    raises nothing and is retired from the config."""
    def compacts(context, **cfg):
        h, d = home(), fakes(context=context, context_after=90000)
        if cfg:
            config(h, **cfg)
        sup = make(h, d)
        event(h, 'A'); sup.run_once()
        event(h, 'B'); sup.run_once()
        return 'compaction' in kinds(d)
    check(not compacts(THRESHOLD), f'exactly the threshold ({THRESHOLD}) must not compact')
    check(compacts(THRESHOLD + 1), f'one token over the threshold ({THRESHOLD + 1}) must compact')
    check(compacts(450000, over_ratio=2.0), '1.5x with over_ratio 2.0 configured must still compact (over_ratio is retired)')
    check(compacts(350000, threshold_tokens=340000), 'a configured threshold_tokens is the due limit')
    check(not compacts(350000, threshold_tokens=350000), 'a context at a configured threshold_tokens does not compact')
    h, d = home(), fakes(context=100000)
    config(h, over_ratio=2.0)
    logged, orig = [], S.log
    S.log = lambda m: logged.append(str(m))
    try:
        sup = make(h, d)
        check('over_ratio' not in sup.compaction_config(), f'compaction_config must not return over_ratio: {sup.compaction_config()}')
        event(h, 'A'); sup.run_once(); run_ticks(sup, 3); sup.compaction_config(); S.compaction_config_for(h)
    finally:
        S.log = orig
    retired = [m for m in logged if 'over_ratio' in m and 'retired' in m]
    check(len(retired) == 1, f'exactly one over_ratio retirement log line per supervisor start, got {retired}')
    check('over_ratio' not in S.COMPACTION_DEFAULTS, 'COMPACTION_DEFAULTS must not carry over_ratio')


def quiet_hours_do_not_gate():
    """Requirement 1: inside quiet hours the overage still compacts before the next turn."""
    h, d = home(), fakes(context=940000, context_after=90000)
    config(h, quiet_hours=[2, 5], quiet_hours_tz='UTC')
    clock = Clock(dt.datetime(2026, 10, 8, 3, 0, 0, tzinfo=dt.timezone.utc))
    sup = make(h, d, clock=clock)
    check(sup.quiet_hours_now(), 'fixture clock must be inside quiet hours')
    event(h, 'A'); sup.run_once()
    event(h, 'B'); sup.run_once()
    check(kinds(d) == ['turn', 'compaction', 'turn'], f'quiet hours gated the compaction: {kinds(d)}')



def held_is_loud_and_keeps_the_schedule():
    """Requirement 2: rollover explicitly disabled -> no engine call, held recorded, pending kept, COMPACT consumed,
    one buildlog line per episode; `hydra compact` reports held with exit 1, without and with a live loop."""
    h, d = home(), fakes(context=940000, context_after=90000)
    config(h, rollover_enabled=False)
    blog = Poster()
    sup = make(h, d, blog=blog)
    rc, out = hydra_compact(h, timeout=10)
    check(rc == 1 and 'compaction held' in out, f'no loop + rollover disabled: exit 1 and held expected, got rc={rc} {out[-200:]!r}')
    check(not Path(h, 'COMPACT').exists(), 'no loop + rollover disabled must not write COMPACT')
    h0, d0 = home(), fakes(context=940000, context_after=90000)
    rc0, out0 = hydra_compact(h0, timeout=10)
    check(rc0 == 0 and Path(h0, 'COMPACT').exists(), f'no loop + rollover enabled (the default) must schedule: rc={rc0} {out0[-200:]!r}')
    event(h, 'A'); sup.run_once()
    event(h, 'B'); sup.run_once()
    check(kinds(d) == ['turn', 'turn'], f'a held rollover must make no [compaction] engine call: {kinds(d)}')
    last = cstate(h).get('last') or {}
    check(str(last.get('error', '')).startswith('held:') and last.get('ok') is None, f'held outcome not recorded: {last}')
    check(cstate(h).get('pending'), 'a held outcome must keep pending')
    run_ticks(sup, 2)
    held_lines = [t for _, _, t in blog.posted if 'compaction held' in t]
    check(len(held_lines) == 1, f'exactly one held buildlog line per episode, got {held_lines}')
    check(f'context 940000 over {THRESHOLD}' in held_lines[0] and 'compaction.rollover_enabled' in held_lines[0],
          f'held line must name the measured context, the limit (threshold_tokens) and the setting: {held_lines[0]}')
    hc, dc = home(), fakes(context=940000, context_after=90000)
    config(hc, rollover_enabled=False, threshold_tokens=500000)
    blogc = Poster(); supc = make(hc, dc, blog=blogc)
    event(hc, 'A'); supc.run_once(); event(hc, 'B'); supc.run_once()
    heldc = [t for _, _, t in blogc.posted if 'compaction held' in t]
    check(len(heldc) == 1 and 'context 940000 over 500000' in heldc[0], f'the held line names the configured threshold: {heldc}')
    Path(h, 'COMPACT').write_text('')
    sup.run_once()
    check(not Path(h, 'COMPACT').exists(), 'a forced request must be consumed, not retried every tick')
    check(kinds(d) == ['turn', 'turn'], 'the forced request with rollover disabled must not call the engine')
    check(len([t for _, _, t in blog.posted if 'compaction held' in t]) == 1, 'the same held episode must not post twice')
    check(cstate(h).get('pending'), 'pending must survive a consumed forced request while held')
    loop = LiveLoop(sup, h)
    try:
        rc, out = hydra_compact(h, timeout=60)
    finally:
        loop.close()
    check(rc == 1 and 'compaction held' in out, f'live loop + held: exit 1 and held expected, got rc={rc} {out[-200:]!r}')
    # a clearing turn (context below the threshold) ends the episode; the next overage posts again
    (d / 'context').write_text('100000')
    event(h, 'C'); sup.run_once()
    check(not cstate(h).get('pending'), 'a measured turn below the threshold must clear pending')
    (d / 'context').write_text('940000')
    event(h, 'D'); sup.run_once()
    event(h, 'E'); sup.run_once()
    check(len([t for _, _, t in blog.posted if 'compaction held' in t]) == 2, 'a new overage after a clearing turn is a new held episode')
    # a live loop with rollover enabled: `hydra compact` reports the verified rollover with exit 0
    h2, d2 = home(), fakes(context=940000, context_after=90000)
    sup2 = make(h2, d2)
    event(h2, 'A'); sup2.run_once()
    loop = LiveLoop(sup2, h2)
    try:
        rc, out = hydra_compact(h2, timeout=60)
    finally:
        loop.close()
    check(rc == 0 and 'compacted at' in out, f'live loop + rollover: exit 0 and compacted expected, got rc={rc} {out[-200:]!r}')
    check(session_id(h2) != 'sess-test', 'the CLI-requested rollover must replace the session id')
    # a live loop whose rollover fails: exit 1, never a success line
    h3, d3 = home(), fakes(context=940000, context_after=90000, fail_compact=True)
    sup3 = make(h3, d3)
    event(h3, 'A'); sup3.run_once()
    loop = LiveLoop(sup3, h3)
    try:
        rc, out = hydra_compact(h3, timeout=60)
    finally:
        loop.close()
    check(rc == 1 and 'compacted at' not in out, f'live loop + failed rollover: exit 1 expected, got rc={rc} {out[-200:]!r}')
    # an outcome without a new session id (ok null, an error not starting `held:`): seeded by a stub loop that
    # consumes COMPACT and writes that form, since the engine seam cannot produce it
    h4, d4 = home(), fakes(context=940000, context_after=90000)
    sup4 = make(h4, d4)
    S.write_text(S.compaction_state_path(h4), json.dumps({'last': {'before_tokens': 940000, 'after_tokens': None, 'at': '2026-10-08T09:00:00Z', 'ok': True, 'reason': 'tokens'}}))

    def seeded_run_once():
        if Path(h4, 'COMPACT').exists():
            Path(h4, 'COMPACT').unlink()
            S.write_text(S.compaction_state_path(h4), json.dumps({'last': {'before_tokens': 940000, 'after_tokens': None, 'at': '2026-10-08T12:00:00Z', 'ok': None, 'reason': 'forced', 'error': 'rollover left no session id'}}))
            return True
        return False
    sup4.run_once = seeded_run_once
    loop = LiveLoop(sup4, h4)
    try:
        rc, out = hydra_compact(h4, timeout=60)
    finally:
        loop.close()
    check(rc == 1 and 'compaction unverified' in out and 'compacted at' not in out, f'live loop + unverified outcome: exit 1 and `compaction unverified` expected, got rc={rc} {out[-200:]!r}')


def failed_rollover_backs_off():
    """Requirement 1/2: a failed [compaction] turn backs off for retry_after_s, then is attempted again."""
    h, d = home(), fakes(context=940000, context_after=90000, fail_compact=True)
    config(h, retry_after_s=3600)
    blog = Poster()
    clock = Clock(dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc))
    sup = make(h, d, clock=clock, blog=blog)
    event(h, 'A'); sup.run_once()
    event(h, 'B'); sup.run_once()
    check(kinds(d) == ['turn', 'compaction', 'turn'], f'the first overage must attempt the rollover: {kinds(d)}')
    check(cstate(h).get('failed_at'), 'a failed rollover must record failed_at')
    check(session_id(h) == 'sess-test', 'a failed rollover must keep the session id')
    check(len([t for _, _, t in blog.posted if 'rollover failed' in t]) == 1, f'one failure line expected: {blog.posted}')
    for name in ('C', 'D', 'E'):
        event(h, name); sup.run_once()
    check(kinds(d).count('compaction') == 1, f'no second attempt within retry_after_s: {kinds(d)}')
    clock.advance(3700)
    (d / 'fail_compact').unlink()
    event(h, 'F'); sup.run_once()
    check(kinds(d).count('compaction') == 2, f'after retry_after_s the rollover must be attempted again: {kinds(d)}')
    check(session_id(h) != 'sess-test', 'the retried rollover must replace the session id')


def transcript_dir(h):
    return Path(h, '.claude', 'projects', os.path.abspath(h).replace('/', '-'))



def cli_compactions_detected():
    """Requirement 3: the module-level parser, then the per-turn scan, the turn flag, the record, the status, the
    path change after a rollover, truncation without duplicates, the 50-record cap, an unreadable transcript."""
    good = {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-10-08T09:40:58.542Z'}
    other = {'type': 'user', 'message': {'role': 'user', 'content': 'hi'}}
    data = (json.dumps(other) + '\n' + json.dumps(good) + '\n' + 'not json at all\n' + json.dumps(other) + '\n'
            + json.dumps(dict(good, timestamp='2026-10-08T10:40:58.542Z')) + '\n').encode()
    partial = b'{"type": "system", "subtype": "compact_bou'
    recs, offset = S.cli_compactions_since(data + partial, 0)
    check(len(recs) == 2, f'two compact_boundary records expected, got {recs}')
    check(offset == len(data), f'the offset must stop before the partial trailing line: {offset} != {len(data)}')
    check(all(r.get('at') for r in recs), f'records must carry the boundary timestamp: {recs}')
    recs2, offset2 = S.cli_compactions_since(data + partial, offset)
    check(recs2 == [] and offset2 == offset, 'a rescan from the offset must find nothing new')
    # integration: a boundary appended between two turns
    h, d = home(), fakes(context=100000, context_after=90000)
    sup = make(h, d)
    tdir = transcript_dir(h); tdir.mkdir(parents=True)
    tpath = tdir / 'sess-test.jsonl'
    tpath.write_text(json.dumps(other) + '\n')
    seen = []
    real_since = S.cli_compactions_since

    def spy(data, offset):
        seen.append((len(data), offset))
        return real_since(data, offset)
    S.cli_compactions_since = spy
    event(h, 'A'); sup.run_once()
    check(not turns(h)[-1].get('cli_compacted'), 'no boundary yet: the turn must not be flagged')
    first_len = tpath.stat().st_size
    with tpath.open('a') as f:
        f.write(json.dumps(good) + '\n')
    added = tpath.stat().st_size - first_len
    event(h, 'B'); sup.run_once()
    check(seen and seen[-1] == (added, 0), f'the scan must hand the parser only the bytes after the recorded offset: {seen[-1:]} vs ({added}, 0)')
    S.cli_compactions_since = real_since
    rec = turns(h)[-1]
    check(rec.get('cli_compacted') is True, f'the turn after a boundary must carry cli_compacted: {rec}')
    cli = cstate(h).get('cli_compactions') or []
    check(len(cli) == 1 and cli[0].get('at') == good['timestamp'], f'one CLI compaction record expected: {cli}')
    scan = cstate(h).get('transcript_scan') or {}
    check(scan.get('path') == str(tpath) and scan.get('offset') == tpath.stat().st_size,
          f'transcript_scan must record the path and the offset at the file size after the scan: {scan} vs {tpath.stat().st_size}')
    line = status_line(h)
    check('(cli)' in line and 'last compaction: 2026-10-08T09:40:58' in line, f'status must show the CLI compaction last: {line}')
    event(h, 'C'); sup.run_once()
    check(len(cstate(h).get('cli_compactions') or []) == 1, 'a boundary must be counted once, not on every scan')
    # a partial boundary line is not consumed; once its newline arrives it is found (offset = consumed, not file size)
    partial_line = json.dumps(dict(good, timestamp='2026-10-08T10:40:58.542Z'))
    with tpath.open('a') as f:
        f.write(partial_line[:20])
    event(h, 'C-partial'); sup.run_once()
    check(len(cstate(h).get('cli_compactions') or []) == 1, 'a partial boundary line must not be counted')
    check((cstate(h).get('transcript_scan') or {}).get('offset') == tpath.stat().st_size - 20, 'the offset must stop before the partial line')
    with tpath.open('a') as f:
        f.write(partial_line[20:] + '\n')
    event(h, 'C-complete'); sup.run_once()
    cli = cstate(h).get('cli_compactions') or []
    check(len(cli) == 2 and cli[-1].get('at') == '2026-10-08T10:40:58.542Z', f'the completed boundary line must be found: {cli}')
    sup_fresh = make(h, d)
    event(h, 'C-fresh'); sup_fresh.run_once()
    check(len(cstate(h).get('cli_compactions') or []) == 2, 'a fresh supervisor must rescan from the recorded offset and find nothing new')
    # truncation: the file is rewritten shorter with the same boundary -> rescanned, not duplicated
    tpath.write_text(json.dumps(good) + '\n')
    event(h, 'C2'); sup.run_once()
    check(len(cstate(h).get('cli_compactions') or []) == 2, 'a rescan after truncation must not duplicate a recorded boundary')
    check((cstate(h).get('transcript_scan') or {}).get('offset') == tpath.stat().st_size, 'the offset must follow the truncated file')
    # a supervisor rollover (the fixture clock runs from 12:00:00Z): a boundary appended to the OLD transcript right
    # before the rollover tick is found by the [compaction] turn's scan; older than the rollover, it does not replace
    # the rollover as the status last
    (d / 'context').write_text('940000')
    event(h, 'D'); sup.run_once()
    with tpath.open('a') as f:
        f.write(json.dumps(dict(good, timestamp='2026-10-08T11:30:00.000Z')) + '\n')
    event(h, 'E'); sup.run_once()
    check('compaction' in kinds(d), 'overage after the CLI case must compact')
    check(session_id(h) != 'sess-test', 'rollover must replace the session id')
    cli = cstate(h).get('cli_compactions') or []
    check(len(cli) == 3 and cli[-1].get('at') == '2026-10-08T11:30:00.000Z', f'the boundary written before the rollover tick must be recorded by the compaction turn: {cli}')
    line = status_line(h)
    check('(cli)' not in line and '->' in line, f'an older boundary must not replace the newer supervisor compaction as the status last: {line}')
    # the scan path follows the new session id; a NEWER boundary there becomes the status last
    newpath = tdir / f'{session_id(h)}.jsonl'
    newpath.write_text(json.dumps(other) + '\n' + json.dumps(dict(good, timestamp='2026-10-08T13:00:00.000Z')) + '\n')
    event(h, 'F'); sup.run_once()
    cli = cstate(h).get('cli_compactions') or []
    check(len(cli) == 4 and cli[-1].get('at') == '2026-10-08T13:00:00.000Z', f'a boundary in the new transcript must be found: {cli}')
    check(turns(h)[-1].get('cli_compacted') is True, 'the turn after the new-transcript boundary must be flagged')
    check('last compaction: 2026-10-08T13:00:00' in status_line(h) and '(cli)' in status_line(h), f'the newer CLI compaction must now be the status last: {status_line(h)}')
    check((cstate(h).get('transcript_scan') or {}).get('path') == str(newpath), 'the scan path must follow the new session id')
    # the cap: 51 boundaries keep the newest 50
    with newpath.open('a') as f:
        for i in range(51):
            f.write(json.dumps(dict(good, timestamp=f'2026-10-09T00:{i // 60:02d}:{i % 60:02d}.000Z')) + '\n')
    event(h, 'G'); sup.run_once()
    cli = cstate(h).get('cli_compactions') or []
    check(len(cli) == 50 and cli[-1].get('at') == '2026-10-09T00:00:50.000Z', f'the newest 50 must be kept: {len(cli)} {cli[-1:]}')
    # an unreadable transcript never fails a turn
    newpath.unlink(); newpath.mkdir()
    event(h, 'H'); check(sup.run_once() is True, 'an unreadable transcript must not fail the turn')
    check(turns(h)[-1].get('events') == [turns(h)[-1]['events'][0]] if turns(h)[-1].get('events') else True, 'turn recorded')
    # a missing transcript never fails a turn
    h2, d2 = home(), fakes(context=100000)
    sup2 = make(h2, d2)
    event(h2, 'A'); check(sup2.run_once() is True, 'a missing transcript must not fail the turn')
    check(turns(h2)[-1].get('cli_compacted') in (False, None), 'no transcript: no flag')



def status_line_contract():
    """Requirement 4: one number, the due limit, the last compaction, at most one suffix in the stated precedence;
    never the old wording; the back-off compares against the given clock."""
    def write(h, state=None, turn=None):
        if state is not None:
            S.write_text(S.compaction_state_path(h), json.dumps(state))
        if turn is not None:
            S.append_jsonl(os.path.join(h, 'logs', 'turns.jsonl'), turn)
    now = dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc)
    h = home()
    line = S.compaction_status_line(h, now=now)
    check(line.startswith('context: unknown') and f'due over {THRESHOLD}' in line and 'last compaction: none' in line, f'no measurement: {line}')
    check('due at' not in line, f'the old due wording must go: {line}')
    write(h, turn={'n': 1, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 100000})
    line = S.compaction_status_line(h, now=now)
    check(line.startswith('context: 100000 tokens (turn 1)') and '; compaction' not in line, f'below threshold: {line}')
    write(h, turn={'n': 2, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': THRESHOLD})
    line = S.compaction_status_line(h, now=now)
    check(line.startswith(f'context: {THRESHOLD} tokens (turn 2)') and '; compaction' not in line, f'at exactly the threshold: no suffix: {line}')
    write(h, state={'pending': {'reason': 'tokens', 'value': THRESHOLD + 1, 'limit': THRESHOLD, 'since': 'x'}},
          turn={'n': 2, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': THRESHOLD + 1})
    line = S.compaction_status_line(h, now=now)
    check(line.startswith(f'context: {THRESHOLD + 1} tokens (turn 2)') and line.endswith('; compaction: due (tokens)'), f'one over the threshold is due: {line}')
    write(h, state={'pending': {'reason': 'tokens', 'value': 940000, 'limit': THRESHOLD, 'since': 'x'}},
          turn={'n': 3, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 940000})
    line = S.compaction_status_line(h, now=now)
    check(line.startswith('context: 940000 tokens (turn 3)') and line.endswith('; compaction: due (tokens)'), f'due: {line}')
    # a failure back-off in force suppresses due, measured against the given clock
    write(h, state={'pending': {'reason': 'tokens', 'value': 940000, 'limit': THRESHOLD, 'since': 'x'},
                    'failed_at': '2026-10-08T11:30:00Z', 'last': {'before_tokens': 940000, 'after_tokens': None, 'at': '2026-10-08T11:30:00Z', 'ok': False, 'reason': 'tokens', 'error': 'usage limit'}})
    line = S.compaction_status_line(h, now=now)
    check('; compaction: due' not in line and '(940000 -> failed)' in line, f'back-off in force: no due suffix: {line}')
    line = S.compaction_status_line(h, now=now + dt.timedelta(hours=7))
    check(line.endswith('; compaction: due (tokens)'), f'after the back-off the overage is due again: {line}')
    # forced wins over everything
    Path(h, 'COMPACT').write_text('')
    check(S.compaction_status_line(h, now=now).endswith('; compaction: forced'), f'forced: {S.compaction_status_line(h, now=now)}')
    Path(h, 'COMPACT').unlink()
    # held wins over due (the overage stays, rollover is disabled)
    write(h, state={'pending': {'reason': 'tokens', 'value': 940000, 'limit': THRESHOLD, 'since': 'x'},
                    'last': {'before_tokens': 940000, 'after_tokens': None, 'at': '2026-10-08T09:58:22Z', 'ok': None,
                             'reason': 'forced', 'error': 'held: automatic rollover is disabled'}})
    line = S.compaction_status_line(h, now=now)
    check('last compaction: 2026-10-08T09:58:22Z (940000 -> held)' in line and line.endswith('; compaction: held'), f'held: {line}')
    # awaiting verification: a rollover happened, the stale pre-rollover measurement is not due
    write(h, state={'verify': {'before_tokens': 940000, 'at': '2026-10-08T11:00:00Z', 'reason': 'tokens', 'old_id': 'a', 'new_id': 'b'}},
          turn={'n': 4, 'at': time.time(), 'engine': 'claude-r2d2', 'events': [], 'kind': 'compaction'})
    line = S.compaction_status_line(h, now=now)
    check(line.startswith('context: 940000 tokens (turn 3)') and line.endswith('; compaction: awaiting verification'), f'awaiting verification: {line}')
    # a stale pending record still present never supplies the number: the last measured context does
    write(h, state={'pending': {'reason': 'tokens', 'value': 999999, 'since': '2026-10-08T07:09:40Z'}},
          turn={'n': 4, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 94586})
    line = S.compaction_status_line(h, now=now)
    check(line.startswith('context: 94586 tokens (turn 4)') and '999999' not in line, f'a present stale pending must not supply the number: {line}')
    # the CLI compaction as the newest event; no suffix below the limit
    write(h, state={'last': {'before_tokens': 940000, 'after_tokens': 90000, 'at': '2026-10-08T11:00:00Z', 'ok': True, 'reason': 'tokens'},
                    'cli_compactions': [{'at': '2026-10-08T12:00:00Z', 'turn': 5}]},
          turn={'n': 5, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 90000, 'cli_compacted': True})
    line = S.compaction_status_line(h, now=now)
    check('last compaction: 2026-10-08T12:00:00Z (cli)' in line and '; compaction' not in line, f'cli last: {line}')
    check('compaction: pending (' not in line, 'the old wording must never appear')


def stale_pending_regression():
    """Requirement 4 (the 2026-10-08 case): a stale pending of 935,607, a CLI boundary, then a measured 94,586:
    status shows 94,586 and the CLI compaction, no suffix; nothing is due."""
    good = {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-10-08T09:40:58.542Z'}
    h, d = home(), fakes(context=935607, context_after=94586)
    config(h, rollover_enabled=False)
    # the clock runs from the diagnosis's pending time, so the held outcome (07:09Z) is older than the CLI boundary (09:40Z)
    sup = make(h, d, clock=Clock(dt.datetime(2026, 10, 8, 7, 9, 40, tzinfo=dt.timezone.utc)))
    tdir = transcript_dir(h); tdir.mkdir(parents=True)
    tpath = tdir / 'sess-test.jsonl'; tpath.write_text('')
    event(h, 'A'); sup.run_once()
    check((cstate(h).get('pending') or {}).get('value') == 935607, 'the stale pending of the diagnosis')
    with tpath.open('a') as f:
        f.write(json.dumps(good) + '\n')
    (d / 'context').write_text('94586')
    event(h, 'B'); sup.run_once()
    line = status_line(h)
    check(line.startswith('context: 94586 tokens'), f'status must show the last measured context, not the pending value: {line}')
    check('(cli)' in line and 'last compaction: 2026-10-08T09:40:58' in line and '; compaction' not in line, f'the CLI compaction last and no suffix: {line}')
    held = cstate(h).get('last') or {}
    check(str(held.get('error', '')).startswith('held:') and str(held.get('at', '')) < '2026-10-08T09:40', f'the held outcome must be older than the boundary: {held}')
    check(not cstate(h).get('pending'), 'a measurement below the threshold clears the stale pending')
    due, _ = sup.compaction_due()
    check(not due, 'scheduling must never act on a stale pending value')


def bytes_overage_compacts():
    """Requirement 1 (bytes) and 7j: a session file grown past max_bytes with the context below the threshold compacts
    before the next queued event; suppressed during a failure back-off, retried after it."""
    h, d = home(), fakes(context=100000, context_after=90000)
    config(h, max_bytes=1000, retry_after_s=3600)
    clock = Clock(dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc))
    sup = make(h, d, clock=clock)
    tdir = transcript_dir(h); tdir.mkdir(parents=True)
    tpath = tdir / 'sess-test.jsonl'; tpath.write_text('x' * 5000 + '\n')
    event(h, 'A'); sup.run_once()
    pending = cstate(h).get('pending') or {}
    check(pending.get('reason') == 'bytes', f'a bytes pending expected after the grown session file: {pending}')
    due, reason = sup.compaction_due()
    check(due and reason == 'bytes', f'compaction_due must be (True, "bytes"), got {(due, reason)}')
    (d / 'fail_compact').write_text('1')
    event(h, 'B'); sup.run_once()
    check(kinds(d) == ['turn', 'compaction', 'turn'], f'the bytes overage must attempt the rollover before B: {kinds(d)}')
    event(h, 'C'); sup.run_once()
    check(kinds(d).count('compaction') == 1, 'no second attempt within the failure back-off')
    (d / 'fail_compact').unlink()
    clock.advance(3700)
    event(h, 'D'); sup.run_once()
    check(kinds(d).count('compaction') == 2, f'after the back-off the bytes overage is retried: {kinds(d)}')
    check(session_id(h) != 'sess-test', 'the retried rollover must replace the session id')
    # rollover disabled: a persisting bytes overage with a low measured context is one held episode, one line
    h2, d2 = home(), fakes(context=100000)
    config(h2, max_bytes=1000, rollover_enabled=False)
    blog2 = Poster()
    sup2 = make(h2, d2, blog=blog2)
    t2 = transcript_dir(h2); t2.mkdir(parents=True)
    (t2 / 'sess-test.jsonl').write_text('x' * 5000 + '\n')
    for name in ('A', 'B', 'C'):
        event(h2, name); sup2.run_once()
    check(kinds(d2) == ['turn', 'turn', 'turn'], f'held: no engine compaction call: {kinds(d2)}')
    held2 = [x for _, _, x in blog2.posted if 'compaction held' in x]
    check(len(held2) == 1, f'a persisting bytes overage with rollover disabled must post exactly one held line: {held2}')
    check((cstate(h2).get('pending') or {}).get('reason') == 'bytes', 'the bytes pending must survive low-context turns')


def codex_selected_ignores_stale_claude_measurement():
    """Requirement 1's engine-family guard: with Codex selected, a stale 3x Claude measurement compacts nothing."""
    h, d = home(), fakes(context=940000, context_after=90000)
    sup = make(h, d)
    event(h, 'A'); sup.run_once()
    check(turns(h)[-1].get('context_tokens') == 940000, 'the Claude measurement')
    Path(h, 'engine').write_text('codex\n')
    due, _ = sup.compaction_due()
    check(not due, 'nothing is due while Codex is selected')
    event(h, 'B'); sup.run_once()
    check(kinds(d) == ['turn', 'codex'], f'the Codex turn must run without a compaction: {kinds(d)}')
    Path(h, 'engine').write_text('claude-r2d2\n')
    due, reason = sup.compaction_due()
    check(due and reason == 'tokens', 'back on Claude the overage is due again')


def big_state(n_tracks=24, entry_kb=5):
    tracks = []
    for i in range(n_tracks):
        tid = f't{i:02d}'
        tracks.append({'id': tid, 'repo': 'faden', 'title': f'track {tid}', 'now': f'now of {tid}: ' + ('x' * 100),
                       'waits_on': f'waits {tid}', 'next_action': f'next {tid}', 'thread': f'17910000{i:02d}.000001',
                       'history_file': f'factory/log/tracks/{tid}.md',
                       'detail': {'marker': f'MARKER-{tid}-DEEP', 'nested': {'thread': f'17919999{i:02d}.000001'},
                                  'blob': 'y' * (entry_kb * 1024)}})
    return {'schema_version': 2, 'updated': '2026-10-08T10:00:00+00:00',
            'founder_decisions_in_force': [f'2026-10-{d:02d}: decision number {d} ' + ('z' * 200) for d in range(1, 31)],
            'tracks': tracks, 'next_gate': 'the next gate text', 'waits_on': 'the waits text'}



def digest_bytes(msg):
    """The digest section of an engine message: from its header line to its terminator line, inclusive."""
    check('# factory/state.json (digest' in msg, 'digest header missing')
    start = msg.index('# factory/state.json (digest')
    check('## end of state digest' in msg[start:], 'digest terminator line missing')
    end = msg.index('## end of state digest', start)
    return len(msg[start:end + len('## end of state digest')].encode('utf-8'))


def state_digest_bounds_the_message():
    """Requirement 5: every track's one-line fields, the full entries of the touched tracks only, the newest
    decisions, under 40,000 bytes, for Claude and Codex, message_bytes on every record, both assignee forms, the
    mandatory-part overflow policy."""
    h, d = home(), fakes(context=100000)
    state = big_state()
    Path(h, 'state.json').write_text(json.dumps(state, indent=1))
    raw = len(Path(h, 'state.json').read_bytes())
    check(raw > 120000, f'fixture state must be real-sized (>120 KB), got {raw}')
    Path(h, 'work-status.json').write_text(json.dumps({'turn': 0, 'mode': 'done', 'track': 't21'}))
    sup = make(h, d)
    event(h, 'what is new? éè — café', thread='1791000005.000001')          # (a) thread of t05, non-ASCII text
    event(h, '<@U0FAKE> assignee: Hermes | track: t11\nrun the thing', thread='9.9')  # (b) assignee line, mention-prefixed
    event(h, 'continue t17: carry on', thread='8.8')                           # (c) continue
    check(sup.run_once() is True, 'turn did not run')
    stdin = calls(d)[-1]['stdin']
    size = len(stdin.encode('utf-8'))
    check(size < 40000, f'the message must be under 40,000 bytes, got {size}')
    check('# factory/state.json (digest' in stdin, 'digest header missing')
    check(digest_bytes(stdin) <= 36000, f'the digest section alone must be at most 36,000 bytes: {digest_bytes(stdin)}')
    check(stdin.count('# factory/state.json') == 1, 'exactly one state section')
    for t in state['tracks']:
        check(f"- {t['id']}: now: now of {t['id']}" in stdin, f"track line missing for {t['id']}")
    for tid in ('t05', 't11', 't17', 't21'):
        check(f'MARKER-{tid}-DEEP' in stdin, f'full entry of touched track {tid} missing')
    for t in state['tracks']:
        if t['id'] not in ('t05', 't11', 't17', 't21'):
            check(f"MARKER-{t['id']}-DEEP" not in stdin, f"untouched track {t['id']} must not be in full")
    check('decision number 30' in stdin and 'decision number 21' in stdin and 'decision number 20' not in stdin,
          'the newest 10 founder decisions, and only those, must be present')
    check('the next gate text' in stdin and 'the waits text' in stdin, 'next_gate and waits_on missing')
    rec = turns(h)[-1]
    check(rec.get('message_bytes') == size, f'message_bytes must equal the stdin length: {rec.get("message_bytes")} != {size}')
    check(size < raw, f'the digest must be smaller than the file it replaces: {size} vs {raw}')
    print(f'[b11.acceptance] message bytes before: {raw} after: {size}')  # the notes carry this pair (requirement 8)
    # the plain assignee form selects too; extra words after the track id do not
    event(h, 'assignee: Hermes | track: t03', thread='7.7')
    event(h, 'assignee: Hermes | track: t04 please', thread='6.6')
    sup.run_once()
    stdin = calls(d)[-1]['stdin']
    check('MARKER-t03-DEEP' in stdin, 'the plain assignee line must select its track')
    check('MARKER-t04-DEEP' not in stdin, 'an assignee line with extra words must not select')
    # the cap: four touched tracks of ~16 KB each cannot fit (one does); selection order is EVENT order (t17, t05,
    # t11, then the work-status track t21), not file order, so t17 survives and t05 (first in file order) is dropped
    state2 = big_state(entry_kb=16)
    Path(h, 'state.json').write_text(json.dumps(state2, indent=1))
    event(h, 'continue t17: carry on', thread='8.8')
    event(h, 'again', thread='1791000005.000001')
    event(h, '<@U0FAKE> assignee: Hermes | track: t11\nrun', thread='9.9')
    sup.run_once()
    stdin = calls(d)[-1]['stdin']
    size = len(stdin.encode('utf-8'))
    check(size < 40000 and digest_bytes(stdin) <= 36000, f'the caps must hold under oversized selections: message {size}, digest {digest_bytes(stdin)}')
    check('(truncated: 3 touched track(s) left out)' in stdin, f'the header must say three selected tracks were left out: {stdin[:200]!r}')
    check('MARKER-t17-DEEP' in stdin, 'the first-selected touched track (by event order) must survive truncation')
    check('MARKER-t05-DEEP' not in stdin, 'the first track in FILE order must not survive ahead of the first-selected one')
    check('MARKER-t21-DEEP' not in stdin, 'the last-selected touched track (work-status) must be dropped first')
    for t in state2['tracks']:
        check(f"- {t['id']}: now:" in stdin, f"track lines are never dropped: {t['id']}")
    # the mandatory part alone over the budget: 80 tracks with long fields and 30 long decisions still fit; no
    # work-status track is selected here (a full entry would legitimately carry the long fields)
    Path(h, 'work-status.json').write_text(json.dumps({'turn': 0, 'mode': 'done'}))
    state3 = big_state(n_tracks=80, entry_kb=1)
    for t in state3['tracks']:
        t['now'] = f"now of {t['id']}: " + ('n' * 230); t['waits_on'] = 'w' * 230; t['next_action'] = 'a' * 230
    Path(h, 'state.json').write_text(json.dumps(state3, indent=1))
    event(h, 'plain', thread='5.5')
    sup.run_once()
    stdin = calls(d)[-1]['stdin']
    check(len(stdin.encode('utf-8')) < 40000 and digest_bytes(stdin) <= 36000, f'the mandatory overflow policy must keep the caps: {len(stdin.encode("utf-8"))} / {digest_bytes(stdin)}')
    check('(mandatory part shortened)' in stdin, 'the header must say the mandatory part was shortened')
    for t in state3['tracks']:
        check(f"- {t['id']}:" in stdin, f"every track id must still appear: {t['id']}")
    # the degradation order: decisions down to the newest three first, then now cut to 120 (which fits), never 60
    check('decision number 30' in stdin and 'decision number 28' in stdin and 'decision number 27' not in stdin,
          'the newest three decisions must remain, the rest dropped')
    check('n' * 108 in stdin and 'n' * 109 not in stdin, 'every track line must be now cut to 120 bytes')
    check('w' * 50 not in stdin and 'a' * 50 not in stdin, 'waits_on/next_action must be gone from the shortened lines')
    # the supported boundary: 400 tracks, 56-byte four-byte-character ids, 30 decisions of 600 four-byte characters
    state4 = big_state(n_tracks=400, entry_kb=1)
    for i, t in enumerate(state4['tracks']):
        t['id'] = chr(0x1F600 + (i % 60)) * 13 + chr(0x1F600 + (i // 60))   # 14 chars x 4 bytes = 56 bytes, unique
        t['now'] = 'now ' + ('\U0001F4AA' * 60); t['waits_on'] = 'w' * 230; t['next_action'] = 'a' * 230
    state4['founder_decisions_in_force'] = ['\U0001F4DD' * 600 for _ in range(30)]
    Path(h, 'state.json').write_text(json.dumps(state4, indent=1, ensure_ascii=False))
    event(h, 'boundary', thread='5.5')
    sup.run_once()
    stdin = calls(d)[-1]['stdin']
    check(digest_bytes(stdin) <= 36000 and len(stdin.encode('utf-8')) < 40000, f'the supported boundary must fit: digest {digest_bytes(stdin)}')
    check('over budget' not in stdin, 'the supported boundary must never be over budget')
    for t in state4['tracks']:
        check(f"- {t['id']}" in stdin, 'every unicode id must appear')
    Path(h, 'work-status.json').write_text(json.dumps({'turn': 0, 'mode': 'done', 'track': 't21'}))
    # Codex gets the same digest and no second state section; message_bytes on every record incl. the compaction
    Path(h, 'state.json').write_text(json.dumps(state, indent=1))
    Path(h, 'engine').write_text('codex\n')
    event(h, 'codex turn', thread='1791000005.000001')
    sup.run_once()
    c = calls(d)[-1]
    check(c['kind'] == 'codex', f'expected a codex call, got {c["kind"]}')
    check(c['stdin'].count('# factory/state.json') == 1 and '(digest' in c['stdin'], 'codex must get exactly the digest')
    check('MARKER-t05-DEEP' in c['stdin'], 'codex digest must include the touched track')
    Path(h, 'engine').write_text('claude-r2d2\n')
    (d / 'context').write_text('940000')
    event(h, 'X'); sup.run_once()
    event(h, 'Y'); sup.run_once()
    check('compaction' in kinds(d), 'the overage must compact (for the compaction record)')
    recs = [r for r in turns(h) if r.get('message_bytes') is not None or r.get('kind') == 'compaction' or r.get('events')]
    stdins = [len(cl['stdin'].encode('utf-8')) for cl in calls(d)]
    check(len(recs) == len(stdins), f'one turn record per engine call expected: {len(recs)} records vs {len(stdins)} calls')
    for r, n in zip(recs, stdins):
        check(r.get('message_bytes') == n, f'message_bytes must match the captured stdin on every record: {r.get("n")} {r.get("kind")} {r.get("message_bytes")} != {n}')


def persistent_mode_covered():
    """Requirement 1/3/5 in Claude persistent mode (a fake stream-json child): the digest and message_bytes on the
    persistent turn record, the child reused across turns, the threshold rollover retiring it and replacing the
    session id before the next turn, a CLI boundary found in the new session's transcript."""
    h, d = home(), fakes(context=100000, context_after=90000)
    Path(h, 'engine').write_text(json.dumps({'acc': 'claude-r2d2', 'mode': 'persistent'}) + '\n')
    state = big_state()
    Path(h, 'state.json').write_text(json.dumps(state, indent=1))
    sup = make(h, d)
    try:
        event(h, 'persistent turn A: éè', thread='1791000005.000001')
        check(sup.run_once() is True, 'persistent turn A did not run')
        c = calls(d)[-1]
        check(c.get('persistent') is True, f'the turn must go through the stream-json child: {c.get("argv")}')
        check(digest_bytes(c['stdin']) <= 36000 and 'MARKER-t05-DEEP' in c['stdin'] and c['stdin'].count('# factory/state.json') == 1,
              'the persistent turn must carry the digest (touched track in full, one section)')
        rec = turns(h)[-1]
        check(rec.get('message_bytes') == len(c['stdin'].encode('utf-8')), f'message_bytes on the persistent record: {rec.get("message_bytes")}')
        pid_a = c.get('pid')
        event(h, 'persistent turn B'); sup.run_once()
        check(calls(d)[-1].get('pid') == pid_a, 'the child must be reused across ordinary turns')
        check(turns(h)[-1].get('message_bytes') == len(calls(d)[-1]['stdin'].encode('utf-8')), 'message_bytes on every persistent record')
        # the threshold rollover: the memory turn runs, the old child is retired, the id replaced before the next turn
        (d / 'context').write_text('940000')
        event(h, 'persistent turn C'); sup.run_once()
        check(turns(h)[-1].get('context_tokens') == 940000, f'turn C must measure 940000: {turns(h)[-1]}')
        event(h, 'persistent turn D'); sup.run_once()
        ks = [(x['kind'], x.get('pid')) for x in calls(d)]
        check([k for k, _ in ks] == ['turn', 'turn', 'turn', 'compaction', 'turn'], f'expected the compaction before turn D: {ks}')
        check(session_id(h) != 'sess-test', 'the rollover must replace the session id in persistent mode')
        check(ks[-1][1] != pid_a, f'the old child must be retired by the rollover (same pid served turn D): {ks}')
        gone = False
        for _ in range(50):
            try:
                st = Path(f'/proc/{pid_a}/stat').read_text()
                gone = st.split(')')[-1].split()[0] == 'Z'
            except FileNotFoundError:
                gone = True
            if gone:
                break
            time.sleep(0.1)
        check(gone, f'the retired child {pid_a} must have exited, not leaked')
        crec = [r for r in turns(h) if r.get('kind') == 'compaction']
        check(crec and crec[-1].get('message_bytes') == len(calls(d)[-2]['stdin'].encode('utf-8')), 'message_bytes on the compaction record')
        last = cstate(h).get('last') or {}
        check(last.get('ok') is True and last.get('after_tokens') == 90000, f'the verifying persistent turn must settle the rollover: {last}')
        # a CLI boundary in the new session's transcript is found in persistent mode
        tdir = transcript_dir(h); tdir.mkdir(parents=True, exist_ok=True)
        good = {'type': 'system', 'subtype': 'compact_boundary', 'timestamp': '2026-10-08T13:00:00.000Z'}
        (tdir / f'{session_id(h)}.jsonl').write_text(json.dumps(good) + '\n')
        event(h, 'persistent turn E'); sup.run_once()
        check(turns(h)[-1].get('cli_compacted') is True, 'the persistent turn after a boundary must be flagged')
        check('(cli)' in status_line(h), f'status must show the CLI compaction in persistent mode: {status_line(h)}')
    finally:
        with contextlib.suppress(Exception):
            sup._retire_persistent('planned', 'acceptance done')


# ---------------------------------------------------------------------------------------------- timer gating fixtures
FAKE_GH = r"""#!/usr/bin/env python3
import sys, json
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]


def norm(p):
    base, _, q = p.partition('?')
    params = sorted(kv for kv in q.split('&') if kv and not kv.startswith('since='))
    return base + ('?' + '&'.join(params) if params else '')


with open(D / 'gh-calls.log', 'a') as f:
    f.write(json.dumps(argv) + '\n')
if argv[:1] != ['api'] or len(argv) < 2 or any(a in ('--jq', '--paginate', '-q') for a in argv):
    print('fake gh: unsupported invocation', argv, file=sys.stderr); sys.exit(2)
path = norm(argv[1])
routes = json.loads((D / 'routes.json').read_text())
fails = json.loads((D / 'fails.json').read_text()) if (D / 'fails.json').exists() else []
if any(path.startswith(norm(f)) for f in fails):
    print('fake gh: HTTP 503', file=sys.stderr); sys.exit(1)
if path not in routes:
    print('fake gh: no route for ' + path, file=sys.stderr); sys.exit(1)
print(json.dumps(routes[path]))
"""

FAKE_SYSTEMCTL = r"""#!/usr/bin/env python3
import sys
from pathlib import Path
state = Path(%(state)r).read_text().strip()
print(state)
sys.exit(0 if state == 'active' else 3)
"""


def sha(seed):
    return hashlib.sha1(seed.encode()).hexdigest()


def pr(number, head, updated='2026-10-08T00:00:00Z', labels=(), comments=0, review_comments=0, login='someone',
       reviews=(), checks=()):
    return {'number': number, 'head': {'sha': head}, 'updated_at': updated, 'labels': [{'name': l} for l in labels],
            'comments': comments, 'review_comments': review_comments, 'user': {'login': login},
            'reviews': [{'state': r} for r in reviews], 'checks': list(checks)}


class FakeGitHub:
    """`gh api <path>` answered from a route table; a failing prefix exits 1."""
    def __init__(self):
        self.d = Path(tempfile.mkdtemp(prefix='b11-gh-'))
        self.routes = {}
        self.bin = self.d / 'gh'
        self.bin.write_text(FAKE_GH % {'dir': str(self.d)})
        self.bin.chmod(0o755)
        self.set('user', {'login': 'hydra-manager'})
        self.fail([])

    @staticmethod
    def norm(p):
        base, _, q = p.partition('?')
        params = sorted(kv for kv in q.split('&') if kv and not kv.startswith('since='))
        return base + ('?' + '&'.join(params) if params else '')

    def set(self, path, data):
        self.routes[self.norm(path)] = data
        (self.d / 'routes.json').write_text(json.dumps(self.routes))

    def repo(self, repo, head, prs=(), issues=()):
        self.set(f'repos/{repo}/commits/HEAD', {'sha': head})
        self.set(f'repos/{repo}/pulls?state=open&per_page=100', [{k: v for k, v in p.items() if k not in ('reviews', 'checks')} for p in prs])
        for p in prs:
            self.set(f"repos/{repo}/pulls/{p['number']}/reviews", p['reviews'])
            self.set(f"repos/{repo}/commits/{p['head']['sha']}/check-runs",
                     {'total_count': len(p['checks']), 'check_runs': [{'conclusion': c} for c in p['checks']]})
        self.set(f'repos/{repo}/issues?state=open&since=2000-01-01T00:00:00Z&per_page=100', list(issues))

    def fail(self, prefixes):
        (self.d / 'fails.json').write_text(json.dumps(list(prefixes)))

    def calls(self):
        p = self.d / 'gh-calls.log'
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


class Dogfood:
    """A fake systemctl, a local /health server whose status is a file, and a scratch git checkout."""
    def __init__(self):
        self.d = Path(tempfile.mkdtemp(prefix='b11-dog-'))
        self.state = self.d / 'unit-state'
        self.state.write_text('active\n')
        self.systemctl = self.d / 'systemctl'
        self.systemctl.write_text(FAKE_SYSTEMCTL % {'state': str(self.state)})
        self.systemctl.chmod(0o755)
        self.status = self.d / 'health-status'
        self.status.write_text('200')
        status_file = self.status

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code = int(status_file.read_text().strip() or '200')
                body = b'{"ok": true}' if code == 200 else b'{"ok": false}'
                self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, *a):
                pass
        self.server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/health'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.checkout = self.d / 'checkout'
        self.checkout.mkdir()
        self.git('init', '-q')
        self.commit('one')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.checkout), '-c', 'user.name=b11', '-c', 'user.email=b11@example.test', *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, msg):
        self.git('commit', '-q', '--allow-empty', '-m', msg)
        return self.git('rev-parse', 'HEAD')

    def close(self):
        self.server.shutdown()


def dead_pid():
    """A pid that was alive a moment ago and is now reaped."""
    p = subprocess.Popen(['sleep', '300'])
    pid = p.pid
    p.send_signal(signal.SIGKILL); p.wait()
    return pid


def coder_dir(root, name, pid, state='running'):
    d = Path(root, name)
    d.mkdir()
    Path(d, 'launcher.json').write_text(json.dumps({'pid': pid, 'HEAD': sha(name)}))
    Path(d, 'status.json').write_text(json.dumps({'state': state, 'exit_code': None}))
    return d


def checks_log(h):
    return S.read_jsonl(os.path.join(h, 'logs', 'timer-checks.jsonl'))


def snapshot(h):
    return json.loads(Path(h, 'logs', 'timer-snapshot.json').read_text())


class Ticker:
    """One timer tick per call, each in a fresh every_s slot; asserts exactly one timer event or none."""
    def __init__(self, sup, h, clock, every_s=60):
        self.sup, self.h, self.clock, self.every_s, self.n = sup, h, clock, every_s, 0

    def __call__(self, expect, label, forbid=()):
        self.clock.advance(self.every_s)
        before = len(S.pending_events(self.h)); checks_before = len(checks_log(self.h))
        rec = self.sup.timer_tick()
        self.n += 1
        new = S.pending_events(self.h)[before:]
        check(isinstance(rec, dict) and {'at', 'woke', 'reasons', 'cost_ms'} <= set(rec), f'{label}: timer_tick must return the check record, got {rec!r}')
        rows = checks_log(self.h)
        check(len(rows) == checks_before + 1 and rows[-1] == rec, f'{label}: every tick appends its record to timer-checks.jsonl: {rows[-1:]} vs {rec}')
        check(isinstance(rec['cost_ms'], int) and rec['cost_ms'] >= 0 and isinstance(rec['reasons'], list), f'{label}: record shape: {rec}')
        if expect is None:
            check(rec['woke'] is False and rec['reasons'] == [] and not new, f'{label}: must not wake, got {rec} and {len(new)} new event(s): {[e.get("payload", {}).get("text") for e in new]}')
            return None
        check(rec['woke'] is True and len(new) == 1, f'{label}: exactly one timer event expected, got {len(new)}; record {rec}')
        ev = new[0]; text = ev.get('payload', {}).get('text') or ''
        check(ev.get('source') == 'timer' and ev.get('id', '').startswith('timer-') and ev['payload'].get('instructs') is False,
              f'{label}: the timer event must be source timer, id timer-<slot>, instructs false: {ev}')
        check(text.startswith('timer: '), f'{label}: event text must start with "timer: ": {text!r}')
        expects = (expect,) if isinstance(expect, str) else tuple(expect)
        for e in expects:
            check(e in text, f'{label}: reason {e!r} missing from the event text {text!r}')
        for f in forbid:
            check(f not in text, f'{label}: reason {f!r} must not appear in the event text {text!r}')
        check(rec['reasons'] and all(r in text for r in rec['reasons']), f'{label}: the record reasons must be the event text reasons: {rec} vs {text!r}')
        return ev


def timer_setup(every_s=60, start=None):
    h, d = home(), fakes(context=100000)
    gh, dog = FakeGitHub(), Dogfood()
    coder = Path(tempfile.mkdtemp(prefix='b11-coder-'))
    Path(h, 'config.json').write_text(json.dumps({'timer': {
        'every_s': every_s, 'gh_bin': str(gh.bin), 'coder_logs_glob': str(coder / '*-logs'),
        'dogfood': {'unit': 'fitflow-dogfood', 'health_url': dog.url, 'checkout': str(dog.checkout), 'systemctl_bin': str(dog.systemctl)}}}))
    gh.repo(FADEN_REPO, head=sha('f1'), prs=[pr(261, sha('p1'), labels=('loop',), reviews=('APPROVED',), checks=('success',))])
    gh.repo(HYDRA_REPO, head=sha('h1'))
    clock = Clock(start or dt.datetime(2026, 10, 8, 15, 0, 0, tzinfo=dt.timezone.utc))   # 08:00 PDT, past the digest time
    sup = make(h, d, clock=clock)
    return h, d, gh, dog, coder, clock, sup, Ticker(sup, h, clock, every_s)


def timer_gating_sources():
    """Requirement 10: a tick wakes the model only on a difference, one timer event naming the reason; every
    source independently; the never-compared inputs; the check log and the status line."""
    h, d, gh, dog, coder, clock, sup, tick = timer_setup()
    try:
        tick('snapshot initialised', 'first check')
        check(Path(h, 'logs', 'timer-snapshot.json').exists(), 'the snapshot must be written after the first check')
        tick(None, 'unchanged')
        check(not any(c['kind'] != 'turn' for c in calls(d)) and calls(d) == [], f'the change check must not call an engine: {calls(d)}')
        # GitHub: main HEAD, the open set, the per-PR fields, issues updated since the last check
        gh.repo(FADEN_REPO, head=sha('f2'), prs=[pr(261, sha('p1'), labels=('loop',), reviews=('APPROVED',), checks=('success',))])
        tick(('faden main', sha('f1')[:7], sha('f2')[:7]), 'main head change', forbid=('hydra-agent', 'PR #261'))
        tick(None, 'unchanged after head')
        gh.repo(HYDRA_REPO, head=sha('h1'), prs=[pr(70, sha('q1'))])
        tick('hydra-agent PR #70', 'new open PR', forbid=('faden',))
        tick(None, 'unchanged after new PR')
        cur = dict(head=sha('p1'), labels=('loop',), reviews=('APPROVED',), checks=('success',))
        for field, kw in (('head', dict(head=sha('p2'))), ('updated_at', dict(updated='2026-10-08T01:00:00Z')),
                          ('labels', dict(labels=('loop', 'approved'))), ('comments', dict(comments=3)),
                          ('comments', dict(review_comments=2)), ('reviews', dict(reviews=('APPROVED', 'CHANGES_REQUESTED'))),
                          ('checks', dict(checks=('success', 'failure')))):
            cur.update(kw)
            p = pr(261, **cur)
            gh.repo(FADEN_REPO, head=sha('f2'), prs=[p])
            tick(('faden PR #261', field), f'PR {field} change')
            tick(None, f'unchanged after PR {field}')
        gh.repo(FADEN_REPO, head=sha('f2'), prs=[p], issues=[{'number': 58, 'updated_at': '2026-10-08T12:30:00Z', 'title': 'x'},
                                                      {'number': 59, 'updated_at': '2026-10-08T12:30:00Z', 'pull_request': {'url': 'u'}}])
        tick('faden issue #58', 'issue updated since the last check', forbid=('#59',))
        tick(None, 'the same issue list again does not wake (cumulative map)')
        gh.repo(FADEN_REPO, head=sha('f2'), prs=[p], issues=[{'number': 58, 'updated_at': '2026-10-08T12:45:00Z', 'title': 'x'}])
        tick('faden issue #58', 'issue updated again')
        gh.repo(FADEN_REPO, head=sha('f2'), prs=[], issues=[])
        tick('faden PR #261', 'PR closed (left the open set)')
        check(all(a[1].startswith(('repos/', 'user')) for a in gh.calls()), f'gh is called with REST paths only: {gh.calls()[:3]}')
        # coder runs
        child = subprocess.Popen(['sleep', '300'])
        try:
            coder_dir(coder, 'b99-logs', child.pid)
            tick('coder b99-logs', 'new coder logs dir')
            tick(None, 'unchanged coder dir')
            os.utime(Path(coder, 'b99-logs', 'status.json'))
            tick(None, 'mtime-only change of status.json')
            Path(coder, 'b99-logs', 'status.json').write_text(json.dumps({'state': 'done', 'exit_code': 0}))
            tick(('coder b99-logs', 'state', 'running', 'done'), 'status.json state change')
            tick(None, 'unchanged after state change')
            child.kill(); child.wait()
            tick(('coder b99-logs', 'pid', 'dead'), 'launcher pid died')
            tick(None, 'a dead pid stays dead without waking again')
            Path(coder, 'b99-logs', 'coder-exit.log').write_text('[b11] FAIL\n')
            tick(('coder b99-logs', 'coder-exit.log'), 'new exit file')
            Path(coder, 'b99-logs', 'coder-exit.log').write_text('[b11] FAIL again\n')
            tick(None, 'exit file content is not compared')
            Path(coder, 'b99-logs', 'result.json').write_text(json.dumps({'pass': False, 'coder_exit': 1}))
            tick(('coder b99-logs', 'result.json'), 'result.json appeared')
        finally:
            with contextlib.suppress(Exception):
                child.kill(); child.wait()
        # work-status deadlines
        now_epoch = sup.clock_epoch()
        Path(h, 'work-status.json').write_text(json.dumps({'turn': 1, 'mode': 'idle', 'track': 'ops', 'deadline': int(now_epoch) - 10, 'since': int(now_epoch) - 100}))
        tick('work-status deadline', 'idle deadline passed')
        tick(None, 'the same deadline wakes once')
        Path(h, 'work-status.json').write_text(json.dumps({'turn': 2, 'mode': 'waiting', 'track': 'ops', 'who': 'L', 'since': int(now_epoch)}))
        tick(None, 'waiting on L with no deadline never wakes')
        clock.advance(3600)
        tick(None, 'waiting on L with no deadline never wakes, an hour later')
        now_epoch = sup.clock_epoch()
        Path(h, 'work-status.json').write_text(json.dumps({'turn': 3, 'mode': 'waiting', 'track': 'b12', 'who': 'Hermes', 'deadline': int(now_epoch) + 30, 'since': int(now_epoch)}))
        tick(None, 'an operator deadline in the future does not wake')
        tick('work-status deadline', 'operator deadline passed')
        Path(h, 'work-status.json').write_text(json.dumps({'turn': 4, 'mode': 'continue', 'track': 'b12', 'next_action': 'x', 'deadline': int(now_epoch) - 10}))
        tick(None, 'continue stays with the self-event scheduler')
        Path(h, 'work-status.json').write_text(json.dumps({'turn': 5, 'mode': 'done', 'deadline': int(now_epoch) - 10}))
        tick(None, 'done never wakes')
        # persistence
        Path(h, 'logs', 'persistence.json').write_text(json.dumps({'last_turn': 5, 'status': 'blocked', 'error': 'push failed', 'pending_local_sha': sha('x')}))
        tick(('persistence', 'blocked'), 'persistence blocked')
        tick(None, 'staying blocked does not wake again')
        Path(h, 'logs', 'persistence.json').write_text(json.dumps({'last_turn': 6, 'status': 'synced', 'error': None, 'pending_local_sha': None}))
        tick(None, 'a change back to synced is not a reason')
        # dogfood
        dog.state.write_text('inactive\n')
        tick(('dogfood unit', 'inactive'), 'unit inactive')
        tick(None, 'staying inactive does not wake again')
        dog.state.write_text('active\n')
        tick(('dogfood unit', 'active'), 'unit back')
        dog.status.write_text('503')
        tick(None, 'one non-200 health answer does not wake')
        tick(('dogfood /health', '503', 'twice'), 'two consecutive non-200 answers')
        tick(None, 'a continuing health streak wakes once')
        dog.status.write_text('200')
        tick(None, 'health recovered: no wake')
        dog.status.write_text('503')
        tick(None, 'a new streak, first answer')
        tick(('dogfood /health', 'twice'), 'a new streak, second answer')
        dog.status.write_text('200')
        new_head = dog.commit('two')
        tick(('dogfood sha', new_head[:7]), 'deployed sha changed')
        dog.git('branch', 'scratch-not-a-pr')
        tick(None, 'a non-PR branch is not compared')
        # engine-runtime
        Path(h, 'engine-runtime.json').write_text(json.dumps({'episode_id': 'ep-1', 'active': True, 'reason': 'usage'}))
        tick(('fallback episode ep-1', 'started'), 'fallback started')
        tick(None, 'an active episode stays active without waking again')
        Path(h, 'engine-runtime.json').write_text(json.dumps({'episode_id': 'ep-1', 'active': False, 'reason': 'usage'}))
        tick(('fallback episode ep-1', 'ended'), 'fallback ended')
        # never compared: Slack, the manager's own files, mtime-only changes
        Path(h, 'state.json').write_text(json.dumps({'tracks': [{'id': 'ops', 'now': 'changed'}]}))
        Path(h, 'MANAGER-HANDOFF.md').write_text('tracks: changed\n')
        Path(h, 'mirror', 'MEMORY.md').write_text('changed\n')
        Path(h, 'work-status.json').write_text(json.dumps({'turn': 6, 'mode': 'waiting', 'who': 'L', 'since': 1}))
        event(h, 'a slack message', thread='3.3')
        queued = len(S.pending_events(h))
        tick(None, 'Slack, state.json, the handoff and memory files are never reasons')
        check(len(S.pending_events(h)) == queued, 'the tick must neither drop nor add events')
        # the log and the status line
        rows = checks_log(h)
        check(len(rows) == tick.n, f'one timer-checks row per tick: {len(rows)} vs {tick.n}')
        for r in rows:
            check({'at', 'woke', 'reasons', 'cost_ms'} <= set(r) and dt.datetime.fromisoformat(r['at'].replace('Z', '+00:00')), f'malformed check row: {r}')
        status = S.status_text(h)
        line = next((l for l in status.splitlines() if l.startswith('timer: ')), '')
        check(re.match(r'^timer: every 60s \| last check \S+ \(\d+ ms\) \| last wake \S+: .*fallback episode ep-1 ended', line),
              f'status must show the interval, the last check and the last wake reason: {line!r} in {status!r}')
    finally:
        dog.close()


def timer_check_failures():
    """Requirement 10: an unreadable source keeps its snapshot entry, never stops the other sources, wakes once
    after three consecutive failures, and a success resets the streak."""
    h, d, gh, dog, coder, clock, sup, tick = timer_setup()
    try:
        tick('snapshot initialised', 'first check')
        gh.fail([f'repos/{FADEN_REPO}/'])
        gh.repo(HYDRA_REPO, head=sha('h2'))
        tick(('hydra-agent main', sha('h2')[:7]), 'the other repo is still read while faden fails', forbid=('check failing',))
        tick(None, 'second consecutive failure: no wake')
        tick(f'check failing: github:{FADEN_REPO}', 'third consecutive failure wakes once')
        tick(None, 'fourth failure: no wake')
        tick(None, 'fifth failure: no wake')
        snap = snapshot(h)
        check((snap.get('failures') or {}).get(f'github:{FADEN_REPO}') == 5, f'the snapshot counts the failure streak: {snap.get("failures")}')
        check(snap.get('github', {}).get(FADEN_REPO, {}).get('head') == sha('f1'), f'the failing source keeps its previous snapshot entry: {snap.get("github", {}).get(FADEN_REPO)}')
        gh.fail([])
        tick(None, 'recovery with unchanged data: no wake')
        check(not (snapshot(h).get('failures') or {}).get(f'github:{FADEN_REPO}'), 'a success resets the streak')
        gh.fail([f'repos/{FADEN_REPO}/'])
        tick(None, 'new streak 1'); tick(None, 'new streak 2')
        tick(f'check failing: github:{FADEN_REPO}', 'new streak 3 wakes again')
        gh.fail([])
        tick(None, 'recovered')
        pers = Path(h, 'logs', 'persistence.json'); pers.write_text('{not json')
        tick(None, 'invalid persistence json 1'); tick(None, 'invalid persistence json 2')
        tick('check failing: persistence', 'invalid persistence json 3')
        pers.write_text(json.dumps({'status': 'synced', 'error': None, 'pending_local_sha': None}))
        tick(None, 'persistence readable again (synced is no reason)')
        Path(coder, 'not-a-dir-logs').write_text('a file where a directory is expected')
        tick(None, 'a stray file matching the coder glob is skipped, not a failure')
    finally:
        dog.close()


def timer_daily_digest():
    """Requirement 10: the daily digest fires once per local date at timer.digest_at (America/Los_Angeles by
    default), with digest: true in the payload; a snapshot initialised after that time does not catch up."""
    h, d, gh, dog, coder, clock, sup, tick = timer_setup(start=dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc))  # 05:00 PDT
    try:
        tick('snapshot initialised', 'first check', forbid=('daily digest',))
        target = dt.datetime(2026, 10, 8, 13, 58, 0, tzinfo=dt.timezone.utc)
        clock.advance(max(0, (target - clock.now).total_seconds()))
        tick(None, 'two minutes before the digest time')
        clock.advance(120)
        ev = tick('timer: daily digest', 'digest at 07:00 local')
        check(ev['payload'].get('digest') is True, f'the digest event carries digest: true: {ev["payload"]}')
        tick(None, 'no second digest the same date')
        clock.advance(6 * 3600)
        tick(None, 'nor six hours later')
        clock.advance(18 * 3600)
        ev = tick('timer: daily digest', 'the next day digests again')
        check(ev['payload'].get('digest') is True, 'digest: true on the next day too')
    finally:
        dog.close()
    h, d, gh, dog, coder, clock, sup, tick = timer_setup(start=dt.datetime(2026, 10, 8, 20, 0, 0, tzinfo=dt.timezone.utc))  # 13:00 PDT
    try:
        tick('snapshot initialised', 'first check after the digest time', forbid=('daily digest',))
        tick(None, 'no catch-up digest after deployment')
        clock.advance(3600)
        tick(None, 'nor an hour later')
    finally:
        dog.close()


def timer_max_silence():
    """Requirement 10: max silence wakes only while something is in flight (a live coder run, an open PR by the
    manager, an operator deadline), never when only L is pending; the silence clock restarts at every wake."""
    h, d, gh, dog, coder, clock, sup, tick = timer_setup(start=dt.datetime(2026, 10, 8, 20, 0, 0, tzinfo=dt.timezone.utc))  # 13:00 PDT
    cfg = json.loads(Path(h, 'config.json').read_text()); cfg['timer']['max_silence_s'] = 1800
    Path(h, 'config.json').write_text(json.dumps(cfg))
    try:
        tick('snapshot initialised', 'first check')
        Path(h, 'work-status.json').write_text(json.dumps({'turn': 1, 'mode': 'waiting', 'track': 'ops', 'who': 'L', 'since': int(sup.clock_epoch())}))
        clock.advance(1800 + 120)
        tick(None, 'max silence with only L pending never wakes')
        clock.advance(1800 + 120)
        tick(None, 'still never, later')
        child = subprocess.Popen(['sleep', '600'])
        try:
            coder_dir(coder, 'b98-logs', child.pid)
            tick('coder b98-logs', 'a live coder run appears')
            clock.advance(900)
            tick(None, 'fifteen minutes of silence is under the limit')
            clock.advance(1200)
            tick(('timer: max silence', 'in flight', 'b98-logs'), 'max silence with a live coder run')
            tick(None, 'right after the forced wake nothing is due')
            clock.advance(900)
            tick(None, 'the silence clock restarted at the forced wake')
            child.kill(); child.wait()
            tick(('coder b98-logs', 'dead'), 'the run ended')
            clock.advance(1800 + 120)
            tick(None, 'a dead run is not in flight; only L pending: no max-silence wake')
        finally:
            with contextlib.suppress(Exception):
                child.kill(); child.wait()
        gh.repo(HYDRA_REPO, head=sha('h1'), prs=[pr(71, sha('m1'), login='hydra-manager')])
        tick('hydra-agent PR #71', 'the manager opened a PR')
        clock.advance(1800 + 120)
        tick(('timer: max silence', 'in flight', 'PR #71'), 'max silence with an open PR by the manager')
        gh.repo(HYDRA_REPO, head=sha('h1'), prs=[pr(72, sha('m2'), login='someone-else')])
        tick(('PR #71', 'PR #72'), 'the manager PR closed, another opened')
        clock.advance(1800 + 120)
        tick(None, 'a PR by someone else is not the manager in flight')
        now_epoch = int(sup.clock_epoch())
        Path(h, 'work-status.json').write_text(json.dumps({'turn': 2, 'mode': 'waiting', 'track': 'b12', 'who': 'Hermes', 'deadline': now_epoch + 10 * 24 * 3600, 'since': now_epoch}))
        tick(None, 'a new waiting record is not itself a reason')
        clock.advance(1800 + 120)
        tick(('timer: max silence', 'in flight', 'Hermes'), 'max silence with an operator deadline')
        line = next((l for l in S.status_text(h).splitlines() if l.startswith('timer: ')), '')
        check('last wake' in line and 'max silence' in line, f'status shows the last wake reason: {line!r}')
        rows = checks_log(h)
        check(len(rows) == tick.n and sum(1 for r in rows if r['woke']) == 8, f'eight wakes in {len(rows)} checks expected: {[r["reasons"] for r in rows if r["woke"]]}')
    finally:
        dog.close()


def timer_service_loop():
    """Requirement 10/11: the service loop itself calls timer_tick once per elapsed every_s slot on the supervisor's
    clock, never while paused, and adopts a changed interval without a restart (driven through the injected sleep)."""
    h, d = home(), fakes(context=100000)
    coder = Path(tempfile.mkdtemp(prefix='b11-coder-'))

    def configure(every_s):
        Path(h, 'config.json').write_text(json.dumps({'timer': {'every_s': every_s, 'github_repos': [], 'coder_logs_glob': str(coder / '*-logs')}}))
    configure(60)
    start = dt.datetime(2026, 10, 8, 15, 0, 0, tzinfo=dt.timezone.utc)
    clock = Clock(start)
    stop = threading.Event()

    class Stop(BaseException):
        pass

    def fake_sleep(seconds):
        if stop.is_set():
            raise Stop()
        clock.advance(max(float(seconds), 1.0))
    sup = make(h, d, clock=clock, sleep=fake_sleep)
    ticks = []
    real = sup.timer_tick

    def spy(now=None):
        ticks.append(sup.clock_epoch())
        return real(now)
    sup.timer_tick = spy
    errors = []

    def run():
        try:
            sup.loop(sleep_s=5)
        except Stop:
            pass
        except BaseException as e:  # noqa: BLE001 - surfaced by the contract
            errors.append(repr(e))
    t = threading.Thread(target=run, daemon=True)
    t.start()

    def wait_clock(seconds, label):
        target = clock.now + dt.timedelta(seconds=seconds)
        deadline = time.time() + 120
        while clock.now < target and time.time() < deadline and t.is_alive():
            time.sleep(0.02)
        check(t.is_alive() and not errors, f'{label}: the loop must keep running: {errors}')
        check(clock.now >= target, f'{label}: the loop must sleep through the injected sleep (clock did not advance)')
    try:
        wait_clock(600, 'ten minutes at 60 s')
        n = len(ticks)
        check(9 <= n <= 12, f'about ten timer_tick calls in ten minutes at every_s 60, got {n}')
        slots = [int(ts // 60) for ts in ticks]
        check(len(slots) == len(set(slots)), f'at most one timer_tick per slot: {slots}')
        Path(h, 'PAUSE').write_text('acceptance\n')
        wait_clock(300, 'paused')
        check(len(ticks) == n, f'no timer_tick while paused: {len(ticks) - n} call(s)')
        Path(h, 'PAUSE').unlink()
        configure(120)
        wait_clock(600, 'ten minutes at 120 s')
        m = len(ticks) - n
        check(4 <= m <= 6, f'about five timer_tick calls in ten minutes at every_s 120 (re-read without a restart), got {m}')
        rows = checks_log(h)
        check(len(rows) == len(ticks), f'every loop-driven tick appends its check record: {len(rows)} rows vs {len(ticks)} calls')
        check(sum(1 for r in rows if r['woke']) == 1 and rows[0]['reasons'] == ['snapshot initialised'], f'only the initialising check wakes with nothing changing: {[r["reasons"] for r in rows if r["woke"]]}')
        check(len([r for r in turns(h) if r.get('events')]) == 1, 'exactly one turn (the initialising wake) ran')
    finally:
        stop.set()
        t.join(10)


def timer_interval_config():
    """Requirement 11: timer.every_s (and the other timer keys) come from config.json, re-read each time, clamped;
    the module has no TIMER_EVERY_S / TIMER_TEXT constants."""
    check(not hasattr(S, 'TIMER_EVERY_S'), 'TIMER_EVERY_S must be gone from the module')
    check(not hasattr(S, 'TIMER_TEXT'), 'TIMER_TEXT must be gone from the module')
    h = home()
    cfg = S.timer_config(h)
    for k, v in (('every_s', 900), ('digest_at', '07:00'), ('digest_tz', 'America/Los_Angeles'), ('max_silence_s', 14400),
                 ('gh_bin', 'gh'), ('github_repos', [FADEN_REPO, HYDRA_REPO]), ('coder_logs_glob', '/srv/hydra/coder/*-logs')):
        check(cfg.get(k) == v, f'timer default {k} must be {v!r}, got {cfg.get(k)!r}')
    check(S.TIMER_DEFAULTS['every_s'] == 900, 'TIMER_DEFAULTS.every_s must be 900')
    sup = make(h, fakes())
    check(sup.add_timer_event(now=1000.0)['id'] == 'timer-1', 'the default slot is 900 s (baseline contract)')
    Path(h, 'config.json').write_text(json.dumps({'timer': {'every_s': 120, 'max_silence_s': 10}}))
    cfg = S.timer_config(h)
    check(cfg['every_s'] == 120 and cfg['max_silence_s'] == 120, f'every_s 120 and max_silence_s clamped up to it: {cfg}')
    ev = sup.add_timer_event(now=1000000.0)
    check(ev and ev['id'] == f'timer-{int(1000000.0 // 120)}', f'the slot id must use the configured every_s without a restart: {ev}')
    Path(h, 'config.json').write_text(json.dumps({'timer': {'every_s': 5}}))
    check(S.timer_config(h)['every_s'] == 30, 'every_s below 30 is clamped to 30')
    ev = sup.add_timer_event(now=1000120.0)
    check(ev and ev['id'] == f'timer-{int(1000120.0 // 30)}', f'a changed interval is re-read: {ev}')
    Path(h, 'config.json').write_text('{not json')
    check(S.timer_config(h)['every_s'] == 900, 'an unreadable config falls back to the defaults')


SCENARIOS = [threshold_compacts_before_next_turn, threshold_boundaries, quiet_hours_do_not_gate,
             bytes_overage_compacts, codex_selected_ignores_stale_claude_measurement,
             held_is_loud_and_keeps_the_schedule, failed_rollover_backs_off, cli_compactions_detected,
             status_line_contract, stale_pending_regression, state_digest_bounds_the_message, persistent_mode_covered,
             timer_gating_sources, timer_check_failures, timer_daily_digest, timer_max_silence, timer_service_loop,
             timer_interval_config]


def main(argv):
    only = argv[argv.index('--only') + 1] if '--only' in argv else None
    for fn in SCENARIOS:
        if only and fn.__name__ != only:
            continue
        try:
            fn()
        except Fail as e:
            print(f'[b11.acceptance] FAIL {fn.__name__}: {e}')
            return 1
        except Exception as e:  # noqa: BLE001 - a crash inside a contract is a failure, reported the same way
            print(f'[b11.acceptance] FAIL {fn.__name__}: {type(e).__name__}: {e}')
            return 1
        print(f'[b11.acceptance] ok {fn.__name__}')
    print('[b11.acceptance] PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
