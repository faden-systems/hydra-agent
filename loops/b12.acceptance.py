#!/usr/bin/env python3
"""Exit-owned offline b12 contracts (loops/b12.md). A temporary home, recording fake engines whose behaviour is
chosen per (account, model) by a table file, fake posters only: no real engine, GitHub or Slack. Every check raises
(no `assert` in the contracts, so `python -O` cannot skip one). `--only <name>` runs one scenario. Every scenario
ends with the no-downgrade check: `logs/attempts.jsonl` never carries a model outside the ladder in force."""
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'manager'))
import supervisor as S  # noqa: E402
import bridge as B  # noqa: E402

HYDRA = str(ROOT / 'manager' / 'hydra')
FABLE, ASTRA, OPUS = 'claude-fable-5-1', 'gpt-6-astra', 'claude-opus-5-5'
LADDER_MODELS = {FABLE, ASTRA, OPUS}
BELOW = ('claude-sonnet-5', 'claude-haiku-4-5-20251001', 'claude-opus-5', 'gpt-5.6-sol', 'gpt-6.1-sol')


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


# ---------------------------------------------------------------------------------------------- fixtures
def home(engine=None, config=None):
    h = tempfile.mkdtemp(prefix='b12-home-')
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
    Path(h, 'session-id').write_text('sess-test\n')
    cfg = {'dev_channel': 'C_DEV'}
    cfg.update(config or {})
    Path(h, 'config.json').write_text(json.dumps(cfg))
    return h


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import sys, os, json
from pathlib import Path
D = Path(%(dir)r)
argv = sys.argv[1:]
acc = {'fake-claude-r2d2': 'claude-r2d2', 'fake-claude-l': 'claude-l'}.get(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''), '?')
model = argv[argv.index('--model') + 1] if '--model' in argv else ''
sid = argv[argv.index('--session-id') + 1] if '--session-id' in argv else (argv[argv.index('--resume') + 1] if '--resume' in argv else '')
PERSISTENT = '--input-format' in argv
table = json.loads((D / 'table.json').read_text()) if (D / 'table.json').exists() else {}
behaviour = table.get(acc + '|' + model, 'ok')
ERRORS = {'credits': "You're out of usage credits. Switch to another model to continue.",
          'usage_limit': 'usage limit reached for this account', 'auth': 'Failed to authenticate. API Error: 401 Invalid bearer token',
          'prompt_too_long': 'Prompt is too long for this model', 'rate_limit': 'Rate limit exceeded', 'other': 'a plain runtime crash'}
REPLY = 'REPLY: ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none'


def record(msg):
    with open(D / 'calls.jsonl', 'a') as f:
        f.write(json.dumps({'acc': acc, 'model': model, 'argv': argv, 'stdin': msg, 'persistent': PERSISTENT,
                            'behaviour': behaviour, 'pid': os.getpid()}) + '\n')


if not PERSISTENT:
    msg = sys.stdin.read() if not sys.stdin.isatty() else ''
    record(msg)
    if behaviour != 'ok':
        print(ERRORS[behaviour], file=sys.stderr); sys.exit(1)
    print(REPLY)
else:
    record('<persistent start>')
    if behaviour != 'ok':
        print(ERRORS[behaviour], file=sys.stderr); sys.exit(1)
    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': sid}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        env = json.loads(line)
        content = env.get('message', {}).get('content')
        msg = content if isinstance(content, str) else ''.join(c.get('text', '') for c in content or [])
        record(msg)
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
    f.write(json.dumps({'acc': 'codex', 'model': model, 'argv': argv, 'stdin': msg, 'behaviour': behaviour}) + '\n')
if behaviour != 'ok':
    print({'usage_limit': 'usage limit reached', 'credits': 'out of credits', 'auth': 'HTTP 401 Unauthorized',
           'rate_limit': '429 Too Many Requests', 'other': 'codex exploded'}[behaviour], file=sys.stderr)
    sys.exit(1)
out = argv[argv.index('-o') + 1]
Path(out).write_text('REPLY: codex ok\n---HANDOFF---\ntracks: t1\nwaiting on: nobody\nlast decision: none\nnext action: none\nopen question: none\n')
print('codex done')
'''


def fakes(table=None):
    d = Path(tempfile.mkdtemp(prefix='b12-engine-'))
    (d / 'table.json').write_text(json.dumps(table or {}))
    claude = d / 'claude'
    claude.write_text(FAKE_CLAUDE % {'dir': str(d)})
    claude.chmod(0o755)
    codex = d / 'codex'
    codex.write_text(FAKE_CODEX % {'dir': str(d)})
    codex.chmod(0o755)
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
    """(account, model) of every engine process start, in order (a persistent child start counts once)."""
    return [(c['acc'], c['model']) for c in calls(d) if c.get('stdin') != '' or not c.get('persistent')]


class Poster:
    def __init__(self):
        self.posted = []

    def __call__(self, channel, thread_ts, text):
        self.posted.append((channel, thread_ts, text))

    @property
    def texts(self):
        return ' '.join(t for _, _, t in self.posted)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        self.now = self.now + dt.timedelta(seconds=1)
        return self.now


def make(h, d, poster=None, blog=None):
    return S.Supervisor(home=h, engines=engines(d), poster=poster or Poster(), buildlog_poster=blog or Poster(),
                        clock=Clock(dt.datetime(2026, 10, 11, 19, 0, 0, tzinfo=dt.timezone.utc)))


def event(h, text, thread='1.0', ts=None, instructs=True):
    ev = {'id': ts or f'ev-{time.time_ns()}', 'source': 'slack', 'at': time.time(),
          'payload': {'channel': 'C_DEV', 'thread_ts': thread, 'user': 'U_FOUNDER', 'text': text, 'instructs': instructs}}
    S.append_event(h, ev)
    return ev['id']


def attempts(h):
    return S.read_jsonl(os.path.join(h, 'logs', 'attempts.jsonl'))


def engine_file(h):
    return json.loads(Path(h, 'engine').read_text())


def status(h):
    return S.status_text(h)


def policy_line(h):
    lines = [l for l in status(h).splitlines() if l.startswith('engine policy:')]
    check(len(lines) == 1, f'status must carry exactly one engine policy line: {status(h)!r}')
    return lines[0]


def no_downgrade(h, allowed=LADDER_MODELS):
    bad = [a for a in attempts(h) if a.get('model') not in allowed]
    check(not bad, f'attempts.jsonl carries a model outside the ladder: {bad}')
    for a in attempts(h):
        check(a.get('model') not in BELOW, f'a model below the floor was invoked: {a}')


def unavailable_lines(h):
    return [it for it in S.read_outbox(h) if 'all allowed engines failed' in (it.get('text') or '')]


def hydra(h, *args, env=None, stdin=None):
    e = {**os.environ, 'HYDRA_HOME': h}
    e.update(env or {})
    r = subprocess.run([sys.executable, HYDRA, *args], env=e, capture_output=True, text=True, timeout=120, input=stdin)
    return r.returncode, r.stdout, r.stderr


def make_bridge(h, poster):
    return B.Bridge(home=h, allowlist={'U_FOUNDER': {'instructs': True}, 'U_OPERATOR': {'instructs': False}},
                    poster=poster, token_env={}, bot_user_id='U_MANAGER', downloader=lambda url, dest: None)


def slack(bridge, text, ts, user='U_FOUNDER'):
    bridge.handle_message({'channel': 'C_DEV', 'user': user, 'text': text, 'ts': ts})


# ---------------------------------------------------------------------------------------------- scenarios
def fable_out_on_both_accounts_lands_on_astra():
    """Requirement 1 (a): tier 1 exhausted on r2d2 then l -> tier 2 codex/astra; the switch persisted; the notice
    names both tiers (requirement 4); nothing below the floor invoked."""
    h = home()
    d = fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA)], f'ladder order: {invoked(d)}')
    att = [(a['engine'], a['model'], a['classification'], a['success']) for a in attempts(h)]
    check(att == [('claude-r2d2', FABLE, 'credits', False), ('claude-l', FABLE, 'credits', False), ('codex', ASTRA, None, True)],
          f'attempts must record the three ladder steps: {att}')
    check(engine_file(h) == {'acc': 'codex', 'model': ASTRA}, f'the credits fallback persists the winning step: {engine_file(h)}')
    notices = [it['text'] for it in S.read_outbox(h) if 'fallback started' in it.get('text', '')]
    check(len(notices) == 1, f'exactly one fallback-started notice: {notices}')
    check('tier 1/3 claude-r2d2 (claude-fable-5-1)' in notices[0] and 'tier 2/3 codex (gpt-6-astra)' in notices[0],
          f'the notice must name the configured and the effective tiers: {notices[0]!r}')
    check('active tier 2/3 codex (gpt-6-astra)' in policy_line(h), f'status after the fallback: {policy_line(h)!r}')
    no_downgrade(h)


def astra_out_lands_on_opus55():
    """Requirement 1 (b): tier 1 and tier 2 exhausted -> tier 3 r2d2/opus5.5 (the floor), never Sonnet."""
    h = home()
    d = fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits', f'codex|{ASTRA}': 'usage_limit'})
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA), ('claude-r2d2', OPUS)], f'ladder order: {invoked(d)}')
    att = attempts(h)
    check(len(att) == 4 and att[-1]['success'] is True and att[-1]['model'] == OPUS and att[-1]['engine'] == 'claude-r2d2',
          f'four attempts ending on opus5.5/r2d2: {att}')
    check(engine_file(h) == {'acc': 'claude-r2d2', 'model': OPUS}, f'persisted floor pair: {engine_file(h)}')
    check('active tier 3/3 claude-r2d2 (claude-opus-5-5)' in policy_line(h) and 'floor claude-opus-5-5' in policy_line(h),
          f'status at the floor: {policy_line(h)!r}')
    no_downgrade(h)
    # recovery upward: tier 1 working again closes the episode and the next turn is back on fable/r2d2
    set_table(d, {})
    event(h, 'B')
    check(sup.run_once() is True, 'turn B did not run')
    check(invoked(d)[-1] == ('claude-r2d2', FABLE), f'the rotation from the floor must reach tier 1 again: {invoked(d)}')
    ended = [it['text'] for it in S.read_outbox(h) if 'fallback ended' in it.get('text', '')]
    check(ended and 'tier 1/3 claude-r2d2 (claude-fable-5-1)' in ended[-1], f'the ended notice names the tier: {ended}')
    no_downgrade(h)


def all_out_posts_one_unavailable_line():
    """Requirement 3: every step fails -> one top-level line naming every step, the normal back-off, no downgrade,
    no second line inside the episode, the episode closed by the next served turn."""
    # Replace only the supervisor's wall clock; subprocess and heartbeat scheduling keep real time.
    original_time = S.time
    wall = [1791745200.0]
    class WallClock:
        def time(self):
            return wall[0]
        def __getattr__(self, name):
            return getattr(original_time, name)
    S.time = WallClock()
    try:
        _all_out_with_clock(wall)
    finally:
        S.time = original_time


def _all_out_with_clock(wall):
    h = home()
    retry = Path(h, 'logs/retry-after')
    def expire_retry():
        deadline = float(retry.read_text())
        check(deadline == wall[0] + S.RETRY_AFTER_S,
              f'retry deadline must be failure time plus RETRY_AFTER_S: {deadline}, {wall[0]}')
        wall[0] = deadline - 1
        count = len(calls(d))
        check(sup.run_once() is False and len(calls(d)) == count,
              'the tick immediately before the deadline must not invoke an engine')
        wall[0] = deadline + 1
        check(retry.exists(), 'the expired deadline stays on disk until the supervisor handles it')
    table = {f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits', f'codex|{ASTRA}': 'usage_limit',
             f'claude-r2d2|{OPUS}': 'credits', f'claude-l|{OPUS}': 'credits'}
    d = fakes(table)
    poster = Poster()
    sup = make(h, d, poster=poster)
    eid = event(h, 'A')
    check(sup.run_once() is True, 'the failing turn must run')
    check(invoked(d) == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA), ('claude-r2d2', OPUS), ('claude-l', OPUS)],
          f'all five ladder steps tried, in order: {invoked(d)}')
    lines = unavailable_lines(h)
    check(len(lines) == 1, f'exactly one unavailable line in the outbox: {[l.get("text") for l in lines]}')
    text = lines[0]['text']
    check(text.startswith('manager unavailable: all allowed engines failed ('), f'wording: {text!r}')
    check(lines[0].get('thread_ts') is None and lines[0].get('channel') == 'C_DEV', f'the line is top level in the dev channel: {lines[0]}')
    expected = '; '.join((
        f'tier 1 claude-r2d2 ({FABLE}): credits', f'tier 1 claude-l ({FABLE}): credits',
        f'tier 2 codex ({ASTRA}): usage_limit', f'tier 3 claude-r2d2 ({OPUS}): credits',
        f'tier 3 claude-l ({OPUS}): credits'))
    check(text == f'manager unavailable: all allowed engines failed ({expected})',
          f'exact ordered reasons and separators: {text!r}')
    check(not any('unavailable' in t for _, _, t in poster.posted), f'no per-thread unavailable note any more: {poster.posted}')
    check(eid not in S.handled_ids(h), 'the event stays pending for the retry')
    check(os.path.exists(os.path.join(h, 'logs', 'retry-after')), 'retry-after must be written')
    ep = json.loads(Path(h, 'logs', 'unavailable.json').read_text())
    check(ep.get('episode_id') and lines[0].get('id') == f"unavailable-{ep['episode_id']}", f'the notice id is bound to the episode: {ep} {lines[0].get("id")}')
    n = len(calls(d))
    check(sup.run_once() is False and len(calls(d)) == n, 'a tick inside the back-off makes no engine call')
    expire_retry()
    check(sup.run_once() is True, 'the retry after the back-off must run')
    check(len(calls(d)) == 2 * n, f'the retry tries the whole ladder again: {len(calls(d))} calls')
    check(len(unavailable_lines(h)) == 1, 'a failing retry inside the episode posts no second line')
    no_downgrade(h)
    # a fresh supervisor (restart) inside the same episode queues nothing new either
    expire_retry()
    sup2 = make(h, d, poster=poster)
    check(sup2.run_once() is True, 'the restarted retry must run')
    check(len(unavailable_lines(h)) == 1, 'a restart inside the episode must not duplicate the line')
    # recovery closes the episode; no "available again" line
    set_table(d, {})
    expire_retry()
    check(sup2.run_once() is True, 'the recovering turn must run')
    check(eid in S.handled_ids(h), 'the pending event is served by the recovering turn')
    ep = json.loads(Path(h, 'logs', 'unavailable.json').read_text())
    check(ep.get('episode_id') is None, f'the episode must close on the served turn: {ep}')
    check(len(unavailable_lines(h)) == 1 and not any('available again' in (it.get('text') or '') for it in S.read_outbox(h)),
          'no extra line on recovery')
    # a new episode later gets its own line
    set_table(d, table)
    event(h, 'C')
    check(sup2.run_once() is True, 'the second failing turn must run')
    check(len(unavailable_lines(h)) == 2, 'a new episode posts a new line')
    no_downgrade(h)


def unavailable_notice_survives_crash_windows():
    """Crash at the durable episode/queue boundary and after enqueue; receipt dedup survives restart."""
    class SimulatedCrash(BaseException):
        pass
    table = {f'{account}|{model}': 'credits' for account, model in [
        ('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA),
        ('claude-r2d2', OPUS), ('claude-l', OPUS)]}
    for window in ('before-queue', 'after-queue', 'after-receipt'):
        h, d, poster = home(), fakes(table), Poster()
        sup = make(h, d)
        eid = event(h, f'crash window {window}')
        original = S._queue_notice_once
        reached = []
        def crash_queue(home_arg, channel, text, note_id):
            if not note_id.startswith('unavailable-'):
                return original(home_arg, channel, text, note_id)
            ep = json.loads(Path(h, 'logs/unavailable.json').read_text())
            check(note_id == f"unavailable-{ep['episode_id']}" and ep.get('reasons'),
                  'episode and reasons must be durable before enqueue')
            reached.append(note_id)
            if window != 'before-queue':
                original(home_arg, channel, text, note_id)
            if window == 'after-receipt':
                S.drain_outbox(h, poster)
                check(S.post_receipt(h, note_id) is not None, 'delivery must have a durable receipt')
            raise SimulatedCrash()
        S._queue_notice_once = crash_queue
        try:
            try:
                sup.run_once()
            except SimulatedCrash:
                pass
        finally:
            S._queue_notice_once = original
        check(len(reached) == 1, f'crash seam was not reached exactly once: {window} {reached}')
        check(len(unavailable_lines(h)) == (1 if window == 'after-queue' else 0),
              f'unexpected pending notice at crash boundary {window}')
        retry = Path(h, 'logs/retry-after')
        if retry.exists():
            retry.unlink()
        if window == 'before-queue':
            set_table(d, {})  # Recovery must not erase the durable, not-yet-enqueued notice.
        restarted = make(h, d)
        check(restarted.run_once() is True, f'restart must retry pending event: {window}')
        S.drain_outbox(h, poster)
        notices = [(c, t, text) for c, t, text in poster.posted if 'all allowed engines failed' in text]
        check(len(notices) == 1 and notices[0][0:2] == ('C_DEV', None),
              f'exactly one eventual top-level delivery across {window}: {notices}')
        check(S.post_receipt(h, reached[0]) is not None, 'original episode ID must be receipted')
        if retry.exists():
            retry.unlink()
        if window == 'before-queue':
            check(eid in S.handled_ids(h), 'the first restart must serve the pending event')
            ep = json.loads(Path(h, 'logs/unavailable.json').read_text())
            check(ep.get('episode_id') is None, 'immediate recovery must close the unavailable episode')
            check(make(h, d).run_once() is False, 'the served event must not replay on another restart')
        else:
            check(make(h, d).run_once() is True, 'second restarted retry must run')
        S.drain_outbox(h, poster)
        check(len([text for _, _, text in poster.posted if 'all allowed engines failed' in text]) == 1,
              f'a receipted episode must not repost: {window}')
        no_downgrade(h)


def commands_refuse_outside_the_allowlist():
    """Requirement 2 (d) and 5: `hydra engine` and `@manager engine` refuse models outside the ladder with one line
    and change nothing; ladder pairs, the legacy form and a bare mode change are accepted."""
    h = home(engine={'acc': 'claude-r2d2', 'model': FABLE})
    before = Path(h, 'engine').read_bytes()
    runtime_path = Path(h, 'engine-runtime.json')
    runtime_path.write_text(json.dumps({'configured': {'acc': 'claude-r2d2', 'model': FABLE},
        'effective': {'acc': 'codex', 'model': ASTRA}, 'episode_id': 'b12-preserve',
        'active': True, 'since': '2026-10-11T18:00:00Z', 'reason': 'credits'}) + '\n')
    runtime_before = runtime_path.read_bytes()
    for args in (('acc=claude-l', 'model=claude-sonnet-5'), ('model=opus5',), ('acc=codex', 'model=gpt6.1'),
                 ('acc=codex', 'model=gpt-5.6-sol'), ('acc=claude-l', 'model=claude-haiku-4-5-20251001')):
        rc, out, err = hydra(h, 'engine', *args)
        check(rc == 2, f'hydra engine {args} must exit 2, got {rc} {out!r} {err!r}')
        check("not on the manager's allowlist" in err and 'floor claude-opus-5-5' in err and 'nothing changed' in err,
              f'the refusal is one line with the floor: {err!r}')
        check(len([l for l in err.strip().splitlines() if l.strip()]) == 1, f'exactly one line: {err!r}')
        check(Path(h, 'engine').read_bytes() == before, f'engine file changed by a refused command {args}')
        check(runtime_path.read_bytes() == runtime_before, f'runtime file changed by a refused command {args}')
    rc, out, err = hydra(h, 'engine', 'model=sonnet5')
    check(rc == 2 and 'unknown model' in err and 'sonnet5 ->' not in err and 'fable5.1' in err, f'sonnet5 is no alias any more: {err!r}')
    check(Path(h, 'engine').read_bytes() == before, 'engine file changed by an unknown alias')
    check(runtime_path.read_bytes() == runtime_before, 'runtime file changed by an unknown alias')
    rc, out, err = hydra(h, 'engine', 'acc=claude-l', 'model=fable5.1')
    check(rc == 0 and out.strip() == 'engine: claude-l (claude-fable-5-1)', f'a ladder pair is accepted: {rc} {out!r} {err!r}')
    rc, out, err = hydra(h, 'engine', 'acc=codex')
    check(rc == 0 and out.strip() == 'engine: codex (gpt-6-astra)', f'the family default on the ladder is accepted: {out!r} {err!r}')
    rc, out, err = hydra(h, 'engine', 'acc=claude-r2d2', 'model=opus5.5')
    check(rc == 0 and engine_file(h) == {'acc': 'claude-r2d2', 'model': OPUS}, f'the floor pair is accepted: {out!r} {err!r}')
    rc, out, err = hydra(h, 'engine', 'mode=persistent')
    check(rc == 0 and engine_file(h) == {'acc': 'claude-r2d2', 'model': OPUS, 'mode': 'persistent'}, f'a bare mode change on a ladder pair: {out!r} {err!r}')
    rc, out, err = hydra(h, 'engine', 'claude-l')
    check(rc == 0 and engine_file(h)['model'] == FABLE and engine_file(h)['acc'] == 'claude-l', f'the legacy form: {out!r} {err!r}')
    rc, out, err = hydra(h, 'engine')
    check(rc == 0 and out.strip().startswith('engine: claude-l (claude-fable-5-1)'), f'`hydra engine` alone prints the pair: {out!r}')
    # Slack
    poster = Poster()
    br = make_bridge(h, poster)
    before = Path(h, 'engine').read_bytes()
    runtime_path.write_bytes(runtime_before)
    slack(br, '<@U_MANAGER> engine acc=claude-l model=claude-sonnet-5', '20.0')
    reply = poster.posted[-1][2]
    check("not on the manager's allowlist" in reply and 'nothing changed' in reply and '\n' not in reply.strip(), f'Slack refusal: {reply!r}')
    check(Path(h, 'engine').read_bytes() == before, 'engine file changed by a refused Slack command')
    check(runtime_path.read_bytes() == runtime_before, 'runtime file changed by a refused Slack command')
    slack(br, '<@U_MANAGER> engine acc=codex model=gpt6.1', '20.1')
    check("not on the manager's allowlist" in poster.posted[-1][2], f'a coder alias is refused in Slack: {poster.posted[-1][2]!r}')
    check(Path(h, 'engine').read_bytes() == before and runtime_path.read_bytes() == runtime_before,
          'both files must remain byte-identical after the refused coder alias in Slack')
    slack(br, '<@U_MANAGER> engine acc=claude-r2d2 model=fable5.1', '20.2')
    check(poster.posted[-1][2] == 'engine: claude-r2d2 (claude-fable-5-1)', f'an allowed Slack command: {poster.posted[-1][2]!r}')
    slack(br, '<@U_MANAGER> engine acc=claude-l model=claude-sonnet-5', '20.3', user='U_OPERATOR')
    check('not authorized' in poster.posted[-1][2] and engine_file(h)['acc'] == 'claude-r2d2', 'an operator is still refused first')
    no_downgrade(h)


def stale_engine_file_never_runs_below_the_floor():
    """Requirement 1 (e) and 4: a pre-b12 engine file naming Sonnet is never invoked; the turn starts at the first
    ladder step on that account; status says so before the turn."""
    h = home(engine={'acc': 'claude-l', 'model': 'claude-sonnet-5'})
    d = fakes()
    line = policy_line(h)
    check('active tier -/3' in line and 'outside the ladder' in line and 'the next turn starts at tier 1' in line, f'status before the turn: {line!r}')
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('claude-l', FABLE)], f'the first ladder step on the configured account runs: {invoked(d)}')
    check(all(c['model'] != 'claude-sonnet-5' for c in calls(d)), 'sonnet must never be invoked')
    no_downgrade(h)


def status_line_contract():
    """Requirement 4 (f): the policy line for the default ladder, a configured ladder, and invalid configs."""
    h = home()
    line = policy_line(h)
    check(line.startswith('engine policy: floor claude-opus-5-5; active tier 1/3 claude-r2d2 (claude-fable-5-1); ladder '), f'default: {line!r}')
    check('claude-fable-5-1 [claude-r2d2, claude-l] > gpt-6-astra [codex] > claude-opus-5-5 [claude-r2d2, claude-l]' in line, f'ladder text: {line!r}')
    check(len(line) <= 400, f'bounded: {len(line)}')
    lines = status(h).splitlines()
    check(lines.index(line) == lines.index(next(l for l in lines if l.startswith('engine: '))) + 1, 'right after the engine line')
    h2 = home(engine={'acc': 'codex', 'model': ASTRA},
              config={'engine_policy': {'ladder': [{'model': 'astra', 'accounts': ['codex']}, {'model': 'opus5.5', 'accounts': ['claude-l']}]}})
    line2 = policy_line(h2)
    check(line2.startswith('engine policy: floor claude-opus-5-5; active tier 1/2 codex (gpt-6-astra); ladder gpt-6-astra [codex] > claude-opus-5-5 [claude-l]'), f'configured ladder: {line2!r}')
    for bad, needle in (({'ladder': [{'model': FABLE, 'accounts': ['nope']}]}, 'nope'),
                        ({'ladder': [{'model': ASTRA, 'accounts': ['claude-l']}]}, 'family'),
                        ({'ladder': [{'model': 'claude-sonnet-5', 'accounts': ['claude-l']}]}, 'claude-sonnet-5'),
                        ({'ladder': [{'model': 'gpt-6.1-sol', 'accounts': ['codex']}]}, 'gpt-6.1-sol'),
                        ({'ladder': []}, 'empty'),
                        ({'ladder': 'fable5.1'}, 'list')):
        h3 = home(config={'engine_policy': bad})
        line3 = policy_line(h3)
        check('engine_policy ignored' in line3 and 'floor claude-opus-5-5; active tier 1/3' in line3, f'invalid config {bad} -> default ladder with the reason: {line3!r}')
        check(needle in line3, f'the reason names the problem ({needle!r}): {line3!r}')
    # Repeating a pair is invalid, not a valid way to reach the display limit.
    repeated = [{'model': FABLE, 'accounts': ['claude-r2d2']}] * 2
    duplicate = policy_line(home(config={'engine_policy': {'ladder': repeated}}))
    check('engine_policy ignored' in duplicate and 'floor claude-opus-5-5' in duplicate,
          f'a repeated account/model pair rejects the entire override: {duplicate!r}')
    # Distinct known custom engines give a valid long ladder without repeating pairs.
    names = [f'claude-control-account-{i:02d}' for i in range(12)]
    many = [{'model': FABLE, 'accounts': names}, {'model': OPUS, 'accounts': ['claude-r2d2']}]
    h4 = home(config={'engine_policy': {'ladder': many},
                      'engines': {name: {'bin': '/unused/fake-claude'} for name in names}})
    check('engine_policy ignored' not in policy_line(h4), 'the truncation fixture must be a valid ladder')
    check(len(policy_line(h4)) <= 400 and policy_line(h4).endswith('...'), f'bounded with an ellipsis: {len(policy_line(h4))} {policy_line(h4)[-20:]!r}')


    # Each non-ladder field independently exceeds the entire line budget.
    long_acc = 'claude-' + '界🙂' * 240
    custom = {'engine_policy': {'ladder': [{'model': FABLE, 'accounts': [long_acc]},
                {'model': OPUS, 'accounts': ['claude-r2d2']}]},
              'engines': {long_acc: {'bin': '/unused/fake-claude'}}}
    active = policy_line(home(engine={'acc': long_acc, 'model': FABLE}, config=custom))
    check('active tier 1/2 ' + long_acc[:37] + '... (' + FABLE + ')' in active,
          f'active account cap preserves tier and model: {active!r}')
    check(len(active) <= 400 and 'engine_policy ignored' not in active, f'long valid account: {active!r}')
    stale_pair = {'acc': long_acc, 'model': 'obsolete-' + '界🙂' * 240}
    stale = policy_line(home(engine=stale_pair))
    label = S.engine_label(stale_pair)
    check('(configured ' + label[:57] + '... is outside the ladder; the next turn starts at tier 1)' in stale,
          f'stale label cap preserves recovery text: {stale!r}')
    check(len(stale) <= 400, f'long stale label: {stale!r}')
    bad_home = home(config={'engine_policy': {'ladder': [{'model': FABLE, 'accounts': [long_acc]}]}})
    ignored = policy_line(bad_home)
    marker = ' (engine_policy ignored: '
    check(marker in ignored and ignored.endswith('...)'), f'diagnostic cap preserves suffix: {ignored!r}')
    reason = ignored.split(marker, 1)[1][:-1]
    check(len(reason) == 80 and long_acc[:20] in reason, f'diagnostic retains first problem in 80 code points: {reason!r}')
    check(len(ignored) <= 400 and 'floor ' + OPUS in ignored and 'active tier 1/3' in ignored,
          f'long ignored-policy reason: {ignored!r}')


def ladder_rotation_from_a_selected_lower_tier():
    """Requirement 1 (g): a deliberately selected lower tier falls back downward first, then wraps to the top."""
    h = home(engine={'acc': 'codex', 'model': ASTRA})
    d = fakes({f'codex|{ASTRA}': 'usage_limit'})
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('codex', ASTRA), ('claude-r2d2', OPUS)], f'from tier 2 the next step is tier 3: {invoked(d)}')
    no_downgrade(h)
    h2 = home(engine={'acc': 'claude-l', 'model': OPUS})
    d2 = fakes({f'claude-l|{OPUS}': 'credits', f'claude-r2d2|{OPUS}': 'credits'})
    sup2 = make(h2, d2)
    event(h2, 'A')
    check(sup2.run_once() is True, 'turn A did not run')
    expected = [('claude-l', OPUS), ('claude-r2d2', OPUS), ('claude-r2d2', FABLE)]
    check(invoked(d2) == expected, f'finish every account in the selected tier before wrapping upward: {invoked(d2)}')
    check([(a['engine'], a['model']) for a in attempts(h2)] == expected,
          f'all three rotation attempts must be recorded: {attempts(h2)}')
    no_downgrade(h2)


def prompt_too_long_skips_the_claude_family():
    """Requirement 1 (h), a preservation contract (it passes on the base too): prompt_too_long on tier 1 skips the
    remaining Claude steps (tier 3 included) and lands on codex; the emergency pending is set as today (the account
    is working: a prior successful turn)."""
    h = home()
    d = fakes()
    sup = make(h, d)
    event(h, 'warm-up')
    check(sup.run_once() is True, 'warm-up did not run')
    set_table(d, {f'claude-r2d2|{FABLE}': 'prompt_too_long'})
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d)[1:] == [('claude-r2d2', FABLE), ('codex', ASTRA)], f'claude-l and tier 3 skipped: {invoked(d)}')
    check(S.read_compaction_state(h).get('emergency_pending'), 'the emergency pending is set as today')
    no_downgrade(h)


def legacy_claude_models_config_is_ignored():
    """Requirement 1 (i): `engine_fallback.claude_models` is no longer a retry list."""
    h = home(config={'engine_fallback': {'claude_models': ['claude-sonnet-5']}})
    d = fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA)], f'no same-account retry: {invoked(d)}')
    no_downgrade(h)


def _policy_diagnostics_worker():
    # A fresh process prevents other scenarios' once-per-process warnings from consuming this check.
    captured = []
    original = S.log
    S.log = lambda message: captured.append(str(message))
    try:
        cfg = {'engine_fallback': {'claude_models': ['claude-sonnet-5']},
               'engine_policy': {'ladder': [
                   {'model': 'claude-sonnet-5', 'accounts': ['claude-r2d2']},
                   {'model': FABLE, 'accounts': ['unknown-second-account']}]}}
        h, d = home(config=cfg), fakes()
        for instance in range(2):
            sup = make(h, d)
            for turn in range(2):
                event(h, f'diagnostic {instance}/{turn}')
                check(sup.run_once() is True, 'diagnostic turn did not run')
                status(h)
        legacy = [line for line in captured if 'engine_fallback.claude_models' in line and 'ignor' in line]
        invalid = [line for line in captured if 'engine_policy ignored:' in line]
        check(len(legacy) == 1, f'exactly one legacy warning across turns and instances: {legacy}')
        check(len(invalid) == 1 and 'claude-sonnet-5' in invalid[0]
              and 'unknown-second-account' not in invalid[0] and 'default ladder is in force' in invalid[0],
              f'exactly one invalid-policy warning naming the first problem: {invalid}')
        check(invoked(d) == [('claude-r2d2', FABLE)] * 4, f'invalid policy must use default: {invoked(d)}')
        no_downgrade(h)
    finally:
        S.log = original


def policy_diagnostics_once_per_process():
    code = "import runpy; n=runpy.run_path(%r); n['_policy_diagnostics_worker']()" % str(Path(__file__).resolve())
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=120)
    check(result.returncode == 0, f'isolated diagnostic contract: {result.stdout} {result.stderr}')


def persistent_mode_covered():
    """Requirement 6 (j): in persistent mode a tier-1 child whose start fails with a credits error on both accounts
    falls back to per-turn for the turn (b8) and the ladder lands on codex; nothing below the floor."""
    h = home(engine={'acc': 'claude-r2d2', 'model': FABLE, 'mode': 'persistent'})
    d = fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
    sup = make(h, d)
    try:
        event(h, 'A')
        check(sup.run_once() is True, 'turn A did not run')
        recorded = calls(d)
        first = [(c['acc'], c['model'], c.get('persistent', False)) for c in recorded[:3]]
        check(first == [('claude-r2d2', FABLE, True), ('claude-r2d2', FABLE, True),
                        ('claude-r2d2', FABLE, False)],
              f'two failed persistent starts must precede the per-turn retry: {first}')
        check(recorded[0]['stdin'] == recorded[1]['stdin'] == '<persistent start>'
              and recorded[0]['pid'] != recorded[1]['pid'], 'two distinct persistent children must actually start')
        check(recorded[2]['stdin'] and recorded[2]['stdin'] != '<persistent start>',
              'the per-turn retry must receive the manager message')
        models = {c['model'] for c in recorded}
        check(models <= LADDER_MODELS, f'only ladder models started in persistent mode: {models}')
        check(invoked(d)[-1] == ('codex', ASTRA), f'the ladder lands on codex: {invoked(d)}')
        att = attempts(h)
        check(att and att[-1]['success'] is True and att[-1]['engine'] == 'codex', f'codex served the turn: {att}')
        no_downgrade(h)
    finally:
        try:
            sup._retire_persistent('planned', 'acceptance done')
        except Exception:
            pass



def configured_ladder_controls_execution_and_commands():
    """A valid override controls account membership, order and both command surfaces."""
    ladder = [{'model': 'gpt6', 'accounts': ['codex']},
              {'model': 'opus5.5', 'accounts': ['claude-l']}]
    cfg = {'engine_policy': {'ladder': ladder}}
    h = home(engine={'acc': 'codex', 'model': ASTRA}, config=cfg)
    d = fakes({f'codex|{ASTRA}': 'usage_limit'})
    sup = make(h, d)
    event(h, 'custom ladder')
    check(sup.run_once() is True, 'custom-ladder turn did not run')
    expected = [('codex', ASTRA), ('claude-l', OPUS)]
    check(invoked(d) == expected, f'override order/account membership ignored: {invoked(d)}')
    check([(a['engine'], a['model']) for a in attempts(h)] == expected,
          f'custom ladder attempt records disagree: {attempts(h)}')
    no_downgrade(h, {ASTRA, OPUS})
    # Same approved model, excluded account; also an approved model excluded entirely.
    for acc, model in [('claude-r2d2', OPUS), ('claude-l', FABLE)]:
        before = Path(h, 'engine').read_bytes()
        runtime = Path(h, 'engine-runtime.json').read_bytes()
        rc, out, err = hydra(h, 'engine', f'acc={acc}', f'model={model}')
        check(rc == 2 and "not on the manager's allowlist" in err, f'custom console refusal: {rc} {out} {err}')
        check(Path(h, 'engine').read_bytes() == before and Path(h, 'engine-runtime.json').read_bytes() == runtime,
              'custom console refusal changed configuration/runtime')
        poster = Poster()
        br = make_bridge(h, poster)
        slack(br, f'<@U_MANAGER> engine acc={acc} model={model}', str(time.time_ns()))
        check(poster.posted and "not on the manager's allowlist" in poster.posted[-1][2],
              f'custom Slack refusal: {poster.posted}')
        check(Path(h, 'engine').read_bytes() == before and Path(h, 'engine-runtime.json').read_bytes() == runtime,
              'custom Slack refusal changed configuration/runtime')
    rc, out, err = hydra(h, 'engine', 'acc=claude-l', 'model=opus5.5')
    check(rc == 0 and engine_file(h)['acc'] == 'claude-l' and engine_file(h)['model'] == OPUS,
          f'custom console allowed pair: {rc} {out} {err}')
    poster = Poster()
    slack(make_bridge(h, poster), '<@U_MANAGER> engine acc=codex model=gpt6', 'custom-allowed')
    check(poster.posted[-1][2] == f'engine: codex ({ASTRA})' and engine_file(h)['acc'] == 'codex',
          f'custom Slack allowed pair: {poster.posted}')


def documentation_and_help():
    """Requirement 5: published instructions cover the policy and runnable help marks coder aliases."""
    readme = (ROOT / 'manager/README.md').read_text().lower()
    for topic in ('floor', FABLE, ASTRA, OPUS, 'engine_policy', 'ladder', 'engine policy:',
                  'all allowed engines failed', 'gpt6.1', 'sol6.1', 'gpt-6.1-sol'):
        check(topic.lower() in readme, f'README omits policy topic {topic!r}')
    check('coder' in readme and ('refus' in readme or 'not allowed' in readme),
          'README must explain that coder aliases are refused for the manager')
    rc, out, err = hydra(home(), '--help')
    check(rc == 0, f'help command failed: {rc} {err}')
    for alias in ('fable5.1', 'opus5.5', 'gpt6', 'gpt6.1', 'sol6.1'):
        check(alias in out, f'help omits {alias}')
    check('sonnet5' not in out and 'haiku4.5' not in out, 'help advertises removed aliases')
    check('coder' in out.lower() and ('refus' in out.lower() or 'not allowed' in out.lower()),
          'help must identify coder-only aliases and their manager restriction')


def models_json_aliases():
    """Requirement 5 (l): the manager's alias table."""
    models = S.load_models(home())
    cl, cx = models['claude']['aliases'], models['codex']['aliases']
    check('sonnet5' not in cl and 'haiku4.5' not in cl, f'sonnet5/haiku4.5 removed: {cl}')
    check(cl.get('fable5.1') == FABLE and cl.get('opus5.5') == OPUS, f'ladder aliases kept: {cl}')
    check(cx.get('gpt6.1') == 'gpt-6.1-sol' and cx.get('sol6.1') == 'gpt-6.1-sol' and cx.get('gpt6') == ASTRA, f'codex aliases: {cx}')
    check(S.resolve_model('codex', 'gpt6.1', models) == 'gpt-6.1-sol', 'the coder alias resolves for coder use')
    try:
        S.resolve_model('claude', 'sonnet5', models)
    except S.BadEngine as e:
        check('unknown model' in str(e), f'sonnet5 is unknown: {e}')
    else:
        raise Fail('resolve_model must reject sonnet5')
    check(S.family_default_model('claude', models) == FABLE and S.family_default_model('codex', models) == ASTRA, 'family defaults unchanged')
    ladder = S.manager_ladder({}, models)
    check(S.floor_model(ladder) == OPUS, f'the default floor: {S.floor_model(ladder)}')
    check([t['model'] for t in ladder] == [FABLE, ASTRA, OPUS], f'the default ladder order: {ladder}')
    check(S.ladder_position({'acc': 'claude-l', 'model': FABLE}, ladder) == 1 and S.ladder_position({'acc': 'codex', 'model': ASTRA}, ladder) == 2
          and S.ladder_position({'acc': 'claude-l', 'model': 'claude-sonnet-5'}, ladder) is None, 'ladder_position')
    try:
        S.enforce_engine_policy({'acc': 'claude-l', 'model': 'gpt-6.1-sol'}, ladder)
    except S.BadEngine as e:
        check("not on the manager's allowlist (floor claude-opus-5-5)" in str(e) and '\n' not in str(e), f'one-line reason: {e}')
    else:
        raise Fail('enforce_engine_policy must refuse a coder alias')


def attach_refuses_outside_the_allowlist():
    """Requirement 2 (k): `hydra attach` never starts a process on an engine file outside the ladder."""
    d = Path(tempfile.mkdtemp(prefix='b12-attach-'))
    script = d / 'claude'
    script.write_text("#!/usr/bin/env python3\nimport sys, json\n"
                      f"open({str(d / 'calls.jsonl')!r}, 'a').write(json.dumps({{'argv': sys.argv[1:]}}) + '\\n')\n")
    script.chmod(0o755)
    cfg = {'engines': {'claude-r2d2': {'bin': str(script)}, 'claude-l': {'bin': str(script)}}}
    h = home(engine={'acc': 'claude-l', 'model': 'claude-sonnet-5'}, config=cfg)
    rc, out, err = hydra(h, 'attach')
    check(rc == 2 and "not on the manager's allowlist" in (out + err), f'attach must refuse a stale sonnet file: {rc} {out!r} {err!r}')
    check(not (d / 'calls.jsonl').exists(), 'no process started by a refused attach')
    Path(h, 'engine').write_text(json.dumps({'acc': 'claude-l', 'model': OPUS}) + '\n')
    rc, out, err = hydra(h, 'attach')
    check(rc == 0, f'attach on a ladder pair: {rc} {out!r} {err!r}')
    argv = json.loads((d / 'calls.jsonl').read_text().strip().splitlines()[-1])['argv']
    check(argv[argv.index('--model') + 1] == OPUS, f'attach runs the configured ladder model: {argv}')


def unavailable_reason_contract():
    """Exercise the real turn/error/notice path; only engine invocation and eligibility inputs are fake."""
    ladder = [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA),
              ('claude-r2d2', OPUS), ('claude-l', OPUS)]
    labels = [f'tier {tier} {acc} ({model}): ' for tier, (acc, model) in zip((1, 1, 2, 3, 3), ladder)]

    def run(replies, expected, expected_calls, *, skip=None, custom=None):
        cfg = {'engine_fallback': {'claude_models': []}}
        specs = None
        if custom:
            cfg['engine_policy'] = {'ladder': [{'model': ASTRA, 'accounts': custom}]}
            cfg['engines'] = {name: {'kind': 'codex', 'bin': '/unused/fake-codex'} for name in custom}
        h = home(engine={'acc': custom[0], 'model': ASTRA} if custom else None, config=cfg)
        d = fakes()
        if custom:
            specs = {name: {'kind': 'codex', 'bin': str(d / 'codex'), 'cred': None} for name in custom}
        sup = (S.Supervisor(home=h, engines=specs, poster=Poster(), buildlog_poster=Poster(),
                           clock=Clock(dt.datetime(2026, 10, 11, 19, tzinfo=dt.timezone.utc)))
               if custom else make(h, d))
        observed = []
        def invoke(name, spec, message, model, preamble=''):
            observed.append((name, model))
            rc, error = replies[(name, model)]
            return rc, '', error, {}
        sup.invoke = invoke
        original_identity = S.credential_identity
        if skip == 'over budget':
            sup.over_budget = lambda name: name == 'claude-l'
        elif skip in ('unverified credential', 'identity mismatch'):
            def identity(hh, name, spec, observed=None):
                if name == 'claude-l':
                    return {'automatic_allowed': False, 'mismatch': skip == 'identity mismatch'}
                return original_identity(hh, name, spec, observed=observed)
            S.credential_identity = identity
        try:
            event(h, 'failure reason contract')
            check(sup.run_once() is True, 'reason turn did not run')
        finally:
            S.credential_identity = original_identity
        check(observed == expected_calls, f'wrong attempts for reasons: {observed!r}')
        lines = unavailable_lines(h)
        wanted = 'manager unavailable: all allowed engines failed (' + expected[:1000] + ')'
        check(len(lines) == 1 and lines[0]['text'] == wanted,
              f'exact reason contract: expected {wanted!r}, got {lines!r}')
        check(lines[0].get('channel') == 'C_DEV' and lines[0].get('thread_ts') is None,
              'reason notice must be top level')
        # Encoding must round-trip: limits count code points, never slices of UTF-8 bytes.
        check(lines[0]['text'].encode('utf-8').decode('utf-8') == wanted, 'broken Unicode boundary')
        no_downgrade(h)

    error = '界🙂é' * 45 + 'UNTRUNCATED_SENTINEL'
    outcomes = [(1, 'out of usage credits'), (1, 'usage limit reached'),
                (1, 'HTTP 401 Unauthorized'), (1, '429 Too Many Requests'), (1, error)]
    reasons = ['credits', 'usage_limit', 'auth', 'rate_limit', 'other: ' + error[:80]]
    run(dict(zip(ladder, outcomes)), '; '.join(l + r for l, r in zip(labels, reasons)), ladder)
    for why in ('unverified credential', 'over budget', 'identity mismatch'):
        reasons = ['credits', 'skipped: ' + why, 'credits', 'credits', 'skipped: ' + why]
        run({pair: (1, 'out of usage credits') for pair in ladder},
            '; '.join(l + r for l, r in zip(labels, reasons)), [ladder[i] for i in (0, 2, 3)], skip=why)
    reasons = ['prompt_too_long', 'skipped: skipped Claude family', 'auth',
               'skipped: skipped Claude family', 'skipped: skipped Claude family']
    run({ladder[0]: (1, 'Prompt is too long for this model'), ladder[2]: (1, 'HTTP 401 Unauthorized')},
        '; '.join(l + r for l, r in zip(labels, reasons)), [ladder[0], ladder[2]])
    for rc, reason in ((17, 'crash'), (124, 'timeout')):
        # An uncertain attempt terminates traversal immediately; no later tier is falsely called failed.
        run({ladder[0]: (rc, 'unstructured process failure')}, labels[0] + reason, [ladder[0]])
    names = [f'codex{i:02d}' for i in range(12)]
    long_reasons = '; '.join(f'tier 1 {name} ({ASTRA}): other: {error[:80]}' for name in names)
    check(len(long_reasons) > 1000, 'Unicode total-bound fixture must exceed the limit')
    run({(name, ASTRA): (1, error) for name in names}, long_reasons,
        [(name, ASTRA) for name in names], custom=names)


SCENARIOS = [unavailable_reason_contract, fable_out_on_both_accounts_lands_on_astra, astra_out_lands_on_opus55, all_out_posts_one_unavailable_line,
             commands_refuse_outside_the_allowlist, stale_engine_file_never_runs_below_the_floor, status_line_contract,
             ladder_rotation_from_a_selected_lower_tier, prompt_too_long_skips_the_claude_family,
             legacy_claude_models_config_is_ignored, persistent_mode_covered, models_json_aliases,
             attach_refuses_outside_the_allowlist, configured_ladder_controls_execution_and_commands, documentation_and_help, policy_diagnostics_once_per_process, unavailable_notice_survives_crash_windows]


def main(argv):
    only = argv[argv.index('--only') + 1] if '--only' in argv else None
    for fn in SCENARIOS:
        if only and fn.__name__ != only:
            continue
        try:
            fn()
        except Fail as e:
            print(f'[b12.acceptance] FAIL {fn.__name__}: {e}')
            return 1
        except Exception as e:  # noqa: BLE001 - a crash inside a contract is a failure, reported the same way
            print(f'[b12.acceptance] FAIL {fn.__name__}: {type(e).__name__}: {e}')
            return 1
        print(f'[b12.acceptance] ok {fn.__name__}')
    print('[b12.acceptance] PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
