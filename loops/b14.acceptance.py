#!/usr/bin/env python3
"""Exit-owned offline b14 contracts (loops/b14.md): the session-in-use repair and the rule-4 safety valve. A fake
Claude CLI that creates its transcript file on every start and refuses `--session-id` on an id whose transcript
exists (the real collision), table-driven behaviours, a controlled clock, a short engine timeout, fake posters;
no real engine, GitHub or Slack. Every check raises (no `assert`). `--only <name>` runs one scenario."""
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
OLD_ID, SID = '79ef53fc-ccf9-4a02-a47f-9560830630d5', '8a4e6f56-1164-4a91-b014-728c29e353dd'  # the 2026-10-15 incident's ids
OLD_ID_14, SID_14 = '4e43204c-0000-4000-8000-000000000000', 'ccfd78e6-d585-4715-a07e-dd1b829ed236'  # the 2026-10-14 incident's ids
IN_USE = f'Error: Session ID {SID} is already in use.'  # the real row text (logs/attempts.jsonl, 2026-10-16)
# the sanitized rows of both incidents (loops/b14.md appendix): the replays are driven from them
ROWS_OCT14 = [
    {'at': '2026-10-14T17:49:23Z', 'engine': 'claude-r2d2', 'model': FABLE, 'rc': 124, 'classification': 'other', 'reason': 'timeout after 1800s', 'success': False},
    {'at': '2026-10-14T17:54:25Z', 'engine': 'claude-r2d2', 'model': FABLE, 'rc': 1, 'classification': 'other', 'reason': f'Error: Session ID {SID_14} is already in use.', 'success': False},
    {'at': '2026-10-14T17:54:26Z', 'engine': 'claude-l', 'model': FABLE, 'rc': 1, 'classification': 'other', 'reason': f'Error: Session ID {SID_14} is already in use.', 'success': False},
    {'at': '2026-10-14T18:07:26Z', 'engine': 'codex', 'model': ASTRA, 'rc': 0, 'classification': None, 'reason': '', 'success': True},
]
ROWS_OCT16 = [
    {'at': '2026-10-16T19:30:26Z', 'engine': 'claude-l', 'model': FABLE, 'rc': 1, 'classification': 'other', 'reason': IN_USE, 'success': False},
    {'at': '2026-10-16T19:35:35Z', 'engine': 'claude-l', 'model': FABLE, 'rc': 1, 'classification': 'other', 'reason': IN_USE, 'success': False},
    {'at': '2026-10-16T19:40:44Z', 'engine': 'claude-l', 'model': FABLE, 'rc': 1, 'classification': 'other', 'reason': IN_USE, 'success': False},
]
MIN = dt.timedelta(minutes=1)
HOME_FAKES = {}


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def home(engine=None, intent_started=False, config=None, sid=SID, old_id=OLD_ID):
    h = tempfile.mkdtemp(prefix='b14-home-')
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
    # the state both incidents left behind: a rolled-over id whose first turn never succeeded
    Path(h, 'rollover-intent.json').write_text(json.dumps({'old_id': old_id, 'new_id': sid, 'started': intent_started}) + '\n')
    cfg = {'dev_channel': 'C_DEV'}
    cfg.update(config or {})
    Path(h, 'config.json').write_text(json.dumps(cfg))
    return h


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import sys, os, json, time
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]
acc = {'fake-claude-r2d2': 'claude-r2d2', 'fake-claude-l': 'claude-l'}.get(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''), '?')
model = argv[argv.index('--model') + 1] if '--model' in argv else ''
resume = '--resume' in argv
sid = argv[argv.index('--resume') + 1] if resume else (argv[argv.index('--session-id') + 1] if '--session-id' in argv else '')
table = json.loads((D / 'table.json').read_text()) if (D / 'table.json').exists() else {}
behaviour = table.get(acc + '|' + model, 'ok')
cfg = Path(os.environ.get('CLAUDE_CONFIG_DIR') or (Path.home() / '.claude'))
project = cfg / 'projects' / os.getcwd().replace('/', '-')   # where Claude Code keeps this cwd's sessions
transcript = project / (sid + '.jsonl')
existed = transcript.exists()
msg = '<persistent start>' if '--input-format' in argv else (sys.stdin.read() if not sys.stdin.isatty() else '')
with open(D / 'calls.jsonl', 'a') as f:
    f.write(json.dumps({'acc': acc, 'model': model, 'argv': argv, 'resume': resume, 'sid': sid, 'transcript_existed': existed,
                        'behaviour': behaviour, 'stdin': msg[:200]}) + '\n')
ERRORS = {'credits': "You're out of usage credits. Switch to another model to continue.",
          'usage_limit': 'usage limit reached for this account', 'other': 'a plain runtime crash'}
def refuse(text):
    print(text, file=sys.stderr); sys.exit(1)
in_use = 'Error: Session ID ' + sid + ' is already in use.'
if behaviour == 'stuck':
    refuse(in_use)                                   # a session nothing can enter: --session-id and --resume both fail
if behaviour == 'session_in_use' and not resume:
    refuse(in_use)                                   # refused although no transcript is visible here (2026-10-14 shape)
if behaviour == 'session_in_use_stdout' and not resume:
    print('error: session id ' + sid + ' IS ALREADY IN USE.'); sys.exit(1)   # the diagnostic on stdout, mixed case
if not resume and existed and behaviour not in ERRORS:
    refuse(in_use)                                   # the real collision: --session-id on an id that already exists
project.mkdir(parents=True, exist_ok=True)
with open(transcript, 'a') as f:                     # the CLI creates the transcript as soon as it starts
    f.write(json.dumps({'type': 'user', 'sessionId': sid, 'cwd': os.getcwd()}) + '\n')
if behaviour == 'timeout':
    time.sleep(30)                                   # past the harness's engine timeout: rc 124 for the supervisor
if behaviour in ERRORS:
    refuse(ERRORS[behaviour])
REPLY = 'REPLY: ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none'
if '--input-format' not in argv:
    print(REPLY)
    sys.exit(0)
print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)   # the persistent child
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    env = json.loads(line)
    content = env.get('message', {}).get('content')
    text = content if isinstance(content, str) else ''.join(c.get('text', '') for c in content or [])
    with open(D / 'calls.jsonl', 'a') as f:
        f.write(json.dumps({'acc': acc, 'model': model, 'argv': argv, 'resume': resume, 'sid': sid, 'transcript_existed': existed,
                            'behaviour': behaviour, 'stdin': text[:200], 'persistent_turn': True}) + '\n')
    usage = {'input_tokens': 100, 'cache_creation_input_tokens': 1000, 'cache_read_input_tokens': 5000, 'output_tokens': 50}
    print(json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'usage': usage, 'content': [{'type': 'text', 'text': REPLY}]}}), flush=True)
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': REPLY, 'session_id': sid,
                      'usage': {'input_tokens': 300, 'cache_creation_input_tokens': 3000, 'cache_read_input_tokens': 15000, 'output_tokens': 150}}), flush=True)
'''

FAKE_CODEX = r'''#!/usr/bin/env python3
import sys, json
from pathlib import Path
D = Path(%(dir)r)
msg = sys.stdin.read() if not sys.stdin.isatty() else ''
argv = sys.argv[1:]
model = argv[argv.index('-m') + 1] if '-m' in argv else ''
table = json.loads((D / 'table.json').read_text()) if (D / 'table.json').exists() else {}
behaviour = table.get('codex|' + model, 'ok')
with open(D / 'calls.jsonl', 'a') as f:
    f.write(json.dumps({'acc': 'codex', 'model': model, 'argv': argv, 'resume': False, 'sid': '', 'behaviour': behaviour, 'stdin': msg[:200]}) + '\n')
if behaviour != 'ok':
    print({'usage_limit': 'usage limit reached', 'credits': 'out of credits', 'other': 'codex exploded'}[behaviour], file=sys.stderr)
    sys.exit(1)
out = argv[argv.index('-o') + 1]
Path(out).write_text('REPLY: codex ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none\n')
print('codex done')
'''


def fakes(table=None):
    d = Path(tempfile.mkdtemp(prefix='b14-engine-'))
    (d / 'table.json').write_text(json.dumps(table or {}))
    for name, src in (('claude', FAKE_CLAUDE), ('codex', FAKE_CODEX)):
        p = d / name
        p.write_text(src % {'dir': str(d)})
        p.chmod(0o755)
    return d


def set_table(d, table):
    (d / 'table.json').write_text(json.dumps(table))


def engines(d):
    return {'claude-r2d2': {'bin': str(d / 'claude'), 'cred': 'claude-r2d2.env'},
            'claude-l': {'bin': str(d / 'claude'), 'cred': 'claude-l.env'},
            'codex': {'bin': str(d / 'codex'), 'cred': None}}


def calls(d):
    p = d / 'calls.jsonl'
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def invoked(d):
    return [(c['acc'], c['model']) for c in calls(d) if not c.get('persistent_turn')]


def transcript_path(h, sid=SID):
    return Path(h, '.claude', 'projects', os.path.abspath(h).replace('/', '-'), f'{sid}.jsonl')


class SimulatedCrash(BaseException):
    pass


def claude_calls(d):
    return [(c['acc'], 'resume' if c['resume'] else 'session-id') for c in calls(d) if c['acc'] != 'codex' and not c.get('persistent_turn')]


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


T0 = dt.datetime(2026, 10, 16, 0, 0, 0, tzinfo=dt.timezone.utc)


def make(h, d, poster=None, frozen=False):
    HOME_FAKES[h] = d
    return S.Supervisor(home=h, engines=engines(d), poster=poster or Poster(), buildlog_poster=Poster(),
                        clock=Clock(T0, frozen=frozen), engine_timeout=3)


def event(h, text, thread='1.0', ts=None, instructs=True):
    ts = ts or f'{time.time():.6f}'
    ev = S.new_event('slack', {'channel': 'C_DEV', 'thread_ts': thread, 'ts': ts, 'text': text, 'instructs': instructs})
    S.append_event(h, ev)
    return ev['id']


def turn(sup, h, at_minutes=None, text='go', at_seconds=None):
    """One supervisor turn at the controlled time (minutes or seconds after T0); the back-off file is removed first
    as the scheduler would wait it out."""
    if at_seconds is not None:
        sup.clock.now = T0 + dt.timedelta(seconds=at_seconds)
    elif at_minutes is not None:
        sup.clock.now = T0 + at_minutes * MIN
    with_retry = Path(h, 'logs', 'retry-after')
    if with_retry.exists():
        with_retry.unlink()
    eid = event(h, text)
    check(sup.run_once() is True, f'the turn at {at_minutes} min did not run')
    return eid


def attempts(h):
    return [a for a in S.read_jsonl(os.path.join(h, 'logs', 'attempts.jsonl')) if isinstance(a, dict)]


def engine_file(h):
    return json.loads(Path(h, 'engine').read_text())


def intent(h):
    return json.loads(Path(h, 'rollover-intent.json').read_text())


def fault(h):
    p = Path(h, 'logs', 'fault.json')
    return json.loads(p.read_text()) if p.exists() else {}


def notices(h, needle):
    return [it for it in S.read_outbox(h) if needle in (it.get('text') or '')]


def status_lines(h, needle):
    return [l for l in S.status_text(h).splitlines() if l.startswith(needle)]


def no_downgrade(h, pairs=LADDER_PAIRS):
    for a in attempts(h):
        check((a.get('engine'), a.get('model')) in pairs, f'attempts.jsonl carries a pair outside the ladder: {a}')
        check(a.get('model') not in BELOW, f'a model below the floor was invoked: {a}')
    d = HOME_FAKES.get(h)
    if d is not None:
        bad = [(c.get('acc'), c.get('model')) for c in calls(d) if (c.get('acc'), c.get('model')) not in pairs]
        check(not bad, f'an engine process was started outside the ladder pairs: {bad}')


# ---------------------------------------------------------------------------------------------- scenarios

def incident_2026_10_15_timeout_then_resume():
    """(a) The first turn on the rolled-over id times out after the CLI created the transcript; the next turn must
    run `--resume` (the transcript exists) and serve, marking the intent started; no operator action."""
    h, d = home(intent_started=False), fakes({f'claude-r2d2|{FABLE}': 'timeout'})
    sup = make(h, d)
    eid = turn(sup, h, 0)
    check(claude_calls(d) == [('claude-r2d2', 'session-id')], f'the first call starts the fresh id: {claude_calls(d)}')
    row = attempts(h)[-1]
    check((row['rc'], row['classification'], row['success'], row['engine'], row['model']) == (124, 'other', False, 'claude-r2d2', FABLE)
          and row['reason'].startswith('timeout after '), f'the timeout row has the real row shape {ROWS_OCT14[0]}: {row}')
    check(intent(h)['started'] is False, 'a timed-out start proves nothing by itself: the marker stays false')
    check(S.find_claude_transcript(os.path.join(h, '.claude'), h, SID), 'the fake must have created the transcript (as the CLI does)')
    check(len(notices(h, 'engine fault: ')) == 1, 'the timeout is a fault with one line')
    check(eid in S.handled_ids(h), 'b8 unchanged: the timed-out request keeps its durable disposition and is never replayed')
    set_table(d, {})
    eid2 = turn(sup, h, 5)
    check(claude_calls(d)[1:] == [('claude-r2d2', 'resume')], f'the next turn must resume the existing transcript: {claude_calls(d)}')
    check(eid2 in S.handled_ids(h), 'the resumed turn serves the event')
    check(intent(h)['started'] is True, 'a served turn marks the session started')
    check(attempts(h)[-1]['success'] is True and attempts(h)[-1]['engine'] == 'claude-r2d2', f'success on the same engine: {attempts(h)[-1]}')
    check(fault(h).get('episode_id') is None, 'the served turn closes the fault episode')
    check(engine_file(h) == {'acc': 'claude-r2d2', 'model': FABLE} and not S.read_engine_runtime(h).get('active'), 'nothing moved')
    turn(sup, h, 10)
    check(claude_calls(d)[2:] == [('claude-r2d2', 'resume')], 'later turns resume as before')
    no_downgrade(h)


def incident_2026_10_14_in_use_retries_resume_in_the_same_turn():
    """(b) `--session-id` refused "already in use" with no transcript visible: the same turn retries once with
    `--resume` and serves; one attempt row, success, retried_resume true; no fault line."""
    h, d = home(intent_started=False, sid=SID_14, old_id=OLD_ID_14), fakes({f'claude-r2d2|{FABLE}': 'session_in_use'})
    sup = make(h, d)
    eid = turn(sup, h, 0)
    check(claude_calls(d) == [('claude-r2d2', 'session-id'), ('claude-r2d2', 'resume')], f'exactly one in-turn retry: {claude_calls(d)}')
    check(calls(d)[0]['transcript_existed'] is False and calls(d)[0]['sid'] == SID_14 == ROWS_OCT14[1]['reason'].split('Session ID ')[1].split(' ')[0],
          'the refusal came without a transcript on disk, on the 2026-10-14 id')
    check(eid in S.handled_ids(h), 'the retried turn serves the event')
    rows = attempts(h)
    check(len(rows) == 1 and rows[0]['success'] is True and rows[0].get('retried_resume') is True, f'one attempt row with retried_resume: {rows}')
    check(intent(h)['started'] is True, 'the retry marks the session started')
    check(not notices(h, 'engine fault: ') and (not Path(h, 'logs', 'fault.json').exists() or fault(h).get('episode_id') is None), 'no fault')
    turn(sup, h, 5)
    check(claude_calls(d)[2:] == [('claude-r2d2', 'resume')], 'the next turn resumes directly')
    no_downgrade(h)


def resume_refusal_is_a_fault_without_a_second_retry():
    """(c) A `--resume` call refused "already in use" is a fault: no further retry, the event waits, one line."""
    h, d = home(intent_started=True), fakes({f'claude-r2d2|{FABLE}': 'stuck'})
    sup = make(h, d)
    eid = turn(sup, h, 0)
    check(claude_calls(d) == [('claude-r2d2', 'resume')], f'one call only: {claude_calls(d)}')
    row = attempts(h)[-1]
    check((row['rc'], row['classification'], row['reason'], row['success']) == (1, 'other', IN_USE, False) and 'retried_resume' not in row,
          f'the row reads like the real 2026-10-16 rows {ROWS_OCT16[0]}: {row}')
    check(eid not in S.handled_ids(h) and Path(h, 'logs', 'retry-after').exists(), 'the event waits for the back-off')
    check(len(notices(h, 'engine fault: ')) == 1 and fault(h).get('count') == 1 and fault(h).get('episode_id'), f'fault episode with count 1: {fault(h)}')
    check(engine_file(h)['acc'] == 'claude-r2d2' and not S.read_engine_runtime(h).get('active'), 'nothing moved')
    no_downgrade(h)


def _stuck_r2d2(mode=None):
    """A home whose configured r2d2 session nothing can enter (the 2026-10-16 shape); `mode` adds the explicitly
    set mode key of b8 requirement 7 to the engine file."""
    engine = {'acc': 'claude-r2d2', 'model': FABLE}
    if mode:
        engine['mode'] = mode
    h, d = home(engine=engine, intent_started=False), fakes({f'claude-r2d2|{FABLE}': 'stuck'})
    return h, d, make(h, d)


def stuck_session_escalates_after_three_faults_over_thirty_minutes(mode=None):
    """(d) Faults at 0, 5 and 10 minutes change nothing; the fault at 35 minutes (count 4, span 35 min) escalates:
    the walk continues to the other Fable account, one `engine fault escalated` line, the move persisted, the
    fault episode open with a probe one hour out."""
    h, d, sup = _stuck_r2d2(mode)
    before = Path(h, 'engine').read_bytes()
    for m in (0, 5, 10):
        turn(sup, h, m)
        check(all(acc == 'claude-r2d2' for acc, _ in claude_calls(d)), f'before the threshold only the faulting engine is called: {claude_calls(d)}')
        check(Path(h, 'engine').read_bytes() == before and not S.read_engine_runtime(h).get('active'), f'no move at {m} min')
    f = fault(h)
    check(f.get('count') == 3 and f.get('step', {}).get('acc') == 'claude-r2d2' and not f.get('escalated'), f'three counted faults, not escalated: {f}')
    check(len(notices(h, 'engine fault: ')) == 1 and not notices(h, 'engine fault escalated'), 'one fault line, no escalation yet')
    eid = turn(sup, h, 35)
    check(invoked(d)[-2:] == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'the escalated walk continues to the other Fable account: {invoked(d)[-3:]}')
    check(eid in S.handled_ids(h), 'the escalated walk serves the event')
    check(engine_file(h) == dict({'acc': 'claude-l', 'model': FABLE}, **({'mode': mode} if mode else {})), f'the move is persisted (mode kept): {engine_file(h)}')
    rt = S.read_engine_runtime(h)
    check(rt.get('active') and {k: rt.get('configured', {}).get(k) for k in ('acc', 'model')} == {'acc': 'claude-r2d2', 'model': FABLE}
          and {k: rt.get('effective', {}).get(k) for k in ('acc', 'model')} == {'acc': 'claude-l', 'model': FABLE}, f'fallback episode: {rt}')
    check(len(notices(h, 'fallback started')) == 1, 'one fallback started line')
    esc = notices(h, 'engine fault escalated: ')
    check(len(esc) == 1 and esc[0].get('thread_ts') is None and esc[0].get('channel') == 'C_DEV'
          and esc[0]['text'] == f'engine fault escalated: tier 1 claude-r2d2 ({FABLE}): other: {IN_USE}', f'the escalated line: {esc}')
    check(esc[0].get('id') == f"fault-{fault(h)['episode_id']}-escalated", f'durable id: {esc[0].get("id")}')
    f = fault(h)
    check(f.get('escalated') is True and f.get('episode_id') and f.get('count') == 4, f'episode open and escalated: {f}')
    due = S.parse_iso(f.get('probe_after')) - sup.clock.now
    check(dt.timedelta(seconds=3590) <= due <= dt.timedelta(seconds=3600), f'probe one hour out: {f}')
    check(fault(h).get('probe_after') and 'probe_after' not in rt, 'the same-tier move schedules no tier probe (rule 3 unchanged)')
    no_downgrade(h)
    return h, d, sup


def escalated_episode_returns_at_the_hourly_probe(mode=None):
    """(e) Inside the hour the faulted step is never called; at the hour the probe serves on the configured pair:
    fallback ended, engine file restored, fault episode closed."""
    h, d, sup = stuck_session_escalates_after_three_faults_over_thirty_minutes(mode)
    n = len(calls(d))
    turn(sup, h, 40)
    turn(sup, h, 80)
    check(invoked(d)[n:] == [('claude-l', FABLE), ('claude-l', FABLE)], f'inside the hour only the effective pair runs: {invoked(d)[n:]}')
    set_table(d, {})  # the session fault clears
    n = len(calls(d))
    eid = turn(sup, h, 96)
    check(invoked(d)[n:] == [('claude-r2d2', FABLE)], f'at the hour the faulted step is probed first and serves: {invoked(d)[n:]}')
    check(claude_calls(d)[-1] == ('claude-r2d2', 'resume'), 'the probe resumes the existing session')
    check(eid in S.handled_ids(h) and engine_file(h) == dict({'acc': 'claude-r2d2', 'model': FABLE}, **({'mode': mode} if mode else {})), f'the return lands in the engine file: {engine_file(h)}')
    check(not S.read_engine_runtime(h).get('active') and len(notices(h, 'fallback ended')) == 1, 'fallback ended once')
    check(fault(h).get('episode_id') is None and fault(h).get('closed_at'), f'fault episode closed: {fault(h)}')
    check(len(notices(h, 'engine fault escalated: ')) == 1, 'no second escalated line')
    n = len(calls(d))
    turn(sup, h, 100)
    check(invoked(d)[n:] == [('claude-r2d2', FABLE)], 'back on the configured pair')
    no_downgrade(h)


def a_faulting_probe_keeps_the_effective_pair():
    """(f) At the hour the faulted step still faults: the walk continues at the effective pair, no second
    escalated line, the next probe one hour later, count grows."""
    h, d, sup = stuck_session_escalates_after_three_faults_over_thirty_minutes()
    n = len(calls(d))
    eid = turn(sup, h, 96)
    check(invoked(d)[n:] == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'probe faults, effective pair serves: {invoked(d)[n:]}')
    check(eid in S.handled_ids(h), 'served')
    f = fault(h)
    check(f.get('escalated') is True and f.get('episode_id') and f.get('count') == 5, f'still escalated, count 5: {f}')
    due = S.parse_iso(f.get('probe_after')) - sup.clock.now
    check(dt.timedelta(seconds=3590) <= due <= dt.timedelta(seconds=3600), f'next probe one hour out: {f}')
    check(len(notices(h, 'engine fault escalated: ')) == 1 and len(notices(h, 'engine fault: ')) == 1, 'no new lines')
    check(engine_file(h) == {'acc': 'claude-l', 'model': FABLE}, 'the effective pair stays')
    n = len(calls(d))
    turn(sup, h, 120)
    check(invoked(d)[n:] == [('claude-l', FABLE)], 'inside the next hour the faulted step is not called')
    no_downgrade(h)


def escalation_walks_the_ladder_never_below_the_floor():
    """(g) Configured L stuck with r2d2 out of credits: the escalation lands on Astra; with Astra out as well, on
    Opus 5.5 at the floor; nothing below the floor."""
    for table, expect, landing in (({f'claude-l|{FABLE}': 'stuck', f'claude-r2d2|{FABLE}': 'credits'},
                                    [('claude-l', FABLE), ('claude-r2d2', FABLE), ('codex', ASTRA)], {'acc': 'codex', 'model': ASTRA}),
                                   ({f'claude-l|{FABLE}': 'stuck', f'claude-r2d2|{FABLE}': 'credits', f'codex|{ASTRA}': 'usage_limit'},
                                    [('claude-l', FABLE), ('claude-r2d2', FABLE), ('codex', ASTRA), ('claude-r2d2', OPUS)], {'acc': 'claude-r2d2', 'model': OPUS})):
        h, d = home(engine={'acc': 'claude-l', 'model': FABLE}), fakes(table)
        sup = make(h, d)
        for m in (0, 5, 10):
            turn(sup, h, m)
        check(all(acc == 'claude-l' for acc, _ in claude_calls(d)), f'only L before the threshold: {claude_calls(d)}')
        n = len(calls(d))
        eid = turn(sup, h, 35)
        check(invoked(d)[n:] == expect, f'the escalated walk: {invoked(d)[n:]}')
        check(eid in S.handled_ids(h) and engine_file(h) == landing, f'landing: {engine_file(h)}')
        check(len(notices(h, 'engine fault escalated: ')) == 1 and len(notices(h, 'fallback started')) == 1, 'one escalated line, one fallback started')
        rt = S.read_engine_runtime(h)
        check(rt.get('active') and rt.get('probe_after'), f'a lower tier carries the rule-3 probe as well: {rt}')
        no_downgrade(h)


def long_episode_shows_in_status_and_the_digest_event():
    """(h) Two faults 35 minutes apart (count 2: no escalation) show the status line and the digest event line;
    29 minutes apart show nothing; a served turn removes it."""
    h0, d0, sup0 = _stuck_r2d2()
    turn(sup0, h0, 0)
    turn(sup0, h0, 29)
    check(not status_lines(h0, 'fault episode:'), 'nothing before thirty minutes')
    sup0.add_timer_event(now=time.time(), text='timer: daily digest', digest=True)
    ev = [e for e in S.pending_events(h0) if (e.get('payload') or {}).get('digest')]
    check(ev and 'fault episode' not in (ev[-1]['payload'].get('text') or ''), f'digest event without a short episode: {ev[-1]["payload"] if ev else None}')
    no_downgrade(h0)
    h, d, sup = _stuck_r2d2()
    turn(sup, h, 0)
    turn(sup, h, 35)
    f = fault(h)
    check(f.get('count') == 2 and not f.get('escalated') and f.get('episode_id'), f'two faults, not escalated: {f}')
    check(all(acc == 'claude-r2d2' for acc, _ in claude_calls(d)) and engine_file(h)['acc'] == 'claude-r2d2', 'nothing moved')
    lines = status_lines(h, 'fault episode:')
    since = S._status_pdt(f['since']) if hasattr(S, '_status_pdt') else None
    check(len(lines) == 1 and lines[0] == f'fault episode: tier 1 claude-r2d2 ({FABLE}) since {since}, 2 faults over 35 min', f'status line: {lines} (since {since})')
    text = S.status_text(h)
    idx = text.splitlines().index(lines[0])
    check(idx > 0 and text.splitlines()[idx - 1].startswith(('engine:', 'engine policy:', 'identity:', 'engine fallback:', 'last turn attempts:', 'off configured engine:', 'accounts:')),
          f'the line sits with the engine diagnostics: {text.splitlines()[max(0, idx - 1)]!r}')
    sup.add_timer_event(now=time.time() + 600, text='timer: daily digest', digest=True)
    ev = [e for e in S.pending_events(h) if (e.get('payload') or {}).get('digest')]
    check(ev and (ev[-1]['payload'].get('text') or '').splitlines()[1:] == [lines[0]], f'the digest event carries the line: {ev[-1]["payload"]}')
    set_table(d, {})
    turn(sup, h, 40)
    check(not status_lines(h, 'fault episode:') and fault(h).get('episode_id') is None, 'closed episodes are not shown')
    no_downgrade(h)


def below_the_threshold_never_escalates():
    """(i) Two faults an hour apart (count 2) and three faults over ten minutes (span 10) never escalate."""
    h, d, sup = _stuck_r2d2()
    turn(sup, h, 0)
    turn(sup, h, 60)
    check(claude_calls(d) and all(acc == 'claude-r2d2' for acc, _ in claude_calls(d)), f'count 2 over an hour: only the faulting engine: {claude_calls(d)}')
    check(not fault(h).get('escalated') and not notices(h, 'engine fault escalated'), 'no escalation on count 2')
    h2, d2, sup2 = _stuck_r2d2()
    for m in (0, 5, 10):
        turn(sup2, h2, m)
    check(all(acc == 'claude-r2d2' for acc, _ in claude_calls(d2)) and fault(h2).get('count') == 3 and not fault(h2).get('escalated'), f'count 3 over ten minutes: {fault(h2)}')
    check(not notices(h2, 'engine fault escalated') and engine_file(h2)['acc'] == 'claude-r2d2', 'nothing moved')
    no_downgrade(h)
    no_downgrade(h2)


def explicit_selection_closes_an_escalated_episode(mode=None):
    """(j) An operator's explicit engine selection during an escalated episode closes it: the next turn runs the
    selected pair and the faulted step is not probed."""
    h, d, sup = stuck_session_escalates_after_three_faults_over_thirty_minutes(mode)
    S.set_engine(h, 'claude-l', FABLE, engines(d))
    check(fault(h).get('episode_id') is None, f'the explicit selection closes the fault episode: {fault(h)}')
    check(engine_file(h) == dict({'acc': 'claude-l', 'model': FABLE}, **({'mode': mode} if mode else {})), f'mode kept: {engine_file(h)}')
    n = len(calls(d))
    turn(sup, h, 100)
    check(invoked(d)[n:] == [('claude-l', FABLE)], f'the selected pair runs, no probe: {invoked(d)[n:]}')
    no_downgrade(h)


def explicit_mode_key_keeps_the_return_and_the_selection_honest():
    """(k) Review 8.1: with an explicitly set mode key in the engine file (b8 requirement 7), the escalation, the
    configured pair's return (exactly one `fallback ended`) and the explicit selection behave the same and keep
    the mode."""
    escalated_episode_returns_at_the_hourly_probe(mode='per-turn')
    explicit_selection_closes_an_escalated_episode(mode='per-turn')


def escalated_notice_survives_a_crash_before_the_enqueue():
    """(l) Review 1.1: the process dies after the episode was marked escalated but before its line was queued; the
    next supervisor's start-up reconcile queues exactly one line with the durable id."""
    h, d, sup = _stuck_r2d2()
    for m in (0, 5, 10):
        turn(sup, h, m)
    original = S._queue_notice_once
    reached = []
    def crash_queue(home_arg, channel, text, note_id):
        if not note_id.endswith('-escalated'):
            return original(home_arg, channel, text, note_id)
        f = fault(h)
        check(f.get('escalated') is True and f.get('escalated_queued') is False and note_id == f"fault-{f['episode_id']}-escalated",
              f'the escalation must be durable and unqueued before the enqueue: {f}')
        reached.append(note_id)
        raise SimulatedCrash()
    S._queue_notice_once = crash_queue
    try:
        sup.clock.now = T0 + 35 * MIN
        Path(h, 'logs', 'retry-after').unlink(missing_ok=True)
        event(h, 'crash')
        try:
            sup.run_once()
        except SimulatedCrash:
            pass
    finally:
        S._queue_notice_once = original
    check(len(reached) == 1 and not notices(h, 'engine fault escalated'), f'crashed before the enqueue: {reached}')
    restarted = make(h, d)  # the start-up reconcile finishes the durable, not-yet-queued line
    esc = notices(h, 'engine fault escalated: ')
    check(len(esc) == 1 and esc[0].get('id') == reached[0] and fault(h).get('escalated_queued') is True, f'exactly one line after the restart: {esc}')
    restarted.clock.now = T0 + 36 * MIN
    eid = turn(restarted, h, 36)
    check(eid in S.handled_ids(h) and engine_file(h)['acc'] == 'claude-l' and len(notices(h, 'engine fault escalated: ')) == 1,
          'the restarted supervisor serves on the other account with no second line')
    no_downgrade(h)


def both_probes_due_return_to_a_non_configured_step():
    """(m) Review 1.2: L serves (r2d2 out of credits) and then gets stuck: the escalation lands on Astra (both the
    fault probe and rule 3's tier probe are scheduled); at the hour the faulted step L goes first, serves, and the
    move back is a `fallback moved`; the fault episode closes while the fallback episode stays active."""
    h, d = home(engine={'acc': 'claude-l', 'model': FABLE}, intent_started=False), fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'stuck'})
    Path(h, 'engine-runtime.json').write_text(json.dumps({'configured': {'acc': 'claude-r2d2', 'model': FABLE}, 'effective': {'acc': 'claude-l', 'model': FABLE},
                                                          'episode_id': 'sticky-b14', 'since': '2026-10-15T19:52:43Z', 'reason': 'credits', 'active': True}))
    sup = make(h, d)
    for m in (0, 5, 10):
        turn(sup, h, m)
    check(all(acc == 'claude-l' for acc, _ in claude_calls(d)), f'only the effective pair before the threshold: {claude_calls(d)}')
    n = len(calls(d))
    turn(sup, h, 35)
    check(invoked(d)[n:] == [('claude-l', FABLE), ('claude-r2d2', FABLE), ('codex', ASTRA)], f'the escalated walk: {invoked(d)[n:]}')
    check(engine_file(h) == {'acc': 'codex', 'model': ASTRA} and len(notices(h, 'fallback moved')) == 1, f'moved to Astra: {engine_file(h)}')
    rt, f = S.read_engine_runtime(h), fault(h)
    check(rt.get('probe_after') and f.get('probe_after') and f.get('escalated'), f'both probes scheduled: {rt} {f}')
    n = len(calls(d))
    turn(sup, h, 60)
    check(invoked(d)[n:] == [('codex', ASTRA)], f'inside the hour only Astra: {invoked(d)[n:]}')
    set_table(d, {f'claude-r2d2|{FABLE}': 'credits'})  # L recovers, r2d2 still out
    n = len(calls(d))
    eid = turn(sup, h, 96)
    check(invoked(d)[n:] == [('claude-l', FABLE)], f'both probes due: the faulted step goes first and serves: {invoked(d)[n:]}')
    check(eid in S.handled_ids(h) and engine_file(h) == {'acc': 'claude-l', 'model': FABLE}, f'back on L: {engine_file(h)}')
    check(len(notices(h, 'fallback moved')) == 2 and notices(h, 'fallback moved')[-1]['text'].endswith(f'now running tier 1/3 claude-l ({FABLE})'), f'the move back: {notices(h, "fallback moved")}')
    check(fault(h).get('episode_id') is None and S.read_engine_runtime(h).get('active'), 'fault episode closed, fallback episode still active (configured pair out)')
    no_downgrade(h)


def persistent_mode_resumes_an_existing_transcript():
    """(n) Review 2.2: in persistent mode a not-yet-started id whose transcript exists is started with `--resume`
    (requirement 1 on the second invocation path); the request is sent to that child and served."""
    h, d = home(engine={'acc': 'claude-r2d2', 'model': FABLE, 'mode': 'persistent'}, intent_started=False), fakes()
    tp = transcript_path(h)
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(json.dumps({'type': 'user', 'sessionId': SID}) + '\n')
    sup = make(h, d)
    try:
        eid = turn(sup, h, 0)
        starts = [c for c in calls(d) if '--input-format' in c['argv'] and not c.get('persistent_turn')]
        check(len(starts) == 1 and starts[0]['resume'] is True and starts[0]['sid'] == SID, f'the persistent child resumes the existing transcript: {starts}')
        check(eid in S.handled_ids(h) and intent(h)['started'] is True, 'served; the marker is set')
        check(claude_calls(d) == [('claude-r2d2', 'resume')], f'no --session-id start at all: {claude_calls(d)}')
    finally:
        sup._retire_persistent('planned', 'harness end')
    no_downgrade(h)


def a_second_faulting_step_replaces_the_episode():
    """(o) Review 5.1: while an escalated episode (r2d2) is open and L serves, L gets stuck too: its fault opens a
    new episode on L (count 1, not escalated; the r2d2 probe is dropped and r2d2 rejoins the ordinary rotation);
    three more faults over thirty minutes escalate L, and the walk reaches r2d2, which has recovered: the configured
    pair serves, `fallback ended` once, the fault episode closes."""
    h, d, sup = stuck_session_escalates_after_three_faults_over_thirty_minutes()
    set_table(d, {f'claude-r2d2|{FABLE}': 'stuck', f'claude-l|{FABLE}': 'stuck'})
    turn(sup, h, 50)
    f = fault(h)
    check(f.get('step', {}).get('acc') == 'claude-l' and f.get('count') == 1 and not f.get('escalated'), f'a new episode on L: {f}')
    check(engine_file(h)['acc'] == 'claude-l' and len(notices(h, 'engine fault: ')) == 2, 'one new fault line, nothing moved')
    turn(sup, h, 55)
    turn(sup, h, 60)
    check(all(acc == 'claude-l' for acc, _ in claude_calls(d)[-3:]), f'rule 4 on L below the threshold: {claude_calls(d)[-3:]}')
    set_table(d, {f'claude-l|{FABLE}': 'stuck'})  # r2d2 recovered meanwhile
    n = len(calls(d))
    eid = turn(sup, h, 85)
    check(invoked(d)[n:] == [('claude-l', FABLE), ('claude-r2d2', FABLE)], f'L escalates, the walk reaches r2d2: {invoked(d)[n:]}')
    check(eid in S.handled_ids(h) and engine_file(h) == {'acc': 'claude-r2d2', 'model': FABLE}, f'home again: {engine_file(h)}')
    check(len(notices(h, 'fallback ended')) == 1 and not S.read_engine_runtime(h).get('active'), 'fallback ended once')
    check(fault(h).get('episode_id') is None and len(notices(h, 'engine fault escalated: ')) == 2, f'closed; one escalated line per episode: {fault(h)}')
    no_downgrade(h)


def exact_boundary_three_faults_spanning_1800_seconds():
    """(q) Review 8.1, with a non-advancing clock: three faults spanning exactly 1800 s escalate; three spanning
    1799 s do not; the third fault alone (count) is not enough at 1800 s with only two faults."""
    for span, expect in ((1799, False), (1800, True)):
        h, d = home(intent_started=True), fakes({f'claude-r2d2|{FABLE}': 'stuck'})
        sup = make(h, d, frozen=True)
        turn(sup, h, at_seconds=0)
        turn(sup, h, at_seconds=900)
        check(fault(h).get('since') == S._iso(T0) and fault(h).get('count') == 2, f'frozen clock: since is exactly T0: {fault(h)}')
        turn(sup, h, at_seconds=span)
        check(bool(fault(h).get('escalated')) is expect and (engine_file(h)['acc'] == 'claude-l') is expect,
              f'span {span}: escalated={fault(h).get("escalated")} engine={engine_file(h)}')
        no_downgrade(h)
    h, d = home(intent_started=True), fakes({f'claude-r2d2|{FABLE}': 'stuck'})
    sup = make(h, d, frozen=True)
    turn(sup, h, at_seconds=0)
    turn(sup, h, at_seconds=1800)
    check(not fault(h).get('escalated') and fault(h).get('count') == 2, 'two faults at 1800 s: the count is short')
    no_downgrade(h)


def uncertain_failures_keep_b8_precedence_inside_the_valve():
    """(p) Review B1: a timeout on the step whose episode reaches the threshold opens the valve (escalation durable,
    its line queued) but the walk stops there without replay: the event gets its durable disposition, nothing is
    persisted this turn; the next turn serves on the other account and persists the move. A timeout on the probe
    stops the walk the same way, counts, and reschedules the probe."""
    h, d = home(intent_started=True), fakes({f'claude-r2d2|{FABLE}': 'timeout'})
    sup = make(h, d)
    for m in (0, 5, 10):
        eid = turn(sup, h, m)
        check(eid in S.handled_ids(h) and claude_calls(d)[-1] == ('claude-r2d2', 'resume'), f'each timed-out request is disposed, never replayed: {claude_calls(d)}')
    before = Path(h, 'engine').read_bytes()
    eid = turn(sup, h, 35)
    check(invoked(d)[-1] == ('claude-r2d2', FABLE) and len(invoked(d)) == 4, f'the walk stops at the uncertain failure even though the valve opened: {invoked(d)}')
    check(eid in S.handled_ids(h) and Path(h, 'engine').read_bytes() == before, 'durable disposition, nothing persisted this turn')
    f = fault(h)
    check(f.get('escalated') is True and f.get('count') == 4 and len(notices(h, 'engine fault escalated: ')) == 1, f'the escalation is durable and announced: {f}')
    n = len(calls(d))
    eid = turn(sup, h, 40)
    check(invoked(d)[n:] == [('claude-l', FABLE)] and eid in S.handled_ids(h) and engine_file(h)['acc'] == 'claude-l', f'the next turn serves on the other account: {invoked(d)[n:]}')
    check(len(notices(h, 'fallback started')) == 1, 'the move is persisted as a fallback episode')
    n = len(calls(d))
    eid = turn(sup, h, 100)  # the probe: r2d2 still times out
    check(invoked(d)[n:] == [('claude-r2d2', FABLE)] and eid in S.handled_ids(h), f'the probe timeout stops the walk, no replay on L: {invoked(d)[n:]}')
    f = fault(h)
    check(f.get('count') == 5 and f.get('escalated') and S.parse_iso(f.get('probe_after')) > sup.clock.now + dt.timedelta(seconds=3500), f'counted and rescheduled: {f}')
    check(engine_file(h)['acc'] == 'claude-l' and len(notices(h, 'engine fault escalated: ')) == 1, 'effective pair kept, no second line')
    no_downgrade(h)


def persistent_start_refused_resumes_once_and_stdout_diagnostics_count():
    """(r) Review 8.2: a persistent start refused "already in use" is followed by exactly one --resume start that
    serves the same message (success row with retried_resume); a refused --resume start is not retried; a
    per-turn refusal written to stdout in mixed case is recognised too."""
    h, d = home(engine={'acc': 'claude-r2d2', 'model': FABLE, 'mode': 'persistent'}, intent_started=False), fakes({f'claude-r2d2|{FABLE}': 'session_in_use'})
    sup = make(h, d)
    try:
        eid = turn(sup, h, 0)
        starts = [c for c in calls(d) if '--input-format' in c['argv'] and not c.get('persistent_turn')]
        check([c['resume'] for c in starts] == [False, True], f'one refused --session-id start, one --resume start: {starts}')
        served = [c for c in calls(d) if c.get('persistent_turn')]
        check(len(served) == 1 and served[0]['resume'] is True and eid in S.handled_ids(h), f'the message is served once, by the resumed child: {served}')
        rows = attempts(h)
        check(rows[-1]['success'] is True and rows[-1].get('retried_resume') is True and intent(h)['started'] is True, f'success row with retried_resume: {rows}')
    finally:
        sup._retire_persistent('planned', 'harness end')
    no_downgrade(h)
    h2, d2 = home(engine={'acc': 'claude-r2d2', 'model': FABLE, 'mode': 'persistent'}, intent_started=True), fakes({f'claude-r2d2|{FABLE}': 'stuck'})
    sup2 = make(h2, d2)
    try:
        eid = turn(sup2, h2, 0)
        starts = [c for c in calls(d2) if not c.get('persistent_turn')]
        check(all(c['resume'] for c in starts) and eid not in S.handled_ids(h2), f'a refused --resume start is never retried with --resume again: {starts}')
    finally:
        sup2._retire_persistent('planned', 'harness end')
    no_downgrade(h2)
    h3, d3 = home(intent_started=False), fakes({f'claude-r2d2|{FABLE}': 'session_in_use_stdout'})
    sup3 = make(h3, d3)
    eid = turn(sup3, h3, 0)
    check(claude_calls(d3) == [('claude-r2d2', 'session-id'), ('claude-r2d2', 'resume')] and eid in S.handled_ids(h3), f'a mixed-case stdout refusal is retried too: {claude_calls(d3)}')
    no_downgrade(h3)


def a_later_step_faulting_inside_the_escalated_walk_is_skipped():
    """(s) Review 8.3: r2d2 escalates, L returns an ordinary runtime fault inside the same walk, Astra serves; the
    episode (id and step) is unchanged and both failed attempts are recorded."""
    h, d = home(intent_started=True), fakes({f'claude-r2d2|{FABLE}': 'stuck'})
    sup = make(h, d)
    for m in (0, 5, 10):
        turn(sup, h, m)
    episode = fault(h)['episode_id']
    set_table(d, {f'claude-r2d2|{FABLE}': 'stuck', f'claude-l|{FABLE}': 'other'})
    n, rows = len(calls(d)), len(attempts(h))
    eid = turn(sup, h, 35)
    check(invoked(d)[n:] == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA)], f'the walk goes past the later fault: {invoked(d)[n:]}')
    check(eid in S.handled_ids(h) and engine_file(h) == {'acc': 'codex', 'model': ASTRA}, f'Astra serves and is persisted: {engine_file(h)}')
    f = fault(h)
    check(f.get('episode_id') == episode and f.get('step', {}).get('acc') == 'claude-r2d2' and f.get('escalated'), f'the episode is unchanged: {f}')
    new = attempts(h)[rows:]
    check([(a['engine'], a['success'], a['classification']) for a in new] == [('claude-r2d2', False, 'other'), ('claude-l', False, 'other'), ('codex', True, None)], f'both failed attempts recorded: {new}')
    check(len(notices(h, 'engine fault: ')) == 1 and len(notices(h, 'engine fault escalated: ')) == 1, 'no extra fault lines for the later step')
    no_downgrade(h)


SCENARIOS = [incident_2026_10_15_timeout_then_resume, incident_2026_10_14_in_use_retries_resume_in_the_same_turn,
             resume_refusal_is_a_fault_without_a_second_retry, stuck_session_escalates_after_three_faults_over_thirty_minutes,
             escalated_episode_returns_at_the_hourly_probe, a_faulting_probe_keeps_the_effective_pair,
             escalation_walks_the_ladder_never_below_the_floor, long_episode_shows_in_status_and_the_digest_event,
             below_the_threshold_never_escalates, explicit_selection_closes_an_escalated_episode,
             explicit_mode_key_keeps_the_return_and_the_selection_honest, escalated_notice_survives_a_crash_before_the_enqueue,
             both_probes_due_return_to_a_non_configured_step, persistent_mode_resumes_an_existing_transcript,
             a_second_faulting_step_replaces_the_episode, exact_boundary_three_faults_spanning_1800_seconds,
             uncertain_failures_keep_b8_precedence_inside_the_valve, persistent_start_refused_resumes_once_and_stdout_diagnostics_count,
             a_later_step_faulting_inside_the_escalated_walk_is_skipped]


def main(argv):
    if argv and (len(argv) != 2 or argv[0] != '--only' or argv[1] not in {fn.__name__ for fn in SCENARIOS}):
        print('[b14.acceptance] FAIL: use --only with a known scenario name, or no arguments')
        return 2
    only = argv[1] if argv else None
    for fn in SCENARIOS:
        if only and fn.__name__ != only:
            continue
        try:
            fn()
        except Fail as e:
            print(f'[b14.acceptance] FAIL {fn.__name__}: {e}')
            return 1
        except Exception as e:  # noqa: BLE001 - a crash inside a contract is a failure, reported the same way
            print(f'[b14.acceptance] FAIL {fn.__name__}: {type(e).__name__}: {e}')
            return 1
        print(f'[b14.acceptance] ok {fn.__name__}')
    print('[b14.acceptance] PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
