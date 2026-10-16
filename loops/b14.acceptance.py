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
IN_USE = f'Error: Session ID {SID} is already in use.'  # the real row text (logs/attempts.jsonl, 2026-10-16)
MIN = dt.timedelta(minutes=1)
HOME_FAKES = {}


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


def home(engine=None, intent_started=False, config=None):
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
    Path(h, 'session-id').write_text(SID + '\n')
    # the state both incidents left behind: a rolled-over id whose first turn never succeeded
    Path(h, 'rollover-intent.json').write_text(json.dumps({'old_id': OLD_ID, 'new_id': SID, 'started': intent_started}) + '\n')
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
msg = sys.stdin.read() if not sys.stdin.isatty() else ''
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
if not resume and existed and behaviour not in ERRORS:
    refuse(in_use)                                   # the real collision: --session-id on an id that already exists
project.mkdir(parents=True, exist_ok=True)
with open(transcript, 'a') as f:                     # the CLI creates the transcript as soon as it starts
    f.write(json.dumps({'type': 'user', 'sessionId': sid, 'cwd': os.getcwd()}) + '\n')
if behaviour == 'timeout':
    time.sleep(30)                                   # past the harness's engine timeout: rc 124 for the supervisor
if behaviour in ERRORS:
    refuse(ERRORS[behaviour])
print('REPLY: ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none')
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
    return [(c['acc'], c['model']) for c in calls(d)]


def claude_calls(d):
    return [(c['acc'], 'resume' if c['resume'] else 'session-id') for c in calls(d) if c['acc'] != 'codex']


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


T0 = dt.datetime(2026, 10, 16, 0, 0, 0, tzinfo=dt.timezone.utc)


def make(h, d, poster=None):
    HOME_FAKES[h] = d
    return S.Supervisor(home=h, engines=engines(d), poster=poster or Poster(), buildlog_poster=Poster(),
                        clock=Clock(T0), engine_timeout=3)


def event(h, text, thread='1.0', ts=None, instructs=True):
    ts = ts or f'{time.time():.6f}'
    ev = S.new_event('slack', {'channel': 'C_DEV', 'thread_ts': thread, 'ts': ts, 'text': text, 'instructs': instructs})
    S.append_event(h, ev)
    return ev['id']


def turn(sup, h, at_minutes=None, text='go'):
    """One supervisor turn at the controlled time (minutes after T0); the back-off file is removed first as the
    scheduler would wait it out."""
    if at_minutes is not None:
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
    check(attempts(h)[-1]['rc'] == 124 and not attempts(h)[-1]['success'], f'the timeout is recorded as rc 124: {attempts(h)}')
    check(intent(h)['started'] is False, 'a timed-out start proves nothing by itself: the marker stays false')
    check(S.find_claude_transcript(os.path.join(h, '.claude'), h, SID), 'the fake must have created the transcript (as the CLI does)')
    check(len(notices(h, 'engine fault: ')) == 1, 'the timeout is a fault with one line')
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
    h, d = home(intent_started=False), fakes({f'claude-r2d2|{FABLE}': 'session_in_use'})
    sup = make(h, d)
    eid = turn(sup, h, 0)
    check(claude_calls(d) == [('claude-r2d2', 'session-id'), ('claude-r2d2', 'resume')], f'exactly one in-turn retry: {claude_calls(d)}')
    check(calls(d)[0]['transcript_existed'] is False, 'the refusal came without a transcript on disk')
    check(eid in S.handled_ids(h), 'the retried turn serves the event')
    rows = attempts(h)
    check(len(rows) == 1 and rows[0]['success'] is True and rows[0].get('retried_resume') is True, f'one attempt row with retried_resume: {rows}')
    check(intent(h)['started'] is True, 'the retry marks the session started')
    check(not notices(h, 'engine fault: ') and not Path(h, 'logs', 'fault.json').exists() or fault(h).get('episode_id') is None, 'no fault')
    turn(sup, h, 5)
    check(claude_calls(d)[2:] == [('claude-r2d2', 'resume')], 'the next turn resumes directly')
    no_downgrade(h)


def resume_refusal_is_a_fault_without_a_second_retry():
    """(c) A `--resume` call refused "already in use" is a fault: no further retry, the event waits, one line."""
    h, d = home(intent_started=True), fakes({f'claude-r2d2|{FABLE}': 'stuck'})
    sup = make(h, d)
    eid = turn(sup, h, 0)
    check(claude_calls(d) == [('claude-r2d2', 'resume')], f'one call only: {claude_calls(d)}')
    check(eid not in S.handled_ids(h) and Path(h, 'logs', 'retry-after').exists(), 'the event waits for the back-off')
    check(len(notices(h, 'engine fault: ')) == 1 and fault(h).get('count') == 1 and fault(h).get('episode_id'), f'fault episode with count 1: {fault(h)}')
    check(engine_file(h)['acc'] == 'claude-r2d2' and not S.read_engine_runtime(h).get('active'), 'nothing moved')
    no_downgrade(h)


def _stuck_r2d2():
    """A home whose configured r2d2 session nothing can enter (the 2026-10-16 shape)."""
    h, d = home(intent_started=False), fakes({f'claude-r2d2|{FABLE}': 'stuck'})
    return h, d, make(h, d)


def stuck_session_escalates_after_three_faults_over_thirty_minutes():
    """(d) Faults at 0, 5 and 10 minutes change nothing; the fault at 35 minutes (count 4, span 35 min) escalates:
    the walk continues to the other Fable account, one `engine fault escalated` line, the move persisted, the
    fault episode open with a probe one hour out."""
    h, d, sup = _stuck_r2d2()
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
    check(engine_file(h) == {'acc': 'claude-l', 'model': FABLE}, f'the move is persisted: {engine_file(h)}')
    rt = S.read_engine_runtime(h)
    check(rt.get('active') and rt.get('configured') == {'acc': 'claude-r2d2', 'model': FABLE} and rt.get('effective') == {'acc': 'claude-l', 'model': FABLE}, f'fallback episode: {rt}')
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


def escalated_episode_returns_at_the_hourly_probe():
    """(e) Inside the hour the faulted step is never called; at the hour the probe serves on the configured pair:
    fallback ended, engine file restored, fault episode closed."""
    h, d, sup = stuck_session_escalates_after_three_faults_over_thirty_minutes()
    n = len(calls(d))
    turn(sup, h, 40)
    turn(sup, h, 80)
    check(invoked(d)[n:] == [('claude-l', FABLE), ('claude-l', FABLE)], f'inside the hour only the effective pair runs: {invoked(d)[n:]}')
    set_table(d, {})  # the session fault clears
    n = len(calls(d))
    eid = turn(sup, h, 96)
    check(invoked(d)[n:] == [('claude-r2d2', FABLE)], f'at the hour the faulted step is probed first and serves: {invoked(d)[n:]}')
    check(claude_calls(d)[-1] == ('claude-r2d2', 'resume'), 'the probe resumes the existing session')
    check(eid in S.handled_ids(h) and engine_file(h) == {'acc': 'claude-r2d2', 'model': FABLE}, f'the return lands in the engine file: {engine_file(h)}')
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
    h, d, sup = _stuck_r2d2()
    turn(sup, h, 0)
    turn(sup, h, 29)
    check(not status_lines(h, 'fault episode:'), 'nothing before thirty minutes')
    sup.add_timer_event(now=time.time(), text='timer: daily digest', digest=True)
    ev = [e for e in S.pending_events(h) if (e.get('payload') or {}).get('digest')]
    check(ev and 'fault episode' not in (ev[-1]['payload'].get('text') or ''), f'digest event without a short episode: {ev[-1]["payload"] if ev else None}')
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


def explicit_selection_closes_an_escalated_episode():
    """(j) An operator's explicit engine selection during an escalated episode closes it: the next turn runs the
    selected pair and the faulted step is not probed."""
    h, d, sup = stuck_session_escalates_after_three_faults_over_thirty_minutes()
    S.set_engine(h, 'claude-l', FABLE, engines(d))
    check(fault(h).get('episode_id') is None, f'the explicit selection closes the fault episode: {fault(h)}')
    n = len(calls(d))
    turn(sup, h, 100)
    check(invoked(d)[n:] == [('claude-l', FABLE)], f'the selected pair runs, no probe: {invoked(d)[n:]}')
    no_downgrade(h)


SCENARIOS = [incident_2026_10_15_timeout_then_resume, incident_2026_10_14_in_use_retries_resume_in_the_same_turn,
             resume_refusal_is_a_fault_without_a_second_retry, stuck_session_escalates_after_three_faults_over_thirty_minutes,
             escalated_episode_returns_at_the_hourly_probe, a_faulting_probe_keeps_the_effective_pair,
             escalation_walks_the_ladder_never_below_the_floor, long_episode_shows_in_status_and_the_digest_event,
             below_the_threshold_never_escalates, explicit_selection_closes_an_escalated_episode]


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
