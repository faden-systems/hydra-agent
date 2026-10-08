#!/usr/bin/env python3
"""Exit-owned offline b11 contracts (loops/b11.md). A temporary home, recording fake engines that speak the
supervisor's stream-json, a controlled clock and fake posters only: no real engine, GitHub or Slack. Every check
raises (no `assert` in the contracts, so `python -O` cannot skip one). `--only <name>` runs one scenario."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'manager'))
import supervisor as S  # noqa: E402

HYDRA = str(ROOT / 'manager' / 'hydra')
THRESHOLD = 300000
LIMIT = int(1.25 * THRESHOLD)


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
msg = sys.stdin.read() if not sys.stdin.isatty() else ''
argv = sys.argv[1:]
with open(D / 'calls.jsonl', 'a') as f:
    f.write(json.dumps({'argv': argv, 'stdin': msg, 'kind': 'compaction' if '[compaction]' in msg else 'turn'}) + '\n')
assert '/compact' not in argv, 'obsolete compaction invocation'
sid = argv[argv.index('--session-id') + 1] if '--session-id' in argv else (argv[argv.index('--resume') + 1] if '--resume' in argv else '')
if '[compaction]' in msg:
    if (D / 'fail_compact').exists():
        print('usage limit reached', file=sys.stderr); sys.exit(1)
    m = Path(os.environ['HYDRA_MEMORY_DIR']) / 'MEMORY.md'
    m.write_text(m.read_text() + '\nfixture memory flushed ' + sid + '\n')
    reply = 'compacted\n---HANDOFF---\ntracks: t1\nwaiting on: none\nlast decision: flush\nnext action: resume\nopen question: none'
    ctx = 20000
else:
    reply = 'REPLY: ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none'
    ctx = int((D / ('context_after' if sid != 'sess-test' and (D / 'context_after').exists() else 'context')).read_text())
usage = {'input_tokens': 100, 'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': ctx - 1100, 'output_tokens': 50}
print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'usage': usage, 'content': [{'type': 'text', 'text': reply}]}}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'result': reply, 'session_id': sid,
                  'usage': {'input_tokens': usage['input_tokens'] * 3, 'cache_creation_input_tokens': usage['cache_creation_input_tokens'] * 3,
                            'cache_read_input_tokens': usage['cache_read_input_tokens'] * 3, 'output_tokens': 150}}))
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


def make(h, d, clock=None, blog=None, poster=None):
    return S.Supervisor(home=h, engines=engines(d), poster=poster or Poster(), buildlog_poster=blog or Poster(),
                        clock=clock or Clock(dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=dt.timezone.utc)))


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

import threading


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


def over_ratio_boundaries():
    """Requirement 1: due at or above over_ratio x threshold, not below; over_ratio configurable, clamped to >= 1."""
    def compacts(context, **cfg):
        h, d = home(), fakes(context=context, context_after=90000)
        if cfg:
            config(h, **cfg)
        sup = make(h, d)
        event(h, 'A'); sup.run_once()
        event(h, 'B'); sup.run_once()
        return 'compaction' in kinds(d)
    check(not compacts(350000), '1.17x must stay pending, not compact')
    check(compacts(LIMIT), f'exactly 1.25x ({LIMIT}) must compact')
    check(not compacts(450000, over_ratio=2.0), '1.5x with over_ratio 2.0 must not compact')
    check(compacts(600000, over_ratio=2.0), '2.0x with over_ratio 2.0 must compact')
    h = home(); config(h, over_ratio=0.5)
    sup = make(h, fakes())
    check(sup.compaction_config()['over_ratio'] == 1.0, 'over_ratio below 1.0 must be clamped to 1.0')


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
    event(h, 'A'); sup.run_once()
    event(h, 'B'); sup.run_once()
    check(kinds(d) == ['turn', 'turn'], f'a held rollover must make no [compaction] engine call: {kinds(d)}')
    last = cstate(h).get('last') or {}
    check(str(last.get('error', '')).startswith('held:') and last.get('ok') is None, f'held outcome not recorded: {last}')
    check(cstate(h).get('pending'), 'a held outcome must keep pending')
    run_ticks(sup, 2)
    held_lines = [t for _, _, t in blog.posted if 'compaction held' in t]
    check(len(held_lines) == 1, f'exactly one held buildlog line per episode, got {held_lines}')
    check('over' in held_lines[0], f'held line must name the overage: {held_lines[0]}')
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
    event(h, 'A'); sup.run_once()
    check(not turns(h)[-1].get('cli_compacted'), 'no boundary yet: the turn must not be flagged')
    with tpath.open('a') as f:
        f.write(json.dumps(good) + '\n')
    event(h, 'B'); sup.run_once()
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
    # truncation: the file is rewritten shorter with the same boundary -> rescanned, not duplicated
    tpath.write_text(json.dumps(good) + '\n')
    event(h, 'C2'); sup.run_once()
    check(len(cstate(h).get('cli_compactions') or []) == 1, 'a rescan after truncation must not duplicate a recorded boundary')
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
    check(len(cli) == 2 and cli[-1].get('at') == '2026-10-08T11:30:00.000Z', f'the boundary written before the rollover tick must be recorded by the compaction turn: {cli}')
    line = status_line(h)
    check('(cli)' not in line and '->' in line, f'an older boundary must not replace the newer supervisor compaction as the status last: {line}')
    # the scan path follows the new session id; a NEWER boundary there becomes the status last
    newpath = tdir / f'{session_id(h)}.jsonl'
    newpath.write_text(json.dumps(other) + '\n' + json.dumps(dict(good, timestamp='2026-10-08T13:00:00.000Z')) + '\n')
    event(h, 'F'); sup.run_once()
    cli = cstate(h).get('cli_compactions') or []
    check(len(cli) == 3 and cli[-1].get('at') == '2026-10-08T13:00:00.000Z', f'a boundary in the new transcript must be found: {cli}')
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
    check(line.startswith('context: unknown') and f'due at {LIMIT}' in line and 'last compaction: none' in line, f'no measurement: {line}')
    write(h, turn={'n': 1, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 100000})
    line = S.compaction_status_line(h, now=now)
    check(line.startswith('context: 100000 tokens (turn 1)') and '; compaction' not in line, f'below threshold: {line}')
    write(h, state={'pending': {'reason': 'tokens', 'value': 320000, 'limit': THRESHOLD, 'since': 'x'}},
          turn={'n': 2, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 320000})
    line = S.compaction_status_line(h, now=now)
    check(line.startswith('context: 320000 tokens (turn 2)') and '; compaction' not in line, f'pending between threshold and limit: {line}')
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
    check(size <= 40000, f'the cap must hold under oversized selections, got {size}')
    check('(truncated: 3 touched track(s) left out)' in stdin, f'the header must say three selected tracks were left out: {stdin[:200]!r}')
    check('MARKER-t17-DEEP' in stdin, 'the first-selected touched track (by event order) must survive truncation')
    check('MARKER-t05-DEEP' not in stdin, 'the first track in FILE order must not survive ahead of the first-selected one')
    check('MARKER-t21-DEEP' not in stdin, 'the last-selected touched track (work-status) must be dropped first')
    for t in state2['tracks']:
        check(f"- {t['id']}: now:" in stdin, f"track lines are never dropped: {t['id']}")
    # the mandatory part alone over the budget: 80 tracks with long fields and 30 long decisions still fit
    state3 = big_state(n_tracks=80, entry_kb=1)
    for t in state3['tracks']:
        t['now'] = f"now of {t['id']}: " + ('n' * 230); t['waits_on'] = 'w' * 230; t['next_action'] = 'a' * 230
    Path(h, 'state.json').write_text(json.dumps(state3, indent=1))
    event(h, 'plain', thread='5.5')
    sup.run_once()
    stdin = calls(d)[-1]['stdin']
    check(len(stdin.encode('utf-8')) <= 40000, f'the mandatory overflow policy must keep the cap: {len(stdin.encode("utf-8"))}')
    check('(mandatory part shortened)' in stdin, 'the header must say the mandatory part was shortened')
    for t in state3['tracks']:
        check(f"- {t['id']}:" in stdin, f"every track id must still appear: {t['id']}")
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


SCENARIOS = [threshold_compacts_before_next_turn, over_ratio_boundaries, quiet_hours_do_not_gate,
             bytes_overage_compacts, codex_selected_ignores_stale_claude_measurement,
             held_is_loud_and_keeps_the_schedule, failed_rollover_backs_off, cli_compactions_detected,
             status_line_contract, stale_pending_regression, state_digest_bounds_the_message]


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
