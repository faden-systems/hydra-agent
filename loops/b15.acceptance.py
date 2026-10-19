#!/usr/bin/env python3
"""Exit-owned offline b15 contracts (loops/b15.md): rollover verification by the new session's first request, the
reply-shape failures of the `[compaction]` turn, and turn timeouts told apart from account faults. A fake Claude CLI
that appends `assistant` records with real-shaped usage to its transcript as it works and answers with stream-json,
table-driven behaviours, a controlled clock, a short engine timeout, fake posters; no real engine, GitHub or Slack.
Every check raises (no `assert`). `--only <name>` runs one scenario."""
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'manager'))
import supervisor as S  # noqa: E402

FABLE, ASTRA, OPUS = 'claude-fable-5-1', 'gpt-6-astra', 'claude-opus-5-5'
LADDER_PAIRS = {('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA), ('claude-r2d2', OPUS), ('claude-l', OPUS)}
BELOW = ('claude-sonnet-5', 'claude-haiku-4-5-20251001', 'claude-opus-5', 'gpt-5.6-sol', 'gpt-6.1-sol')
OLD_ID, NEW_ID = '52164f18-f7ee-42cc-a694-f50832d61cf0', 'a0023f84-0449-4fd4-974b-f286c67c1cd0'  # the 2026-10-18 14:54Z rollover
THRESHOLD = 300000
FIRST_REQUEST = {'input_tokens': 4, 'cache_creation_input_tokens': 15407, 'cache_read_input_tokens': 14201}  # 29,612: the new session's real first request
FIRST_TOTAL = 29612
# the sanitized rows of 2026-10-18 (loops/b15.md appendix): the replays are driven from them
ROWS_TURNS = [
    {'n': 2134, 'at': '2026-10-18T14:51:44Z', 'engine': 'claude-r2d2', 'context_tokens': 305024, 'duration_s': 142.3},
    {'n': 2135, 'at': '2026-10-18T14:54:57Z', 'engine': 'claude-r2d2', 'context_tokens': None, 'duration_s': 0.0, 'kind': 'compaction'},
    {'n': 2136, 'at': '2026-10-18T14:54:57Z', 'engine': 'claude-r2d2', 'context_tokens': None, 'duration_s': 1800.9},
    {'n': 2137, 'at': '2026-10-18T15:29:58Z', 'engine': 'claude-r2d2', 'context_tokens': 343387, 'duration_s': 218.4},
]
ROWS_TIMEOUTS = [
    {'at': '2026-10-18T15:24:58Z', 'engine': 'claude-r2d2', 'model': FABLE, 'rc': 124, 'classification': 'other', 'reason': 'timeout after 1800s', 'success': False},
    {'at': '2026-10-18T16:03:46Z', 'engine': 'claude-r2d2', 'model': FABLE, 'rc': 124, 'classification': 'other', 'reason': 'timeout after 1800s', 'success': False},
    {'at': '2026-10-18T16:38:49Z', 'engine': 'claude-r2d2', 'model': FABLE, 'rc': 124, 'classification': 'other', 'reason': 'timeout after 1800s', 'success': False},
    {'at': '2026-10-18T17:13:55Z', 'engine': 'claude-r2d2', 'model': FABLE, 'rc': 124, 'classification': 'other', 'reason': 'timeout after 1800s', 'success': False},
]
HANDOFF_NAMES = ('tracks:', 'waiting on:', 'last decision:', 'next action:', 'open question:')
HANDOFF_MISS = 'carried no valid five-line handoff; end it with'
MEMORY_MISS = 'left MEMORY.md unchanged; write the durable facts'
MIN = dt.timedelta(minutes=1)
HOME_FAKES = {}


class Fail(Exception):
    pass


class SimulatedCrash(BaseException):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def home(engine=None, sid=OLD_ID, config=None):
    """A running home: a started session whose transcript exists (the ordinary state before a rollover)."""
    h = tempfile.mkdtemp(prefix='b15-home-')
    for d in ('inbox', 'inbox/files', 'logs', 'credentials', '.claude', 'mirror'):
        os.makedirs(os.path.join(h, d), exist_ok=True)
    for name in ('claude-r2d2', 'claude-l'):
        token = f'fake-{name}'
        Path(h, 'credentials', f'{name}.env').write_text(f'CLAUDE_CODE_OAUTH_TOKEN={token}\n')
        Path(h, 'credentials', f'{name}.env.identity.json').write_text(json.dumps(
            {'schema_version': 1, 'label': name, 'account_id': f'fixture-{name}',
             'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'verified_at': '2026-10-04T20:00:00Z',
             'method': 'private-window-and-usage-bar', 'evidence': 'https://example.test/evidence'}))
    Path(h, 'engine').write_text(json.dumps(engine or {'acc': 'claude-r2d2', 'model': FABLE}) + '\n')
    Path(h, 'session-id').write_text(sid + '\n')
    Path(h, 'rollover-intent.json').write_text(json.dumps({'old_id': 'none', 'new_id': sid, 'started': True}) + '\n')
    tp = transcript_path(h, sid)
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps({'type': 'user', 'sessionId': sid, 'cwd': h}) + '\n'
                  + json.dumps(assistant_record(sid, {'input_tokens': 10, 'cache_creation_input_tokens': 100, 'cache_read_input_tokens': 1000})) + '\n')
    cfg = {'dev_channel': 'C_DEV'}
    cfg.update(config or {})
    Path(h, 'config.json').write_text(json.dumps(cfg))
    return h


def assistant_record(sid, usage, text='working'):
    return {'type': 'assistant', 'sessionId': sid, 'message': {'role': 'assistant', 'usage': dict(usage, output_tokens=5),
                                                                'content': [{'type': 'text', 'text': text}]}}


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import sys, os, json, time, hashlib
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]
acc = {'fake-claude-r2d2': 'claude-r2d2', 'fake-claude-l': 'claude-l'}.get(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''), '?')
model = argv[argv.index('--model') + 1] if '--model' in argv else ''
resume = '--resume' in argv
sid = argv[argv.index('--resume') + 1] if resume else (argv[argv.index('--session-id') + 1] if '--session-id' in argv else '')
table = json.loads((D / 'table.json').read_text()) if (D / 'table.json').exists() else {}
cfg = Path(os.environ.get('CLAUDE_CONFIG_DIR') or (Path.home() / '.claude'))
project = cfg / 'projects' / os.getcwd().replace('/', '-')   # where Claude Code keeps this cwd's sessions
transcript = project / (sid + '.jsonl')
existed = transcript.exists()
HANDOFF = '---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none'
def entry(msg):
    """The behaviour of this call: `compact` for a [compaction] message, else the next queued entry, else the
    engine's default. An entry is a name or {behaviour, ctx, records}."""
    if msg.startswith('[compaction]'):
        e = table.get('compact', 'ok')
    else:
        q = json.loads((D / 'queue.json').read_text()) if (D / 'queue.json').exists() else []
        if q:
            e = q.pop(0)
            (D / 'queue.json').write_text(json.dumps(q))
        else:
            e = table.get(acc + '|' + model, 'ok')
    return e if isinstance(e, dict) else {'behaviour': e}
def record(msg, e, persistent=False):
    with open(D / 'calls.jsonl', 'a') as f:
        f.write(json.dumps({'acc': acc, 'model': model, 'argv': argv, 'resume': resume, 'sid': sid, 'transcript_existed': existed,
                            'behaviour': e.get('behaviour'), 'stdin': msg, 'stdin_sha': hashlib.sha256(msg.encode()).hexdigest(),
                            'persistent_turn': persistent}) + '\n')
def usage_of(e):
    ctx = int(e.get('ctx', 50000))
    return {'input_tokens': 4, 'cache_creation_input_tokens': ctx - 4 - 1000, 'cache_read_input_tokens': 1000}
def append(u, text='working'):
    project.mkdir(parents=True, exist_ok=True)
    if not transcript.exists():
        with open(transcript, 'a') as f:
            f.write(json.dumps({'type': 'user', 'sessionId': sid, 'cwd': os.getcwd()}) + '\n')
    with open(transcript, 'a') as f:
        f.write(json.dumps({'type': 'assistant', 'sessionId': sid, 'message': {'role': 'assistant', 'usage': dict(u, output_tokens=5),
                                                                                'content': [{'type': 'text', 'text': text}]}}) + '\n')
ERRORS = {'credits': "You're out of usage credits. Switch to another model to continue.", 'other': 'a plain runtime crash'}
def serve(msg, e):
    """Returns the (reply, usage) to answer with, or exits for the failing behaviours."""
    b = e.get('behaviour', 'ok')
    u = usage_of(e)
    if msg.startswith('[compaction]'):
        m = Path(os.environ['HYDRA_MEMORY_DIR']) / 'MEMORY.md'
        if b == 'fail':
            print('compaction engine failure', file=sys.stderr); sys.exit(1)
        if b in ('ok', 'no_handoff'):
            m.write_text(m.read_text() + '\n- note\n')
        append(u, 'compacted')
        return ('compacted' if b == 'no_handoff' else 'compacted\n' + HANDOFF), u
    if b in ERRORS:
        print(ERRORS[b], file=sys.stderr); sys.exit(1)
    if b == 'timeout_working':
        for i in range(int(e.get('records', 3))):
            append(u, 'working %%d' %% i)
        time.sleep(30)                                   # past the harness's engine timeout: rc 124 while working
    if b == 'timeout_silent':
        time.sleep(30)                                   # rc 124 with nothing written: the engine hung
    if b == 'crash':
        sys.exit(17)
    append(u, 'REPLY: ok')
    return 'REPLY: ok\n' + HANDOFF, u
if '--input-format' not in argv:
    msg = sys.stdin.read() if not sys.stdin.isatty() else ''
    e = entry(msg)
    record(msg, e)
    reply, u = serve(msg, e)
    print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'usage': dict(u, output_tokens=5), 'content': [{'type': 'text', 'text': reply}]}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': reply, 'session_id': sid, 'usage': dict(u, output_tokens=5)}), flush=True)
    sys.exit(0)
e0 = entry('<persistent start>')
record('<persistent start>', e0)
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)   # the persistent child
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    env = json.loads(line)
    content = env.get('message', {}).get('content')
    text = content if isinstance(content, str) else ''.join(c.get('text', '') for c in content or [])
    e = entry(text)
    record(text, e, persistent=True)
    reply, u = serve(text, e)
    print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'usage': dict(u, output_tokens=5), 'content': [{'type': 'text', 'text': reply}]}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': reply, 'session_id': sid, 'usage': dict(u, output_tokens=5)}), flush=True)
'''

FAKE_CODEX = r'''#!/usr/bin/env python3
import sys, json
from pathlib import Path
D = Path(%(dir)r)
msg = sys.stdin.read() if not sys.stdin.isatty() else ''
argv = sys.argv[1:]
model = argv[argv.index('-m') + 1] if '-m' in argv else ''
with open(D / 'calls.jsonl', 'a') as f:
    f.write(json.dumps({'acc': 'codex', 'model': model, 'argv': argv, 'resume': False, 'sid': '', 'behaviour': 'ok', 'stdin': msg[:200]}) + '\n')
out = argv[argv.index('-o') + 1]
Path(out).write_text('REPLY: codex ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none\n')
print('codex done')
'''


def fakes(table=None):
    d = Path(tempfile.mkdtemp(prefix='b15-engine-'))
    (d / 'table.json').write_text(json.dumps(table or {}))
    for name, src in (('claude', FAKE_CLAUDE), ('codex', FAKE_CODEX)):
        p = d / name
        p.write_text(src % {'dir': str(d)})
        p.chmod(0o755)
    return d


def set_table(d, table):
    (d / 'table.json').write_text(json.dumps(table))


def queue(d, *entries):
    """The behaviours of the next non-compaction calls, in order."""
    (d / 'queue.json').write_text(json.dumps(list(entries)))


def engines(d):
    return {'claude-r2d2': {'bin': str(d / 'claude'), 'cred': 'claude-r2d2.env'},
            'claude-l': {'bin': str(d / 'claude'), 'cred': 'claude-l.env'},
            'codex': {'bin': str(d / 'codex'), 'cred': None}}


def calls(d):
    p = d / 'calls.jsonl'
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def invoked(d):
    return [(c['acc'], c['model']) for c in calls(d) if not c.get('persistent_turn')]


def compaction_calls(d):
    return [c for c in calls(d) if c['stdin'].startswith('[compaction]')]


def transcript_path(h, sid):
    return Path(h, '.claude', 'projects', os.path.abspath(h).replace('/', '-'), f'{sid}.jsonl')


class Poster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))


class Clock:
    def __init__(self, start, frozen=False):
        self.now = start
        self.frozen = frozen

    def __call__(self):
        if not self.frozen:
            self.now = self.now + dt.timedelta(seconds=1)
        return self.now


T0 = dt.datetime(2026, 10, 18, 14, 50, 0, tzinfo=dt.timezone.utc)


def make(h, d, poster=None, buildlog=None, frozen=False):
    HOME_FAKES[h] = d
    return S.Supervisor(home=h, engines=engines(d), poster=poster or Poster(), buildlog_poster=buildlog or Poster(),
                        clock=Clock(T0, frozen=frozen), engine_timeout=3)


def event(h, text, thread='1.0', ts=None, instructs=True):
    ts = ts or f'{time.time():.6f}'
    ev = S.new_event('slack', {'channel': 'C_DEV', 'thread_ts': thread, 'ts': ts, 'text': text, 'instructs': instructs})
    S.append_event(h, ev)
    return ev['id']


def tick(sup, h, at_minutes=None, at_seconds=None):
    if at_seconds is not None:
        sup.clock.now = T0 + dt.timedelta(seconds=at_seconds)
    elif at_minutes is not None:
        sup.clock.now = T0 + at_minutes * MIN
    retry = Path(h, 'logs', 'retry-after')
    if retry.exists():
        retry.unlink()


def turn(sup, h, at_minutes=None, text='go', at_seconds=None):
    """One supervisor turn at the controlled time; the back-off file is removed first as the scheduler would wait it out."""
    tick(sup, h, at_minutes, at_seconds)
    eid = event(h, text)
    check(sup.run_once() is True, f'the turn at {at_minutes} min did not run')
    return eid


def attempts(h):
    return [a for a in S.read_jsonl(os.path.join(h, 'logs', 'attempts.jsonl')) if isinstance(a, dict)]


def turns(h):
    return [t for t in S.read_jsonl(os.path.join(h, 'logs', 'turns.jsonl')) if isinstance(t, dict)]


def engine_file(h):
    return json.loads(Path(h, 'engine').read_text())


def session_id(h):
    return Path(h, 'session-id').read_text().strip()


def fault(h):
    p = Path(h, 'logs', 'fault.json')
    return json.loads(p.read_text()) if p.exists() else {}


def fault_bytes(h):
    p = Path(h, 'logs', 'fault.json')
    return p.read_bytes() if p.exists() else None


def cstate(h):
    return S.read_compaction_state(h)


def notices(h, needle):
    return [it for it in S.read_outbox(h) if needle in (it.get('text') or '')]


def status_lines(h, needle):
    return [l for l in S.status_text(h).splitlines() if l.startswith(needle)]


def compaction_line(h):
    lines = status_lines(h, 'context:')
    check(len(lines) == 1, f'one compaction status line: {lines}')
    return lines[0]


def memory_path(sup):
    return Path(sup.memory_dir, 'MEMORY.md')


def handoff_path(sup):
    return Path(sup.memory_dir, 'MANAGER-HANDOFF.md')


def no_downgrade(h, pairs=LADDER_PAIRS):
    for a in attempts(h):
        check((a.get('engine'), a.get('model')) in pairs, f'attempts.jsonl carries a pair outside the ladder: {a}')
        check(a.get('model') not in BELOW, f'a model below the floor was invoked: {a}')
    d = HOME_FAKES.get(h)
    if d is not None:
        bad = [(c.get('acc'), c.get('model')) for c in calls(d) if (c.get('acc'), c.get('model')) not in pairs]
        check(not bad, f'an engine process was started outside the ladder pairs: {bad}')


def rollover(sup, h, d, at_minutes=0, ctx=305024):
    """A tokens compaction the way 2026-10-18 14:54Z happened: a measured turn over the threshold (305,024), then the
    next tick runs the `[compaction]` turn on the old id and replaces the session id. Returns the new id."""
    old = session_id(h)
    queue(d, {'behaviour': 'ok', 'ctx': ctx})
    turn(sup, h, at_minutes)
    check((cstate(h).get('pending') or {}).get('reason') == 'tokens', f'a measured turn over the threshold schedules the compaction: {cstate(h)}')
    return old


def timeout_row(h):
    rows = [a for a in attempts(h) if a.get('rc') == 124]
    check(rows, 'no rc 124 attempt row')
    return rows[-1]


# ---------------------------------------------------------------------------------------------- scenarios

def incident_2026_10_18_rollover_verified_from_the_transcript():
    """(a) The 14:54Z replay: the compaction rolls over; the first turn on the new id is a working timeout; at the
    next turn the verification settles from the new transcript's first assistant usage (29,612), no back-off, no
    failure line; that turn's own measurement of 343,387 schedules an ordinary tokens compaction."""
    h, d = home(), fakes()
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    old = rollover(sup, h, d, 0)
    queue(d, {'behaviour': 'timeout_working', 'ctx': FIRST_TOTAL, 'records': 2})
    tick(sup, h, 5)
    eid = event(h, 'first turn on the new id')
    check(sup.run_once() is True, 'the compaction tick ran')
    new = session_id(h)
    check(new != old and len(compaction_calls(d)) == 1, f'the compaction rolled the session over: {old} -> {new}')
    check(cstate(h).get('verify') and cstate(h)['verify'].get('new_id') == new, f'the verification is outstanding on the new id: {cstate(h)}')
    row = timeout_row(h)
    check((row['classification'], row.get('timeout'), row.get('transcript_records')) == ('timeout', 'working', 2)
          and row['reason'].startswith('timeout after ') and row['engine'] == 'claude-r2d2',
          f'the timed-out first turn is a working timeout (the real row {ROWS_TIMEOUTS[0]} said other): {row}')
    check(eid in S.handled_ids(h), 'b8 unchanged: the timed-out request keeps its durable disposition')
    check(not Path(h, 'logs', 'fault.json').exists(), f'a working timeout opens no fault episode: {fault(h)}')
    check(cstate(h).get('verify') is not None and cstate(h).get('failed_at') is None, 'nothing measured yet: still pending, no back-off')
    tp = transcript_path(h, new)
    first = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
    first = [r for r in first if r.get('type') == 'assistant'][0]
    check(S.Supervisor._context_tokens_from(first['message']['usage']) == FIRST_TOTAL, 'the fake wrote the real first request to the new transcript')
    queue(d, {'behaviour': 'ok', 'ctx': 343387})
    eid2 = turn(sup, h, 40, text='the first measured turn')
    check(eid2 in S.handled_ids(h), 'served')
    last = cstate(h).get('last') or {}
    check(cstate(h).get('verify') is None and last.get('ok') is True and last.get('after_tokens') == FIRST_TOTAL
          and last.get('source') == 'transcript' and last.get('new_id') == new and last.get('before_tokens') == 305024,
          f'verified from the transcript, not from the 343,387 turn: {last}')
    check(cstate(h).get('failed_at') is None and not any('rollover failed' in t for _, _, t in buildlog.posted),
          f'no back-off, no failure line: {cstate(h)} {buildlog.posted}')
    pend = cstate(h).get('pending') or {}
    check(pend.get('reason') == 'tokens' and pend.get('value') == 343387, f'the large measurement schedules the next compaction instead: {pend}')
    check(sup.compaction_due() == (True, 'tokens'), 'and that compaction is due (no back-off, no pending verification)')
    check(f'session start: {FIRST_TOTAL} tokens' in compaction_line(h), f'status shows the session-start size: {compaction_line(h)}')
    no_downgrade(h)
    return h, d, sup


def first_request_over_the_threshold_fails_the_verification():
    """(b) A new session whose first request is already over the threshold: `ok: false` from the transcript, the
    back-off and the one buildlog line as today."""
    h, d = home(), fakes()
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    old = rollover(sup, h, d, 0)
    queue(d, {'behaviour': 'ok', 'ctx': 320000})
    turn(sup, h, 5)
    new = session_id(h)
    last = cstate(h).get('last') or {}
    check(new != old and last.get('ok') is False and last.get('after_tokens') == 320000 and last.get('source') == 'transcript'
          and 'still 320000' in (last.get('error') or ''), f'a genuine failure is still a failure: {last}')
    check(cstate(h).get('failed_at') and sum('rollover failed' in t for _, _, t in buildlog.posted) == 1, f'back-off and one line: {cstate(h)} {buildlog.posted}')
    check(sup.compaction_due() == (False, None), 'the back-off holds')
    no_downgrade(h)


def verification_stays_pending_until_the_transcript_has_a_record():
    """(c) No assistant record yet: pending across a tick without one; settled from the file as soon as one appears,
    at `compaction_due` (no turn needed)."""
    h, d = home(), fakes()
    sup = make(h, d)
    old = rollover(sup, h, d, 0)
    tick(sup, h, 2)
    check(sup.run_once() is True, 'the compaction tick ran without events')
    new = session_id(h)
    check(new != old and cstate(h).get('verify', {}).get('new_id') == new, 'rolled over, verification outstanding')
    check(sup.compaction_due() == (False, None) and cstate(h).get('verify') is not None and (cstate(h).get('last') or {}).get('ok') is None,
          f'no record, no verdict: {cstate(h)}')
    tp = transcript_path(h, new)
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps({'type': 'user', 'sessionId': new}) + '\n')
    check(sup.compaction_due() == (False, None) and cstate(h).get('verify') is not None, 'a transcript without an assistant record settles nothing')
    with open(tp, 'a') as f:
        f.write(json.dumps(assistant_record(new, FIRST_REQUEST)) + '\n')
        f.write(json.dumps(assistant_record(new, {'input_tokens': 4, 'cache_creation_input_tokens': 400000, 'cache_read_input_tokens': 0})) + '\n')
    sup.compaction_due()
    last = cstate(h).get('last') or {}
    check(cstate(h).get('verify') is None and last.get('ok') is True and last.get('after_tokens') == FIRST_TOTAL and last.get('source') == 'transcript',
          f'the first record decides, later records do not: {last}')
    rec = sup.verify_compaction(10)
    check(rec is None, 'nothing left to verify')
    no_downgrade(h)


def explicit_argument_still_settles_a_verification():
    """(c2) The baseline contract: an explicit measurement passed by a caller settles the verification with
    `source: "argument"`; the turn path never passes one."""
    h, d = home(), fakes()
    sup = make(h, d)
    state = cstate(h)
    state['verify'] = {'before_tokens': 500000, 'at': 'fixture', 'reason': 'tokens', 'old_id': 'a', 'new_id': 'b'}
    sup.save_compaction_state(state)
    rec = sup.verify_compaction()
    check(rec['ok'] is None and cstate(h).get('verify') is not None, 'no transcript for b: pending')
    rec = sup.verify_compaction(10)
    check(rec['ok'] is True and rec.get('source') == 'argument' and cstate(h).get('verify') is None, f'an explicit argument settles: {rec}')


def incident_17_37_no_handoff_is_retried_once_without_backoff():
    """(d) The 17:37Z replay: a forced compaction answered `compacted` alone (MEMORY.md written) keeps the old id
    with the five-line error, no back-off, `shape_failures: 1`, COMPACT kept, no buildlog line; the next tick
    repeats the compaction with the handoff sentence, the reply now carries the block, the rollover happens."""
    h, d = home(), fakes({'compact': 'no_handoff'})
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    sup.ensure_memory_layout()
    ho_before = handoff_path(sup).read_bytes() if handoff_path(sup).exists() else None
    Path(h, 'COMPACT').write_text('founder\n')
    old = session_id(h)
    tick(sup, h, 0)
    check(sup.run_once() is True, 'the forced compaction ran')
    last = cstate(h).get('last') or {}
    check(session_id(h) == old and last.get('ok') is False and 'five-line' in (last.get('error') or ''), f'the old id is kept with the baseline error text: {last}')
    check(cstate(h).get('failed_at') is None and cstate(h).get('shape_failures') == 1 and last.get('shape_failures') == 1,
          f'a shape failure arms no back-off: {cstate(h)}')
    check(Path(h, 'COMPACT').exists(), 'the forced file is kept for the retry')
    check(not buildlog.posted and not notices(h, 'rollover failed'), f'no failure line for a shape miss: {buildlog.posted}')
    check((handoff_path(sup).read_bytes() if handoff_path(sup).exists() else None) == ho_before, 'the handoff file is untouched')
    check(sup.compaction_due() == (True, 'forced'), 'the retry is due at the next tick')
    set_table(d, {'compact': 'ok'})
    tick(sup, h, 1)
    check(sup.run_once() is True, 'the retry ran')
    cc = compaction_calls(d)
    check(len(cc) == 2 and HANDOFF_MISS in cc[1]['stdin'] and HANDOFF_MISS not in cc[0]['stdin'] and cc[1]['stdin'].startswith(S.COMPACTION_MESSAGE),
          f'the retry names the miss after the unchanged message: {[c["stdin"][-160:] for c in cc]}')
    check(session_id(h) != old and not Path(h, 'COMPACT').exists() and not cstate(h).get('shape_failures'), f'rolled over, streak reset: {cstate(h)}')
    check(cstate(h).get('failed_at') is None and not buildlog.posted, 'still no back-off')
    no_downgrade(h)


def unchanged_memory_backs_off_on_the_second_miss_only():
    """(e) MEMORY.md unchanged twice in a row: the first miss is retried with the MEMORY.md sentence and no back-off;
    the second arms `failed_at`, posts the one buildlog line and consumes COMPACT. A changed reply after a first
    miss resets the streak. An engine failure (rc 1) backs off at once as today."""
    h, d = home(), fakes({'compact': 'unchanged'})
    buildlog = Poster()
    sup = make(h, d, buildlog=buildlog)
    old = session_id(h)
    Path(h, 'COMPACT').write_text('founder\n')
    tick(sup, h, 0)
    sup.run_once()
    last = cstate(h).get('last') or {}
    check(session_id(h) == old and 'did not change MEMORY.md' in (last.get('error') or '') and cstate(h).get('failed_at') is None
          and cstate(h).get('shape_failures') == 1 and Path(h, 'COMPACT').exists() and not buildlog.posted, f'first miss: {cstate(h)} {buildlog.posted}')
    tick(sup, h, 1)
    sup.run_once()
    cc = compaction_calls(d)
    check(len(cc) == 2 and MEMORY_MISS in cc[1]['stdin'] and HANDOFF_MISS not in cc[1]['stdin'], f'the retry names the MEMORY.md miss: {cc[1]["stdin"][-160:]}')
    check(session_id(h) == old and cstate(h).get('failed_at') and not Path(h, 'COMPACT').exists()
          and sum('rollover failed' in t for _, _, t in buildlog.posted) == 1, f'second miss: back-off, one line, COMPACT consumed: {cstate(h)} {buildlog.posted}')
    check(sup.compaction_due() == (False, None), 'the back-off holds')
    # a changed reply after a first miss resets the streak
    h2, d2 = home(), fakes({'compact': 'unchanged'})
    sup2 = make(h2, d2)
    Path(h2, 'COMPACT').write_text('x\n')
    tick(sup2, h2, 0)
    sup2.run_once()
    check(cstate(h2).get('shape_failures') == 1, 'one miss')
    set_table(d2, {'compact': 'ok'})
    tick(sup2, h2, 1)
    sup2.run_once()
    check(session_id(h2) != OLD_ID and not cstate(h2).get('shape_failures') and cstate(h2).get('failed_at') is None, f'reset: {cstate(h2)}')
    # an engine failure backs off at once
    h3, d3 = home(), fakes({'compact': 'fail'})
    bl3 = Poster()
    sup3 = make(h3, d3, buildlog=bl3)
    Path(h3, 'COMPACT').write_text('x\n')
    tick(sup3, h3, 0)
    sup3.run_once()
    check(session_id(h3) == OLD_ID and cstate(h3).get('failed_at') and not Path(h3, 'COMPACT').exists() and len(bl3.posted) == 1
          and not cstate(h3).get('shape_failures'), f'rc 1 is not a shape failure: {cstate(h3)} {bl3.posted}')
    for hh in (h, h2, h3):
        no_downgrade(hh)


def working_timeout_is_not_a_fault():
    """(f) A turn that wrote three replies and ran out of time: classification timeout, working, 3 records; no
    fault episode; one `manager turn timeout:` line with a durable id; the events answered as unavailable; the next
    turn on the same engine."""
    h, d = home(), fakes({f'claude-r2d2|{FABLE}': {'behaviour': 'timeout_working', 'records': 3}})
    poster = Poster()
    sup = make(h, d, poster=poster)
    eid = turn(sup, h, 0)
    row = timeout_row(h)
    check((row['rc'], row['classification'], row.get('timeout'), row.get('transcript_records'), row['success']) == (124, 'timeout', 'working', 3, False),
          f'the working timeout row: {row}')
    check(eid in S.handled_ids(h), 'uncertain disposition (b8): handled, never replayed')
    check(not Path(h, 'logs', 'fault.json').exists() and not notices(h, 'engine fault'), f'no fault episode, no fault line: {fault(h)}')
    n = turns(h)[-1]['n'] if turns(h) else None
    line = notices(h, 'manager turn timeout: ')
    check(len(line) == 1 and line[0].get('thread_ts') is None and line[0].get('channel') == 'C_DEV'
          and line[0]['text'].startswith(f'manager turn timeout: turn {n} on claude-r2d2 ({FABLE}) ran ')
          and ' s while working (3 replies); its events were answered as unavailable' in line[0]['text']
          and line[0].get('id') == f'timeout-{n}', f'one line with the durable id: {line} (turn {n})')
    check(turns(h) and turns(h)[-1].get('timeout') == 'working', f'the turn row records the kind before the line is queued: {turns(h)[-1]}')
    check(engine_file(h) == {'acc': 'claude-r2d2', 'model': FABLE} and not S.read_engine_runtime(h).get('active'), 'nothing moved')
    set_table(d, {})
    eid2 = turn(sup, h, 5)
    check(eid2 in S.handled_ids(h) and invoked(d)[-1] == ('claude-r2d2', FABLE), f'the same engine serves next: {invoked(d)}')
    check(len(notices(h, 'manager turn timeout: ')) == 1, 'no second line')
    check(f'last turn timeout: ' in S.status_text(h) and 'working (3 replies)' in status_lines(h, 'last turn timeout: ')[0], f'status: {status_lines(h, "last turn timeout: ")}')
    sup.clock.now = T0 + dt.timedelta(hours=25)
    check(not status_lines(h, 'last turn timeout: '), 'the status line is gone after 24 h')
    no_downgrade(h)
    return h, d, sup


def silent_timeout_is_a_fault_under_b14():
    """(g) A hang with nothing written is a fault as today: episode count 1, text ending `silent timeout`; three
    silent timeouts over thirty minutes escalate under b14 and the next turn moves."""
    h, d = home(), fakes({f'claude-r2d2|{FABLE}': 'timeout_silent'})
    sup = make(h, d)
    for m in (0, 5, 10):
        turn(sup, h, m)
    row = timeout_row(h)
    check((row['classification'], row.get('timeout'), row.get('transcript_records')) == ('timeout', 'silent', 0), f'the silent row: {row}')
    f = fault(h)
    check(f.get('count') == 3 and f.get('text', '').endswith('silent timeout') and not f.get('escalated'), f'three counted faults, silent wording: {f}')
    check(len(notices(h, 'engine fault: ')) == 1 and notices(h, 'engine fault: ')[0]['text'].endswith('silent timeout') and not notices(h, 'manager turn timeout: '),
          f'one fault line, no timeout line: {notices(h, "engine fault: ")}')
    turn(sup, h, 35)
    check(fault(h).get('escalated') is True and fault(h).get('count') == 4, f'the valve opened on the fourth silent timeout: {fault(h)}')
    check(len(notices(h, 'engine fault escalated: ')) == 1, 'one escalated line')
    set_table(d, {f'claude-r2d2|{FABLE}': 'timeout_silent'})
    eid = turn(sup, h, 36)
    check(eid in S.handled_ids(h) and invoked(d)[-1] == ('claude-l', FABLE) and engine_file(h)['acc'] == 'claude-l', f'the next turn moves (b14 p): {invoked(d)[-2:]}')
    no_downgrade(h)


def four_working_timeouts_never_escalate():
    """(h) The 15:24Z-17:13Z replay: four working timeouts over two hours, no fault file, no escalation, no move, four
    timeout lines with distinct ids."""
    h, d = home(), fakes({f'claude-r2d2|{FABLE}': {'behaviour': 'timeout_working', 'records': 1}})
    sup = make(h, d)
    for i, m in enumerate((0, 35, 70, 105)):
        turn(sup, h, m)
        check(not Path(h, 'logs', 'fault.json').exists(), f'no fault file after timeout {i + 1}')
    rows = [a for a in attempts(h) if a.get('rc') == 124]
    check(len(rows) == len(ROWS_TIMEOUTS) and all(r['classification'] == 'timeout' and r.get('timeout') == 'working' for r in rows), f'four working rows: {rows}')
    check(engine_file(h) == {'acc': 'claude-r2d2', 'model': FABLE} and not S.read_engine_runtime(h).get('active'), 'nothing moved')
    check(not notices(h, 'engine fault') and not notices(h, 'fallback'), 'no fault, no fallback lines')
    lines = notices(h, 'manager turn timeout: ')
    check(len(lines) == 4 and len({l.get('id') for l in lines}) == 4, f'four lines, four ids: {[l.get("id") for l in lines]}')
    check(all(invoked(d)[i] == ('claude-r2d2', FABLE) for i in range(4)), f'only the configured engine ran: {invoked(d)}')
    no_downgrade(h)


def status_shows_the_session_start_and_the_last_timeout():
    """(i) The compaction line carries the current session's first request; the timeout line appears after a
    timeout and is absent for a home without one."""
    h, d = home(), fakes()
    sup = make(h, d)
    check(f'session start: 1110 tokens' in compaction_line(h), f'the running session (fixture transcript) shows its first request: {compaction_line(h)}')
    check(not status_lines(h, 'last turn timeout: '), 'no timeout, no line')
    h2, d2 = home(sid='00000000-0000-4000-8000-000000000000'), fakes()
    transcript_path(h2, '00000000-0000-4000-8000-000000000000').unlink()
    sup2 = make(h2, d2)
    check('session start: unknown' in compaction_line(h2), f'no transcript: unknown: {compaction_line(h2)}')
    h3, d3, sup3 = working_timeout_is_not_a_fault()
    text = S.status_text(h3).splitlines()
    ctx = [i for i, l in enumerate(text) if l.startswith('context:')]
    sup3.clock.now = T0 + 10 * MIN
    text = S.status_text(h3).splitlines()
    ctx = [i for i, l in enumerate(text) if l.startswith('context:')]
    check(ctx and text[ctx[0] + 1].startswith('last turn timeout: ') and 'working (3 replies)' in text[ctx[0] + 1], f'the timeout line sits right after the compaction line: {text}')
    _ = sup, sup2


def persistent_mode_classifies_a_working_timeout_the_same():
    """(j) In persistent mode a child that wrote replies and then hung is a working timeout: the same row, no fault,
    one line; the child is retired as today."""
    h, d = home(engine={'acc': 'claude-r2d2', 'model': FABLE, 'mode': 'persistent'}), fakes()
    queue(d, 'ok', {'behaviour': 'timeout_working', 'records': 2})
    sup = make(h, d)
    try:
        eid = turn(sup, h, 0)
        row = timeout_row(h)
        check((row['classification'], row.get('timeout'), row.get('transcript_records')) == ('timeout', 'working', 2), f'the persistent working row: {row}')
        check(eid in S.handled_ids(h) and not Path(h, 'logs', 'fault.json').exists() and len(notices(h, 'manager turn timeout: ')) == 1, 'disposed, no fault, one line')
        check(not sup.persistent_status().get('pid'), f'the child was retired: {sup.persistent_status()}')
    finally:
        sup._retire_persistent('planned', 'harness end')
    no_downgrade(h)


def compaction_message_states_the_whole_contract():
    """(k) The `[compaction]` stdin received by the engine names MEMORY.md, `compacted`, the `---HANDOFF---` block and
    its five line names."""
    h, d = home(), fakes()
    sup = make(h, d)
    Path(h, 'COMPACT').write_text('x\n')
    tick(sup, h, 0)
    sup.run_once()
    cc = compaction_calls(d)
    check(len(cc) == 1, 'one compaction call')
    msg = cc[0]['stdin']
    for needle in ('[compaction]', 'MEMORY.md', 'compacted', '---HANDOFF---') + HANDOFF_NAMES:
        check(needle in msg, f'the compaction message lacks {needle!r}: {msg}')
    check(msg.startswith(S.COMPACTION_MESSAGE) and all(n in S.COMPACTION_MESSAGE for n in ('---HANDOFF---', 'compacted', 'MEMORY.md')), 'the constant carries the contract')
    check(session_id(h) != OLD_ID, 'a compliant reply rolls over')


def timeout_notice_survives_a_crash_before_the_enqueue():
    """(l) The process dies after the working-timeout row is durable but before its line is queued; the restarted
    supervisor's reconcile queues exactly one line with the durable id."""
    h, d = home(), fakes({f'claude-r2d2|{FABLE}': {'behaviour': 'timeout_working', 'records': 1}})
    sup = make(h, d)
    original = S._queue_notice_once
    reached = []

    def crash_queue(home_arg, channel, text, note_id):
        if not str(note_id).startswith('timeout-'):
            return original(home_arg, channel, text, note_id)
        check(turns(h) and turns(h)[-1].get('timeout') == 'working' and note_id == f"timeout-{turns(h)[-1]['n']}",
              f'the row must be durable before the enqueue: {turns(h)[-1:]}')
        reached.append(note_id)
        raise SimulatedCrash()
    S._queue_notice_once = crash_queue
    try:
        tick(sup, h, 0)
        event(h, 'crash')
        try:
            sup.run_once()
        except SimulatedCrash:
            pass
    finally:
        S._queue_notice_once = original
    check(len(reached) == 1 and not notices(h, 'manager turn timeout: '), f'crashed before the enqueue: {reached}')
    restarted = make(h, d)
    line = notices(h, 'manager turn timeout: ')
    check(len(line) == 1 and line[0].get('id') == reached[0], f'exactly one line after the restart: {line}')
    set_table(d, {})
    eid = turn(restarted, h, 5)
    check(eid in S.handled_ids(h) and len(notices(h, 'manager turn timeout: ')) == 1, 'served, no second line')
    no_downgrade(h)


SCENARIOS = [incident_2026_10_18_rollover_verified_from_the_transcript, first_request_over_the_threshold_fails_the_verification,
             verification_stays_pending_until_the_transcript_has_a_record, explicit_argument_still_settles_a_verification,
             incident_17_37_no_handoff_is_retried_once_without_backoff, unchanged_memory_backs_off_on_the_second_miss_only,
             working_timeout_is_not_a_fault, silent_timeout_is_a_fault_under_b14, four_working_timeouts_never_escalate,
             status_shows_the_session_start_and_the_last_timeout, persistent_mode_classifies_a_working_timeout_the_same,
             compaction_message_states_the_whole_contract, timeout_notice_survives_a_crash_before_the_enqueue]


def main(argv):
    if argv and (len(argv) != 2 or argv[0] != '--only' or argv[1] not in {fn.__name__ for fn in SCENARIOS}):
        print('[b15.acceptance] FAIL: use --only with a known scenario name, or no arguments')
        return 2
    only = argv[1] if argv else None
    for fn in SCENARIOS:
        if only and fn.__name__ != only:
            continue
        try:
            fn()
        except Fail as e:
            print(f'[b15.acceptance] FAIL {fn.__name__}: {e}')
            return 1
        except Exception as e:  # noqa: BLE001 - a crash inside a contract is a failure, reported the same way
            print(f'[b15.acceptance] FAIL {fn.__name__}: {type(e).__name__}: {e}')
            return 1
        print(f'[b15.acceptance] ok {fn.__name__}')
    print('[b15.acceptance] PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
