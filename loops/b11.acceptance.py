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
    one buildlog line per episode, and `hydra compact` reports the held outcome with exit 1."""
    h, d = home(), fakes(context=940000, context_after=90000)
    config(h, rollover_enabled=False)
    blog = Poster()
    sup = make(h, d, blog=blog)
    event(h, 'A'); sup.run_once()
    event(h, 'B'); sup.run_once()
    check(kinds(d) == ['turn', 'turn'], f'a held rollover must make no [compaction] engine call: {kinds(d)}')
    last = cstate(h).get('last') or {}
    check(str(last.get('error', '')).startswith('held:') and last.get('ok') is None, f'held outcome not recorded: {last}')
    check(cstate(h).get('pending'), 'a held outcome must keep pending')
    run_ticks(sup, 2)
    held_lines = [t for _, _, t in blog.posted if 'compaction held' in t]
    check(len(held_lines) == 1, f'exactly one held buildlog line per episode, got {held_lines}')
    check(f'over {LIMIT}' in held_lines[0] or 'over' in held_lines[0], f'held line must name the overage: {held_lines[0]}')
    Path(h, 'COMPACT').write_text('')
    sup.run_once()
    check(not Path(h, 'COMPACT').exists(), 'a forced request must be consumed, not retried every tick')
    check(kinds(d) == ['turn', 'turn'], 'the forced request with rollover disabled must not call the engine')
    check(len([t for _, _, t in blog.posted if 'compaction held' in t]) == 1, 'the same held episode must not post twice')
    check(cstate(h).get('pending'), 'pending must survive a consumed forced request while held')
    env = {**os.environ, 'HYDRA_HOME': h}
    r = subprocess.run([sys.executable, HYDRA, 'compact'], env=env, capture_output=True, text=True, timeout=60)
    out = (r.stdout or '') + (r.stderr or '')
    check(r.returncode == 1 and 'compaction held' in out, f'hydra compact must exit 1 and say held: rc={r.returncode} out={out[-300:]!r}')
    # a clearing turn (context below the threshold) ends the episode; the next overage posts again
    (d / 'context').write_text('100000')
    event(h, 'C'); sup.run_once()
    check(not cstate(h).get('pending'), 'a measured turn below the threshold must clear pending')
    (d / 'context').write_text('940000')
    event(h, 'D'); sup.run_once()
    event(h, 'E'); sup.run_once()
    check(len([t for _, _, t in blog.posted if 'compaction held' in t]) == 2, 'a new overage after a clearing turn is a new held episode')


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
    """Requirement 3: the module-level parser, then the per-turn scan, the turn flag, the record and the status."""
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
    line = status_line(h)
    check('(cli)' in line and 'last compaction: 2026-10-08T09:40:58' in line, f'status must show the CLI compaction last: {line}')
    event(h, 'C'); sup.run_once()
    check(len(cstate(h).get('cli_compactions') or []) == 1, 'a boundary must be counted once, not on every scan')
    # a later supervisor compaction takes precedence in status
    (d / 'context').write_text('940000')
    event(h, 'D'); sup.run_once()
    event(h, 'E'); sup.run_once()
    check('compaction' in kinds(d), 'overage after the CLI case must compact')
    line = status_line(h)
    check('(cli)' not in line and '->' in line, f'the newer supervisor compaction must be the status last: {line}')
    # a missing transcript never fails a turn
    h2, d2 = home(), fakes(context=100000)
    sup2 = make(h2, d2)
    event(h2, 'A'); check(sup2.run_once() is True, 'a missing transcript must not fail the turn')
    check(turns(h2)[-1].get('cli_compacted') in (False, None), 'no transcript: no flag')


def status_line_contract():
    """Requirement 4: one number, the due limit, the last compaction, at most one suffix; never the old wording."""
    def write(h, state=None, turn=None):
        if state is not None:
            S.write_text(S.compaction_state_path(h), json.dumps(state))
        if turn is not None:
            S.append_jsonl(os.path.join(h, 'logs', 'turns.jsonl'), turn)
    h = home()
    line = status_line(h)
    check(line.startswith('context: unknown') and f'due at {LIMIT}' in line and 'last compaction: none' in line, f'no measurement: {line}')
    write(h, turn={'n': 1, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 100000})
    line = status_line(h)
    check(line.startswith('context: 100000 tokens (turn 1)') and '; compaction' not in line, f'below threshold: {line}')
    write(h, state={'pending': {'reason': 'tokens', 'value': 320000, 'limit': THRESHOLD, 'since': 'x'}},
          turn={'n': 2, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 320000})
    line = status_line(h)
    check(line.startswith('context: 320000 tokens (turn 2)') and '; compaction' not in line, f'pending between threshold and limit: {line}')
    write(h, state={'pending': {'reason': 'tokens', 'value': 940000, 'limit': THRESHOLD, 'since': 'x'}},
          turn={'n': 3, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 940000})
    line = status_line(h)
    check(line.startswith('context: 940000 tokens (turn 3)') and line.endswith('; compaction: due (tokens)'), f'due: {line}')
    Path(h, 'COMPACT').write_text('')
    check(status_line(h).endswith('; compaction: forced'), f'forced: {status_line(h)}')
    Path(h, 'COMPACT').unlink()
    write(h, state={'pending': {'reason': 'tokens', 'value': 940000, 'limit': THRESHOLD, 'since': 'x'},
                    'last': {'before_tokens': 940000, 'after_tokens': None, 'at': '2026-10-08T09:58:22Z', 'ok': None,
                             'reason': 'forced', 'error': 'held: automatic rollover is disabled'}, 'failed_at': '2026-10-08T09:58:22Z'})
    line = status_line(h)
    check('last compaction: 2026-10-08T09:58:22Z (940000 -> held)' in line and line.endswith('; compaction: held'), f'held: {line}')
    write(h, state={'verify': {'before_tokens': 940000, 'at': '2026-10-08T11:00:00Z', 'reason': 'tokens'}},
          turn={'n': 4, 'at': time.time(), 'engine': 'claude-r2d2', 'events': [], 'kind': 'compaction'})
    line = status_line(h)
    check(line.endswith('; compaction: awaiting verification'), f'awaiting verification: {line}')
    write(h, state={'last': {'before_tokens': 940000, 'after_tokens': 90000, 'at': '2026-10-08T11:00:00Z', 'ok': True, 'reason': 'tokens'},
                    'cli_compactions': [{'at': '2026-10-08T12:00:00Z', 'turn': 5}]},
          turn={'n': 5, 'at': time.time(), 'engine': 'claude-r2d2', 'events': ['x'], 'context_tokens': 90000, 'cli_compacted': True})
    line = status_line(h)
    check('last compaction: 2026-10-08T12:00:00Z (cli)' in line and '; compaction' not in line, f'cli last: {line}')
    for case in ('context: unknown', 'context: 100000', 'context: 940000'):
        pass
    check('compaction: pending (' not in status_line(h), 'the old wording must never appear')


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
    decisions, under 40,000 bytes, for Claude and Codex, with message_bytes on the turn record."""
    h, d = home(), fakes(context=100000)
    state = big_state()
    Path(h, 'state.json').write_text(json.dumps(state, indent=1))
    raw = len(Path(h, 'state.json').read_bytes())
    check(raw > 120000, f'fixture state must be real-sized (>120 KB), got {raw}')
    Path(h, 'work-status.json').write_text(json.dumps({'turn': 0, 'mode': 'done', 'track': 't21'}))
    sup = make(h, d)
    event(h, 'what is new?', thread='1791000005.000001')                       # (a) thread of t05
    event(h, '<@U0FAKE> assignee: Hermes | track: t11\nrun the thing', thread='9.9')  # (b) assignee line
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
    # the cap: four touched tracks of ~15 KB each cannot fit; the last-selected (work-status) goes first
    state2 = big_state(entry_kb=15)
    Path(h, 'state.json').write_text(json.dumps(state2, indent=1))
    event(h, 'again', thread='1791000005.000001')
    event(h, '<@U0FAKE> assignee: Hermes | track: t11\nrun', thread='9.9')
    event(h, 'continue t17: carry on', thread='8.8')
    sup.run_once()
    stdin = calls(d)[-1]['stdin']
    size = len(stdin.encode('utf-8'))
    check(size <= 40000, f'the cap must hold under oversized selections, got {size}')
    check('(truncated' in stdin, 'the header must say the digest was truncated')
    check('MARKER-t05-DEEP' in stdin, 'the first-selected touched track must survive truncation')
    check('MARKER-t21-DEEP' not in stdin, 'the last-selected touched track must be dropped first')
    for t in state2['tracks']:
        check(f"- {t['id']}: now:" in stdin, f"track lines are never dropped: {t['id']}")
    # Codex gets the same digest and no second state section
    Path(h, 'engine').write_text('codex\n')
    event(h, 'codex turn', thread='1791000005.000001')
    sup.run_once()
    c = calls(d)[-1]
    check(c['kind'] == 'codex', f'expected a codex call, got {c["kind"]}')
    check(c['stdin'].count('# factory/state.json') == 1 and '(digest' in c['stdin'], 'codex must get exactly the digest')
    check('MARKER-t05-DEEP' in c['stdin'], 'codex digest must include the touched track')
    check(len(c['stdin'].encode('utf-8')) <= 40000 + 20000, 'codex message must stay bounded (digest + preamble + handoff)')


SCENARIOS = [threshold_compacts_before_next_turn, over_ratio_boundaries, quiet_hours_do_not_gate,
             held_is_loud_and_keeps_the_schedule, failed_rollover_backs_off, cli_compactions_detected,
             status_line_contract, state_digest_bounds_the_message]


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
