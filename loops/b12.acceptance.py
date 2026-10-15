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
LADDER_PAIRS = {('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA), ('claude-r2d2', OPUS), ('claude-l', OPUS)}
HOUR = dt.timedelta(seconds=3600)  # review r3 2.1: the hourly re-probe is measured against this literal, not the candidate's constant
HOME_FAKES = {}  # home -> its recording fakes dir (filled by make), for the process-start side of no_downgrade
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
    HOME_FAKES[h] = d
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


def no_downgrade(h, pairs=LADDER_PAIRS):
    """Review r3 2.2: every attempt record and every recorded engine process start names an (account, model) pair of
    the ladder in force; a model set alone would let an approved model run on an excluded account."""
    bad = [a for a in attempts(h) if (a.get('engine'), a.get('model')) not in pairs]
    check(not bad, f'attempts.jsonl carries an (account, model) pair outside the ladder: {bad}')
    for a in attempts(h):
        check(a.get('model') not in BELOW, f'a model below the floor was invoked: {a}')
    d = HOME_FAKES.get(h)
    if d is not None:
        started = [(c.get('acc'), c.get('model')) for c in calls(d)]
        bad = [p for p in started if p not in pairs]
        check(not bad, f'an engine process was started outside the ladder pairs: {bad}')


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
    # requirement 11: inside the hour the higher tiers are not re-probed even when they would work again
    set_table(d, {})
    event(h, 'B')
    check(sup.run_once() is True, 'turn B did not run')
    check(invoked(d)[4:] == [('claude-r2d2', OPUS)], f'no probe inside the hour: {invoked(d)[4:]}')
    check(engine_file(h) == {'acc': 'claude-r2d2', 'model': OPUS}, 'still on the floor inside the hour')
    # the hour passes: the probe reaches tier 1, the episode ends, the engine file returns to the configured pair
    sup.clock.now += dt.timedelta(seconds=S.TIER_PROBE_S)
    event(h, 'C')
    check(sup.run_once() is True, 'turn C did not run')
    check(invoked(d)[5:] == [('claude-r2d2', FABLE)], f'the hourly probe must reach tier 1 first: {invoked(d)[5:]}')
    ended = [it['text'] for it in S.read_outbox(h) if 'fallback ended' in it.get('text', '')]
    check(ended and 'tier 1/3 claude-r2d2 (claude-fable-5-1)' in ended[-1], f'the ended notice names the tier: {ended}')
    check(engine_file(h) == {'acc': 'claude-r2d2', 'model': FABLE}, f'the return lands in the engine file: {engine_file(h)}')
    rt = json.loads(Path(h, 'engine-runtime.json').read_text())
    check(not rt.get('active') and 'probe_after' not in rt, f'episode closed, no probe pending: {rt}')
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
    def refusal(model, current):
        return (f"model {model} is not on the manager's allowlist (floor {OPUS}); allowed: "
                f"{FABLE} on claude-r2d2, claude-l; {ASTRA} on codex; {OPUS} on claude-r2d2, claude-l"
                f"; nothing changed (engine: {current})")
    for args, model in ((('acc=claude-l', 'model=claude-sonnet-5'), 'claude-sonnet-5'),
                        (('model=opus5',), 'claude-opus-5'), (('acc=codex', 'model=gpt6.1'), 'gpt-6.1-sol'),
                        (('acc=codex', 'model=gpt-5.6-sol'), 'gpt-5.6-sol'),
                        (('acc=claude-l', 'model=claude-haiku-4-5-20251001'), 'claude-haiku-4-5-20251001')):
        rc, out, err = hydra(h, 'engine', *args)
        check(rc == 2, f'hydra engine {args} must exit 2, got {rc} {out!r} {err!r}')
        check(out == '' and err == refusal(model, f'claude-r2d2 ({FABLE})') + '\n',
              f'the complete console refusal differs: {out!r} {err!r}')
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
    check(reply == refusal('claude-sonnet-5', f'claude-l ({FABLE}) [persistent]'), f'Slack refusal: {reply!r}')
    check(Path(h, 'engine').read_bytes() == before, 'engine file changed by a refused Slack command')
    check(runtime_path.read_bytes() == runtime_before, 'runtime file changed by a refused Slack command')
    slack(br, '<@U_MANAGER> engine acc=codex model=gpt6.1', '20.1')
    check(poster.posted[-1][2] == refusal('gpt-6.1-sol', f'claude-l ({FABLE}) [persistent]'),
          f'a coder alias is refused in Slack: {poster.posted[-1][2]!r}')
    check(Path(h, 'engine').read_bytes() == before and runtime_path.read_bytes() == runtime_before,
          'both files must remain byte-identical after the refused coder alias in Slack')
    for ts, text, model in (('20.11', '<@U_MANAGER> engine model=opus5', 'claude-opus-5'),
                            ('20.12', '<@U_MANAGER> engine acc=codex model=gpt-5.6-sol', 'gpt-5.6-sol')):
        slack(br, text, ts)  # review r3 1.1: the whole refusal matrix in Slack, not only on the console
        check(poster.posted[-1][2] == refusal(model, f'claude-l ({FABLE}) [persistent]'), f'Slack refusal of {model}: {poster.posted[-1][2]!r}')
        check(Path(h, 'engine').read_bytes() == before and runtime_path.read_bytes() == runtime_before,
              f'both files must remain byte-identical after the refused Slack command {text!r}')
    slack(br, '<@U_MANAGER> engine model=sonnet5', '20.15')
    reply = poster.posted[-1][2]
    check('unknown model' in reply and 'sonnet5 ->' not in reply and 'fable5.1' in reply
          and '\n' not in reply and reply.endswith(f'; nothing changed (engine: claude-l ({FABLE}) [persistent])'),
          f'unknown sonnet5 alias in Slack: {reply!r}')
    check(Path(h, 'engine').read_bytes() == before and runtime_path.read_bytes() == runtime_before,
          'both files must remain byte-identical after an unknown Slack alias')
    slack(br, '<@U_MANAGER> engine acc=claude-r2d2 model=fable5.1', '20.2')
    check(poster.posted[-1][2] == 'engine: claude-r2d2 (claude-fable-5-1) [persistent]', f'an allowed Slack command: {poster.posted[-1][2]!r}')
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
    no_downgrade(h, {('codex', ASTRA), ('claude-l', OPUS)})
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
    """Exercise the real turn/error/notice path; only engine invocation and eligibility inputs are fake. A usage
    walk through every step posts the unavailable line; a fault (rate limit, other, crash, timeout) ends the walk
    at that step and posts the `engine fault:` line instead (requirement 12)."""
    ladder = [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA),
              ('claude-r2d2', OPUS), ('claude-l', OPUS)]
    labels = [f'tier {tier} {acc} ({model}): ' for tier, (acc, model) in zip((1, 1, 2, 3, 3), ladder)]

    def run(replies, expected, expected_calls, *, skip=None, custom=None, fault=False):
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
        faults = [it for it in S.read_outbox(h) if (it.get('text') or '').startswith('engine fault: ')]
        if fault:
            wanted = 'engine fault: ' + expected[:1000]
            check(not lines, f'a fault must not post the unavailable line: {lines!r}')
            check(len(faults) == 1 and faults[0]['text'] == wanted, f'exact fault contract: expected {wanted!r}, got {faults!r}')
            check(not S.read_engine_runtime(h).get('active'), 'a fault never starts a fallback episode')
            lines = faults
        else:
            wanted = 'manager unavailable: all allowed engines failed (' + expected[:1000] + ')'
            check(not faults, f'a usage walk posts no fault line: {faults!r}')
            check(len(lines) == 1 and lines[0]['text'] == wanted,
                  f'exact reason contract: expected {wanted!r}, got {lines!r}')
        check(lines[0].get('channel') == 'C_DEV' and lines[0].get('thread_ts') is None,
              'reason notice must be top level')
        # Encoding must round-trip: limits count code points, never slices of UTF-8 bytes.
        check(lines[0]['text'].encode('utf-8').decode('utf-8') == wanted, 'broken Unicode boundary')
        no_downgrade(h, {(name, ASTRA) for name in custom} if custom else LADDER_PAIRS)

    error = '界🙂é' * 45 + 'UNTRUNCATED_SENTINEL'
    # a rate limit on tier 3 ends the walk there: three usage steps before it, the fault line names only that step
    outcomes = [(1, 'out of usage credits'), (1, 'usage limit reached'),
                (1, 'HTTP 401 Unauthorized'), (1, '429 Too Many Requests'), (1, error)]
    run(dict(zip(ladder, outcomes)), labels[3] + 'rate_limit', ladder[:4], fault=True)
    # an `other` error on the first step: nothing else is tried, the error text is bounded to 80 code points
    run(dict(zip(ladder, [(1, error)] + outcomes[1:])), labels[0] + 'other: ' + error[:80], ladder[:1], fault=True)
    # every step out on usage: the unavailable line with all five reasons
    usage = [(1, 'out of usage credits'), (1, 'usage limit reached'), (1, 'HTTP 401 Unauthorized'),
             (1, 'out of usage credits'), (1, 'usage limit reached')]
    reasons = ['credits', 'usage_limit', 'auth', 'credits', 'usage_limit']
    run(dict(zip(ladder, usage)), '; '.join(l + r for l, r in zip(labels, reasons)), ladder)
    for why in ('unverified credential', 'over budget', 'identity mismatch'):
        reasons = ['credits', 'skipped: ' + why, 'credits', 'credits', 'skipped: ' + why]
        run({pair: (1, 'out of usage credits') for pair in ladder},
            '; '.join(l + r for l, r in zip(labels, reasons)), [ladder[i] for i in (0, 2, 3)], skip=why)
    reasons = ['prompt_too_long', 'skipped: skipped Claude family', 'auth',
               'skipped: skipped Claude family', 'skipped: skipped Claude family']
    run({ladder[0]: (1, 'Prompt is too long for this model'), ladder[2]: (1, 'HTTP 401 Unauthorized')},
        '; '.join(l + r for l, r in zip(labels, reasons)), [ladder[0], ladder[2]])
    for rc, reason in ((17, 'crash'), (124, 'timeout')):
        # An uncertain attempt terminates traversal immediately as a fault; no later tier is falsely called failed.
        run({ladder[0]: (rc, 'unstructured process failure')}, labels[0] + reason, [ladder[0]], fault=True)
    names = [f'codex{i:02d}' for i in range(30)]
    long_reasons = '; '.join(f'tier 1 {name} ({ASTRA}): credits' for name in names)
    check(len(long_reasons) > 1000, 'total-bound fixture must exceed the limit')
    run({(name, ASTRA): (1, 'out of usage credits') for name in names}, long_reasons,
        [(name, ASTRA) for name in names], custom=names)


def alias_remapping_cannot_extend_allowlist():
    """Home aliases resolve before policy checks on config, both commands and invocation."""
    for alias, disallowed in [('fable5.1', 'claude-sonnet-5'), ('opus5.5', 'claude-opus-5')]:
        cfg = {'engine_policy': {'ladder': [{'model': alias, 'accounts': ['claude-r2d2']}]}}
        h, d = home(config=cfg), fakes()
        Path(h, 'models.json').write_text(json.dumps({'claude': {'aliases': {alias: disallowed}}}))
        models = S.load_models(h)
        check(S.resolve_model('claude', alias, models) == disallowed, 'alias fixture did not remap')
        ladder = S.manager_ladder(cfg, models, engines(d))
        check([t['model'] for t in ladder] == [FABLE, ASTRA, OPUS],
              f'remapped disallowed model must invalidate the whole override: {ladder}')
        check('ignored' in policy_line(h) and disallowed in policy_line(h), 'diagnostic must name resolved model')
        runtime = Path(h, 'engine-runtime.json')
        runtime.write_text(json.dumps({'configured': {'acc': 'claude-r2d2', 'model': FABLE}}))
        before = Path(h, 'engine').read_bytes(), runtime.read_bytes()
        rc, out, err = hydra(h, 'engine', 'acc=claude-r2d2', f'model={alias}')
        check(rc == 2 and "not on the manager's allowlist" in err and 'nothing changed' in err,
              f'remapped alias console refusal: {rc} {out!r} {err!r}')
        check((Path(h, 'engine').read_bytes(), runtime.read_bytes()) == before, 'console refusal changed state')
        poster = Poster()
        slack(make_bridge(h, poster), f'<@U_MANAGER> engine acc=claude-r2d2 model={alias}', str(time.time_ns()))
        check(poster.posted and "not on the manager's allowlist" in poster.posted[-1][2]
              and 'nothing changed' in poster.posted[-1][2], f'remapped alias Slack refusal: {poster.posted}')
        check((Path(h, 'engine').read_bytes(), runtime.read_bytes()) == before, 'Slack refusal changed state')
        eid = event(h, 'remapped alias turn')
        check(make(h, d).run_once() is True and eid in S.handled_ids(h), 'default ladder must serve the event')
        check(invoked(d) == [('claude-r2d2', FABLE)], f'alias must not alter immutable default invocation: {invoked(d)}')
        no_downgrade(h)


def failure_success_persistence_matrix():
    """Auth persists a successful fallback; rate_limit/other are faults (requirement 12): nothing moves, nothing
    persists, one top-level `engine fault:` line, the event waits for the retry on the same engine."""
    for reason in ('auth', 'rate_limit', 'other'):
        h, d = home(), fakes({f'claude-r2d2|{FABLE}': reason})
        primary = {'acc': 'claude-r2d2', 'model': FABLE}
        winner = {'acc': 'claude-l', 'model': FABLE}
        runtime = Path(h, 'engine-runtime.json')
        runtime.write_text(json.dumps({'configured': primary}))
        before = Path(h, 'engine').read_bytes(), runtime.read_bytes()
        eid = event(h, f'failure-success {reason}')
        sup = make(h, d)
        check(sup.run_once() is True, f'{reason}: turn did not run')
        att = attempts(h)
        notices = [it['text'] for it in S.read_outbox(h) if 'fallback started' in it.get('text', '')]
        faults = [it for it in S.read_outbox(h) if (it.get('text') or '').startswith('engine fault: ')]
        state = json.loads(runtime.read_text())
        if reason == 'auth':
            check(eid in S.handled_ids(h), 'auth: event not served')
            check(invoked(d) == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'auth: wrong attempts {invoked(d)}')
            check(len(att) == 2 and att[0]['classification'] == 'auth' and att[1]['success'] is True, f'auth: {att}')
            check(engine_file(h) == winner, 'auth fallback must persist winner in engine file')
            check(state.get('configured') == primary and state.get('effective') == winner
                  and state.get('active') is True and state.get('episode_id') and state.get('reason') == att[0]['reason'],
                  f'auth fallback runtime state: {state}')
            check(len(notices) == 1 and f'claude-l ({FABLE})' in notices[0], f'auth fallback notice: {notices}')
            check(not faults, f'auth is not a fault: {faults}')
        else:
            check(eid not in S.handled_ids(h), f'{reason}: a fault leaves the event pending')
            check(invoked(d) == [('claude-r2d2', FABLE)], f'{reason}: a fault never tries the next step: {invoked(d)}')
            check((Path(h, 'engine').read_bytes(), runtime.read_bytes()) == before, f'{reason}: a fault persisted state')
            check(not notices and not state.get('active') and not state.get('episode_id'), f'{reason}: a fault started an episode')
            what = {'rate_limit': 'rate_limit', 'other': 'other: a plain runtime crash'}[reason]
            check(len(faults) == 1 and faults[0]['text'] == f'engine fault: tier 1 claude-r2d2 ({FABLE}): {what}'
                  and faults[0].get('channel') == 'C_DEV' and faults[0].get('thread_ts') is None, f'{reason}: fault line {faults}')
            fault = json.loads(Path(h, 'logs', 'fault.json').read_text())
            check(fault.get('episode_id') and faults[0].get('id') == f"fault-{fault['episode_id']}", f'fault episode: {fault}')
            check(Path(h, 'logs', 'retry-after').exists(), f'{reason}: retry-after missing')
            os.remove(os.path.join(h, 'logs', 'retry-after'))
            check(sup.run_once() is True and invoked(d) == [('claude-r2d2', FABLE)] * 2, f'{reason}: the retry must use the same engine')
            check(len([it for it in S.read_outbox(h) if (it.get('text') or '').startswith('engine fault: ')]) == 1,
                  f'{reason}: a repeated fault inside the episode posts nothing more')
            set_table(d, {})
            os.remove(os.path.join(h, 'logs', 'retry-after'))
            check(sup.run_once() is True and eid in S.handled_ids(h), f'{reason}: recovery did not serve the event')
            check(json.loads(Path(h, 'logs', 'fault.json').read_text()).get('episode_id') is None, 'recovery closes the fault episode')
            check((Path(h, 'engine').read_bytes(), runtime.read_bytes()) == before, f'{reason}: recovery on the same engine changed state')
        no_downgrade(h)


def persistent_model_switch_replaces_child():
    """Successful Opus and Fable turns must reach distinct children with matching model argv."""
    h, d = home(engine={'acc': 'claude-r2d2', 'model': OPUS, 'mode': 'persistent'}), fakes()
    sup = make(h, d)
    try:
        first_id = event(h, 'B12_OPUS_DELIVERY')
        check(sup.run_once() is True and first_id in S.handled_ids(h), 'Opus persistent turn not served')
        first = calls(d)
        check(len(first) == 2 and first[0].get('persistent') and first[0]['stdin'] == '<persistent start>'
              and first[1]['pid'] == first[0]['pid'] and 'B12_OPUS_DELIVERY' in first[1]['stdin'],
              f'Opus must receive the first message on its live child: {first}')
        rc, out, err = hydra(h, 'engine', 'acc=claude-r2d2', 'model=fable5.1')
        check(rc == 0 and engine_file(h).get('mode') == 'persistent', f'explicit switch: {rc} {out} {err}')
        second_id = event(h, 'B12_FABLE_DELIVERY')
        check(sup.run_once() is True and second_id in S.handled_ids(h), 'Fable persistent turn not served')
        recorded = calls(d)
        check(len(recorded) == 4, f'exactly two child starts and two delivered messages: {recorded}')
        for start, delivered, model, marker in ((recorded[0], recorded[1], OPUS, 'B12_OPUS_DELIVERY'),
                                                 (recorded[2], recorded[3], FABLE, 'B12_FABLE_DELIVERY')):
            check(start.get('persistent') and start['stdin'] == '<persistent start>'
                  and delivered.get('persistent') and delivered['pid'] == start['pid']
                  and marker in delivered['stdin'], f'wrong child delivery for {model}')
            check(start['model'] == delivered['model'] == model
                  and start['argv'][start['argv'].index('--model') + 1] == model, f'wrong child argv for {model}')
        check(recorded[0]['pid'] != recorded[2]['pid'], 'model switch reused the Opus child')
        no_downgrade(h)
    finally:
        sup._retire_persistent('planned', 'acceptance done')


def explicit_primary_reselection_closes_episode():
    """Requirement 4 and the exempted b8 regression: a stored mode never masks explicit recovery."""
    for surface in ('console', 'slack'):
        h = home(engine={'acc': 'claude-r2d2', 'model': FABLE, 'mode': 'per-turn'},
                 config={'engine_fallback': {'claude_models': []}})
        d = fakes({f'claude-r2d2|{FABLE}': 'credits'})
        sup = make(h, d)
        sup.run_engines('open fallback', [])
        runtime = S.read_engine_runtime(h)
        episode = runtime.get('episode_id')
        check(episode and runtime.get('active') is True, 'fallback episode did not open')
        check(runtime['configured'].get('mode') == 'per-turn', 'fixture lost stored mode')
        check(invoked(d) == [('claude-r2d2', FABLE), ('claude-l', FABLE)], 'fallback fixture order')
        if surface == 'console':
            rc, out, err = hydra(h, 'engine', 'acc=claude-r2d2', 'model=fable5.1', 'mode=per-turn')
            check(rc == 0, f'explicit console selection failed: {out!r} {err!r}')
        else:
            poster = Poster()
            br = make_bridge(h, poster)
            slack(br, '<@U_MANAGER> engine acc=claude-r2d2 model=fable5.1 mode=per-turn', '41.0')
            check(poster.posted[-1][2] == f'engine: claude-r2d2 ({FABLE}) [per-turn]', 'explicit Slack selection failed')
        check(engine_file(h) == {'acc': 'claude-r2d2', 'model': FABLE, 'mode': 'per-turn'}, 'selection not persisted')
        set_table(d, {})
        sup = make(h, d)  # A fresh supervisor observes the explicit selection.
        sup.run_engines('recover primary', [])
        check(invoked(d)[-1] == ('claude-r2d2', FABLE), 'explicit primary not invoked')
        runtime = S.read_engine_runtime(h)
        check(runtime.get('active') is False, f'episode stayed active: {runtime}')
        expected = f'fallback ended (episode {episode}): back on tier 1/3 claude-r2d2 ({FABLE})'
        ended = [it for it in S.read_outbox(h) if 'fallback ended' in it.get('text', '')]
        check(len(ended) == 1 and ended[0]['text'] == expected, f'ended notice: {ended}')
        receipt = Poster()
        S.drain_outbox(h, receipt)
        sup = make(h, d)
        sup.run_engines('healthy after restart', [])
        S.drain_outbox(h, receipt)
        check([t for _, _, t in receipt.posted if 'fallback ended' in t] == [expected], 'restart repeated the ended notice')
        check(S.read_engine_runtime(h).get('active') is False, 'restart reopened the episode')
        no_downgrade(h)


def filtered_engine_accounts():
    """Requirement 1: missing/unexecutable accounts never become invocation steps."""
    for variant in ('absent', 'no-bin', 'none'):
        h = home(config={'engine_fallback': {'claude_models': []}})
        d = fakes()
        emap = engines(d)
        if variant == 'absent':
            del emap['claude-r2d2']
            expected = [(1, 'claude-l', FABLE), (2, 'codex', ASTRA), (3, 'claude-l', OPUS)]
        elif variant == 'no-bin':
            del emap['claude-r2d2']['bin']
            expected = [(1, 'claude-l', FABLE), (2, 'codex', ASTRA), (3, 'claude-l', OPUS)]
        else:
            emap = {'claude-l': {'cred': 'claude-l.env'}}
            expected = []
        steps = S.ladder_steps(S.MANAGER_LADDER, emap)
        check(steps == expected, f'{variant}: filtered steps {steps}')
        sup = S.Supervisor(home=h, engines=emap, poster=Poster(), buildlog_poster=Poster())
        if expected:
            sup.run_engines('filtered accounts', [])
            check(invoked(d) == [('claude-l', FABLE)], f'{variant}: traversal {invoked(d)}')
        else:
            try:
                sup.run_engines('no executable accounts', [])
            except S.AllEnginesFailed:
                pass
            else:
                raise Fail('no executable accounts must raise AllEnginesFailed')
            check(calls(d) == [] and attempts(h) == [], 'empty ladder invoked or recorded an engine')
        no_downgrade(h)


def mode_only_commands_enforce_policy():
    """Requirement 2: changing only mode cannot bypass stale/custom pair enforcement."""
    for model, cfg in [('claude-sonnet-5', {}),
                       (FABLE, {'engine_policy': {'ladder': [{'model': ASTRA, 'accounts': ['codex']}]}})]:
        h = home(engine={'acc': 'claude-r2d2', 'model': model}, config=cfg)
        runtime = Path(h, 'engine-runtime.json')
        runtime.write_text(json.dumps({'configured': {'acc': 'claude-r2d2', 'model': model},
                                      'active': True, 'episode_id': 'preserve-mode-refusal'}) + '\n')
        before = Path(h, 'engine').read_bytes(), runtime.read_bytes()
        poster = Poster()
        br = make_bridge(h, poster)
        for mode in ('persistent', 'per-turn'):
            rc, out, err = hydra(h, 'engine', f'mode={mode}')
            check(rc == 2 and "not on the manager's allowlist" in err and 'nothing changed' in err,
                  f'mode-only console bypass: {rc} {out!r} {err!r}')
            check((Path(h, 'engine').read_bytes(), runtime.read_bytes()) == before, 'mode-only console changed files')
            slack(br, f'<@U_MANAGER> engine mode={mode}', str(time.time_ns()))
            reply = poster.posted[-1][2]
            check("not on the manager's allowlist" in reply and 'nothing changed' in reply and '\n' not in reply,
                  f'mode-only Slack bypass: {reply!r}')
            check((Path(h, 'engine').read_bytes(), runtime.read_bytes()) == before, 'mode-only Slack changed files')
        no_downgrade(h)



STICKY_RUNTIME = {'configured': {'acc': 'claude-r2d2', 'model': FABLE}, 'effective': {'acc': 'claude-l', 'model': FABLE},
                  'episode_id': 'sticky-acceptance', 'since': '2026-10-11T18:00:00Z', 'reason': 'credits', 'active': True}


def notices(h, needle):
    return [it for it in S.read_outbox(h) if needle in (it.get('text') or '')]


def engine_line(h):
    return status(h).splitlines()[0]


def sticky_account_after_r2d2_out():
    """Founder acceptance (w): R2D2 out -> L; the next turn stays on L with no R2D2 call; status separates the pairs."""
    h, d = home(), fakes({f'claude-r2d2|{FABLE}': 'credits'})
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'turn A order: {invoked(d)}')
    check(engine_file(h) == {'acc': 'claude-l', 'model': FABLE}, f'the winner is persisted: {engine_file(h)}')
    rt = S.read_engine_runtime(h)
    check(rt.get('configured') == {'acc': 'claude-r2d2', 'model': FABLE} and rt.get('active') and 'probe_after' not in rt,
          f'same-tier move: configured kept, no probe: {rt}')
    event(h, 'B')
    check(sup.run_once() is True, 'turn B did not run')
    check(invoked(d)[2:] == [('claude-l', FABLE)], f'the next turn must stay on L with no R2D2 call: {invoked(d)[2:]}')
    line = engine_line(h)
    check(line.startswith(f'engine: configured claude-r2d2 ({FABLE}) | effective claude-l ({FABLE}) | last turn ran on claude-l ({FABLE})')
          and 'MISMATCH' not in line, f'first status line: {line!r}')
    check('active tier 1/3 claude-l (claude-fable-5-1)' in policy_line(h), f'policy line: {policy_line(h)!r}')
    no_downgrade(h)


def l_out_tries_r2d2_then_both_out_lands_on_astra():
    """Founder acceptance (x): on L, L out -> R2D2 tried -> both out -> Astra; one moved line; the probe an hour out."""
    h = home(engine={'acc': 'claude-l', 'model': FABLE})
    Path(h, 'engine-runtime.json').write_text(json.dumps(STICKY_RUNTIME))
    d = fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('claude-l', FABLE), ('claude-r2d2', FABLE), ('codex', ASTRA)], f'order: {invoked(d)}')
    check(engine_file(h) == {'acc': 'codex', 'model': ASTRA}, f'Astra persisted: {engine_file(h)}')
    moved = notices(h, 'fallback moved')
    check(len(moved) == 1 and moved[0].get('thread_ts') is None and f'tier 1/3 claude-l ({FABLE}) unavailable' in moved[0]['text']
          and moved[0]['text'].endswith(f'now running tier 2/3 codex ({ASTRA})'), f'moved line: {moved}')
    rt = S.read_engine_runtime(h)
    check(S.TIER_PROBE_S == 3600, f'TIER_PROBE_S must be 3600 s (founder rule 3): {S.TIER_PROBE_S!r}')
    due = S.parse_iso(rt.get('probe_after')) - sup.clock.now
    check(HOUR - dt.timedelta(seconds=10) <= due <= HOUR, f'probe an hour out (literal 3600 s): {rt}')
    check(rt.get('configured') == {'acc': 'claude-r2d2', 'model': FABLE}, 'the operator pair is never rewritten')
    check(engine_line(h).startswith(f'engine: configured claude-r2d2 ({FABLE}) | effective codex ({ASTRA}) | last turn ran on codex ({ASTRA})'),
          f'first status line: {engine_line(h)!r}')
    no_downgrade(h)


def hourly_probe_from_astra_returns_to_fable():
    """Founder acceptance (y): no Fable call inside the hour; one probe at the hour; back up as soon as a Fable
    account works; the configured pair's return ends the episode and restores the engine file."""
    h, d = home(), fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
    sup = make(h, d)
    event(h, 'A')
    check(sup.run_once() is True and engine_file(h)['acc'] == 'codex', 'turn A must land on Astra')
    event(h, 'B')
    check(sup.run_once() is True and invoked(d)[3:] == [('codex', ASTRA)], f'no Fable call inside the hour: {invoked(d)[3:]}')
    sup.clock.now += dt.timedelta(seconds=S.TIER_PROBE_S)
    event(h, 'C')
    check(sup.run_once() is True, 'turn C did not run')
    check(invoked(d)[4:] == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA)], f'one probe of both Fable accounts: {invoked(d)[4:]}')
    check(S.parse_iso(S.read_engine_runtime(h)['probe_after']) > sup.clock.now + dt.timedelta(seconds=S.TIER_PROBE_S - 60), 'the failed probe moves the next one out an hour')
    event(h, 'D')
    check(sup.run_once() is True and invoked(d)[7:] == [('codex', ASTRA)], f'no second probe inside the hour: {invoked(d)[7:]}')
    set_table(d, {f'claude-r2d2|{FABLE}': 'credits'})
    sup.clock.now += dt.timedelta(seconds=S.TIER_PROBE_S)
    event(h, 'E')
    check(sup.run_once() is True, 'turn E did not run')
    check(invoked(d)[8:] == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'back up as soon as L works: {invoked(d)[8:]}')
    check(engine_file(h) == {'acc': 'claude-l', 'model': FABLE}, f'L persisted: {engine_file(h)}')
    rt = S.read_engine_runtime(h)
    check(rt.get('active') and rt.get('effective') == {'acc': 'claude-l', 'model': FABLE} and 'probe_after' not in rt, f'runtime after the move up: {rt}')
    moved = notices(h, 'fallback moved')
    check(len(moved) == 1 and moved[0]['text'].endswith(f'now running tier 1/3 claude-l ({FABLE})'), f'moved line: {moved}')
    check(moved[0].get('id') == f"fallback-{rt['episode_id']}-move-1" and rt.get('moves') == 1, f'move id and counter: {moved[0].get("id")} {rt}')
    make(h, d)  # a restart after the move reconciles nothing new
    check(len(notices(h, 'fallback moved')) == 1, 'a restart after the move must not duplicate its line')
    set_table(d, {})
    event(h, 'F')
    check(sup.run_once() is True and invoked(d)[10:] == [('claude-l', FABLE)], f'sticky on L while it works: {invoked(d)[10:]}')
    # the configured pair serving again (here after the operator's re-selection) ends the episode
    S.set_engine(h, 'claude-r2d2', FABLE, engines(d))
    event(h, 'G')
    check(sup.run_once() is True and invoked(d)[11:] == [('claude-r2d2', FABLE)], f'turn G: {invoked(d)[11:]}')
    check(not S.read_engine_runtime(h).get('active') and len(notices(h, 'fallback ended')) == 1, 'episode ended once')
    # and a probe that succeeds on the configured pair restores the engine file by itself
    h2, d2 = home(), fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
    sup2 = make(h2, d2)
    event(h2, 'A')
    check(sup2.run_once() is True and engine_file(h2)['acc'] == 'codex', 'h2: Astra')
    set_table(d2, {})
    sup2.clock.now += dt.timedelta(seconds=S.TIER_PROBE_S)
    event(h2, 'B')
    check(sup2.run_once() is True and invoked(d2)[3:] == [('claude-r2d2', FABLE)], f'h2 probe: {invoked(d2)[3:]}')
    check(engine_file(h2) == {'acc': 'claude-r2d2', 'model': FABLE}, f'h2: the return lands in the engine file: {engine_file(h2)}')
    rt2 = S.read_engine_runtime(h2)
    check(not rt2.get('active') and 'probe_after' not in rt2 and len(notices(h2, 'fallback ended')) == 1, f'h2 runtime: {rt2}')
    check(engine_line(h2).startswith(f'engine: configured claude-r2d2 ({FABLE}) | last turn ran on claude-r2d2 ({FABLE})'), f'h2 line: {engine_line(h2)!r}')
    no_downgrade(h)
    no_downgrade(h2)


def session_error_on_l_changes_nothing():
    """Founder acceptance (z): a session error on L never moves the manager; one engine fault line per episode."""
    h = home(engine={'acc': 'claude-l', 'model': FABLE})
    rt_path = Path(h, 'engine-runtime.json')
    rt_path.write_text(json.dumps(STICKY_RUNTIME))
    d = fakes({f'claude-l|{FABLE}': 'other'})
    before = Path(h, 'engine').read_bytes(), rt_path.read_bytes()
    sup = make(h, d)
    eid = event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('claude-l', FABLE)], f'only the running engine is called: {invoked(d)}')
    check((Path(h, 'engine').read_bytes(), rt_path.read_bytes()) == before, 'engine file and runtime must stay byte-identical')
    check(eid not in S.handled_ids(h) and Path(h, 'logs', 'retry-after').exists(), 'the event waits for the retry')
    check(not unavailable_lines(h) and not notices(h, 'fallback'), 'no unavailable or fallback line')
    faults = notices(h, 'engine fault: ')
    check(len(faults) == 1 and faults[0].get('thread_ts') is None and faults[0].get('channel') == 'C_DEV'
          and faults[0]['text'] == f'engine fault: tier 1 claude-l ({FABLE}): other: a plain runtime crash', f'fault line: {faults}')
    fault = json.loads(Path(h, 'logs', 'fault.json').read_text())
    fault['queued'] = False  # the process died between writing the episode and queueing its line
    Path(h, 'logs', 'fault.json').write_text(json.dumps(fault))
    make(h, d)  # the next supervisor's start-up reconcile delivers it exactly once (the outbox id deduplicates)
    check(len(notices(h, 'engine fault: ')) == 1 and json.loads(Path(h, 'logs', 'fault.json').read_text()).get('queued') is True,
          'reconcile after a crash must deliver the line once and mark it queued')
    os.remove(os.path.join(h, 'logs', 'retry-after'))
    check(sup.run_once() is True and invoked(d)[1:] == [('claude-l', FABLE)] and len(notices(h, 'engine fault: ')) == 1,
          'the retry uses the same engine and posts no second line')
    check('active tier 1/3 claude-l (claude-fable-5-1)' in policy_line(h), f'policy unchanged: {policy_line(h)!r}')
    set_table(d, {})
    os.remove(os.path.join(h, 'logs', 'retry-after'))
    check(sup.run_once() is True and eid in S.handled_ids(h), 'recovery serves the pending event')
    check(json.loads(Path(h, 'logs', 'fault.json').read_text()).get('episode_id') is None, 'recovery closes the fault episode')
    check((Path(h, 'engine').read_bytes(), rt_path.read_bytes()) == before, 'recovery on the same engine changes no state')
    no_downgrade(h)



def quota_then_fault_retries_the_unchanged_rotation():
    """Review B2: r2d2 out of credits, then a fault on l: the walk stops at l, nothing is persisted, the retry (in the
    same process and after a restart) repeats the unchanged rotation; when l recovers the credits move persists."""
    h, d = home(), fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'other'})
    before = Path(h, 'engine').read_bytes()
    sup = make(h, d)
    eid = event(h, 'A')
    check(sup.run_once() is True, 'turn A did not run')
    check(invoked(d) == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'the walk stops at the faulting step: {invoked(d)}')
    check(Path(h, 'engine').read_bytes() == before and not S.read_engine_runtime(h).get('active'), 'a fault after a quota step persists nothing')
    faults = notices(h, 'engine fault: ')
    check(len(faults) == 1 and faults[0]['text'] == f'engine fault: tier 1 claude-l ({FABLE}): other: a plain runtime crash', f'fault line: {faults}')
    check(not unavailable_lines(h) and not notices(h, 'fallback started'), 'no unavailable or fallback line')
    check(eid not in S.handled_ids(h), 'the event waits')
    os.remove(os.path.join(h, 'logs', 'retry-after'))
    check(sup.run_once() is True and invoked(d)[2:] == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'the retry repeats the rotation: {invoked(d)[2:]}')
    os.remove(os.path.join(h, 'logs', 'retry-after'))
    sup2 = make(h, d)
    check(sup2.run_once() is True and invoked(d)[4:] == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'after a restart too: {invoked(d)[4:]}')
    check(len(notices(h, 'engine fault: ')) == 1 and eid not in S.handled_ids(h), 'one fault line per episode; the event still waits')
    set_table(d, {f'claude-r2d2|{FABLE}': 'credits'})
    os.remove(os.path.join(h, 'logs', 'retry-after'))
    check(sup2.run_once() is True and eid in S.handled_ids(h), 'recovery serves the event')
    check(engine_file(h) == {'acc': 'claude-l', 'model': FABLE} and S.read_engine_runtime(h).get('active'), 'the credits move persists on the recovering turn')
    check(len(notices(h, 'fallback started')) == 1, 'the fallback episode starts on the recovering turn')
    check(json.loads(Path(h, 'logs', 'fault.json').read_text()).get('episode_id') is None, 'the fault episode closes')
    no_downgrade(h)


def stated_reset_time_governs_the_probe():
    """Review 1.1: several stated resets, past and future -> the earliest future one; past-only -> now + TIER_PROBE_S."""
    reply = 'REPLY: ok\n---HANDOFF---\ntracks: t\nwaiting on: n\nlast decision: n\nnext action: n\nopen question: n'
    def iso(t):
        return t.strftime('%Y-%m-%dT%H:%M:%SZ')
    def run(resets):
        h, d = home(), fakes()
        sup = make(h, d)
        now = sup.clock.now
        stated = {name: iso(now + delta) for name, delta in resets.items()}
        seen = []
        def invoke(name, spec, message, model, preamble=''):
            seen.append((name, model))
            if name == 'codex':
                return 0, reply, '', {}
            return 1, '', f'usage limit reached; resets {stated[name]}', {}
        sup.invoke = invoke
        event(h, 'A')
        check(sup.run_once() is True and seen == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA)], f'walk: {seen}')
        return h, sup, seen, now
    h, sup, seen, now = run({'claude-r2d2': dt.timedelta(minutes=50), 'claude-l': dt.timedelta(minutes=20)})
    check(S.read_engine_runtime(h).get('probe_after') == iso(now + dt.timedelta(minutes=20)), f'earliest future reset: {S.read_engine_runtime(h)}')
    sup.clock.now += dt.timedelta(minutes=10)
    event(h, 'B')
    check(sup.run_once() is True and seen[3:] == [('codex', ASTRA)], f'no probe before the reset: {seen[3:]}')
    sup.clock.now += dt.timedelta(minutes=10)
    event(h, 'C')
    check(sup.run_once() is True and seen[4:6] == [('claude-r2d2', FABLE), ('claude-l', FABLE)], f'probe at the reset: {seen[4:]}')
    h, sup, seen, now = run({'claude-r2d2': dt.timedelta(hours=-1), 'claude-l': dt.timedelta(minutes=20)})
    check(S.read_engine_runtime(h).get('probe_after') == iso(now + dt.timedelta(minutes=20)), 'a past reset is ignored in favour of the future one')
    h, sup, seen, now = run({'claude-r2d2': dt.timedelta(hours=-2), 'claude-l': dt.timedelta(hours=-1)})
    due = S.parse_iso(S.read_engine_runtime(h).get('probe_after')) - now
    check(HOUR - dt.timedelta(seconds=10) <= due <= HOUR + dt.timedelta(seconds=10), f'past-only resets fall back to one literal hour: {due}')
    no_downgrade(h)



class SimulatedCrash(BaseException):
    pass


def _crash_windows(prefix, arrange, durable_check, delivered_text, after_restart, windows=('before-queue', 'after-queue', 'after-receipt'), before_restart=None):
    """Crash at the durable-state/enqueue boundary of a notice whose outbox id starts with `prefix`: before the
    enqueue, after it, and after the delivery receipt. `arrange(h, d)` builds the home and returns (sup, eid);
    `durable_check(h, note_id)` asserts the durable state at the seam; `after_restart(h, d, window, eid)` finishes
    the scenario; `before_restart(h, d, window)`, when given, changes the world before the restarted supervisor is
    built (review r3 8.1). Exactly one eventual top-level delivery of the original id in every window."""
    for window in windows:
        h, d, poster = home(), fakes(), Poster()
        sup, eid = arrange(h, d)
        original = S._queue_notice_once
        reached = []
        def crash_queue(home_arg, channel, text, note_id):
            if not note_id.startswith(prefix) or (prefix == 'fallback-' and '-move-' not in note_id):
                return original(home_arg, channel, text, note_id)
            durable_check(h, note_id)
            reached.append((note_id, text))
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
        check(len(reached) == 1, f'{prefix}{window}: crash seam not reached exactly once: {reached}')
        note_id, text = reached[0]
        pending = [it for it in S.read_outbox(h) if it.get('id') == note_id]
        check(len(pending) == (1 if window == 'after-queue' else 0), f'{prefix}{window}: unexpected outbox state {pending}')
        retry = Path(h, 'logs/retry-after')
        if retry.exists():
            retry.unlink()
        if before_restart:
            before_restart(h, d, window)
        restarted = make(h, d)  # the start-up reconcile finishes a durable, not-yet-queued line
        after_restart(h, d, restarted, window, eid)
        S.drain_outbox(h, poster)
        delivered = [(c, t, x) for c, t, x in poster.posted if delivered_text in x]
        check(len(delivered) == 1 and delivered[0][0:2] == ('C_DEV', None), f'{prefix}{window}: exactly one eventual delivery: {delivered}')
        check(S.post_receipt(h, note_id) is not None, f'{prefix}{window}: the original id must be receipted')
        check(len([it for it in S.read_outbox(h) if it.get('id') == note_id]) <= 1, f'{prefix}{window}: duplicated outbox item')
        no_downgrade(h)


def fault_notice_survives_crash_windows():
    """Review r2 2.1: the fault line is durable in logs/fault.json before it is queued; a crash at either side of
    the enqueue, or after the receipt, yields exactly one delivery; recovery before the restart still delivers it."""
    def arrange(h, d):
        set_table(d, {f'claude-r2d2|{FABLE}': 'other'})
        return make(h, d), event(h, 'fault crash')
    def durable(h, note_id):
        ep = json.loads(Path(h, 'logs/fault.json').read_text())
        check(note_id == f"fault-{ep['episode_id']}" and ep.get('text', '').startswith('engine fault: tier 1 claude-r2d2') and ep.get('queued') is False,
              f'fault episode must be durable and unqueued before the enqueue: {ep}')
    def before_restart(h, d, window):
        if window == 'before-queue':
            set_table(d, {})  # the engines recover BEFORE the restart: the start-up reconcile must still deliver the line
    def after_restart(h, d, restarted, window, eid):
        check(restarted.run_once() is True, f'{window}: the restart must retry the pending event')
        ep = json.loads(Path(h, 'logs/fault.json').read_text())
        if window == 'before-queue':
            check(eid in S.handled_ids(h) and ep.get('episode_id') is None, f'{window}: recovery serves the event and closes the episode: {ep}')
        else:
            check(eid not in S.handled_ids(h) and ep.get('episode_id'), f'{window}: the fault persists, the event waits: {ep}')
    _crash_windows('fault-', arrange, durable, 'engine fault: tier 1 claude-r2d2', after_restart, before_restart=before_restart)


def move_notice_survives_crash_windows():
    """Review r2 2.2: the move is durable (moves counter, move_from, pending_notice) before its line is queued; a
    crash around the enqueue or after the receipt yields exactly one delivery with the original move id."""
    def arrange(h, d):
        set_table(d, {f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
        sup = make(h, d)
        event(h, 'to astra')
        check(sup.run_once() is True and engine_file(h)['acc'] == 'codex', 'arrange: Astra')
        set_table(d, {f'claude-r2d2|{FABLE}': 'credits'})  # L is back; the hourly probe will move up
        sup.clock.now += dt.timedelta(seconds=S.TIER_PROBE_S)
        return sup, event(h, 'move crash')
    def durable(h, note_id):
        rt = S.read_engine_runtime(h)
        check(note_id == f"fallback-{rt['episode_id']}-move-1" and rt.get('moves') == 1 and rt.get('pending_notice') == 'move'
              and rt.get('move_from') == {'acc': 'codex', 'model': ASTRA} and rt.get('effective') == {'acc': 'claude-l', 'model': FABLE},
              f'the move must be durable before the enqueue: {rt}')
    def after_restart(h, d, restarted, window, eid):
        rt = S.read_engine_runtime(h)
        check(rt.get('pending_notice') is None and rt.get('moves') == 1, f'{window}: reconcile must clear the pending move: {rt}')
        check(engine_file(h) == {'acc': 'claude-l', 'model': FABLE}, f'{window}: the move itself is kept: {engine_file(h)}')
        event(h, 'after')
        check(restarted.run_once() is True and invoked(d)[-1] == ('claude-l', FABLE), f'{window}: the next turn runs on L')
    _crash_windows('fallback-', arrange, durable, 'fallback moved (episode', after_restart)


def fault_text_reaches_console_and_self_events():
    """Review r2 1.1: a founder console event gets the fault text in its reply file; an unroutable self event gets it
    in logs/self-replies.jsonl; both stay pending for the retry like a Slack event."""
    h, d = home(), fakes({f'claude-r2d2|{FABLE}': 'other'})
    sup = make(h, d)
    S.append_event(h, {'id': 'cli-fault', 'source': 'cli', 'at': time.time(), 'payload': {'text': 'status please', 'instructs': True}})
    S.append_event(h, {'id': 'self-fault', 'source': 'self', 'at': time.time(), 'payload': {'text': 'continue b', 'instructs': False}})
    check(sup.run_once() is True, 'the faulting turn must run')
    text = f'engine fault: tier 1 claude-r2d2 ({FABLE}): other: a plain runtime crash'
    reply = Path(h, 'inbox', 'replies', 'cli-fault.txt')
    check(reply.exists() and reply.read_text() == text + '\n', f'console reply file: {reply.read_text() if reply.exists() else None!r}')
    rows = S.read_jsonl(os.path.join(h, 'logs', 'self-replies.jsonl'))
    mine = [r for r in rows if isinstance(r, dict) and r.get('id') == 'self-fault']
    check(len(mine) == 1 and mine[0].get('text') == text, f'self-replies record: {rows}')
    check(len(notices(h, 'engine fault: ')) == 1, 'one top-level line as well')
    check('self-fault' not in S.handled_ids(h) and 'cli-fault' not in S.handled_ids(h), 'both events wait for the retry')
    no_downgrade(h)


def probe_due_during_fault_backoff_is_taken_on_the_retry():
    """Review r2 5.1: on a lower tier, a fault starts the back-off; when probe_after expires meanwhile, the retry
    (in-process or after a restart) probes the tiers above first."""
    for restart in (False, True):
        h, d = home(), fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
        sup = make(h, d)
        event(h, 'A')
        check(sup.run_once() is True and engine_file(h)['acc'] == 'codex', 'arrange: Astra')
        set_table(d, {f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits', f'codex|{ASTRA}': 'rate_limit'})
        eid = event(h, 'B')
        check(sup.run_once() is True and invoked(d)[3:] == [('codex', ASTRA)], f'inside the hour the fault hits Astra alone: {invoked(d)[3:]}')
        check(len(notices(h, 'engine fault: ')) == 1 and eid not in S.handled_ids(h), 'fault line, event waits')
        sup.clock.now += dt.timedelta(seconds=S.TIER_PROBE_S)  # the probe becomes due during the back-off
        os.remove(os.path.join(h, 'logs', 'retry-after'))
        runner = make(h, d) if restart else sup
        if restart:
            runner.clock.now = sup.clock.now
        set_table(d, {f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})  # Astra works again, Fable still out
        check(runner.run_once() is True, 'the retry must run')
        check(invoked(d)[4:] == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA)], f'restart={restart}: the retry probes first: {invoked(d)[4:]}')
        check(eid in S.handled_ids(h) and json.loads(Path(h, 'logs', 'fault.json').read_text()).get('episode_id') is None, 'served, fault episode closed')
        no_downgrade(h)


def missing_or_invalid_probe_after_is_immediately_eligible():
    """Review r3 5.1: an active lower-tier runtime whose `probe_after` is missing or unparsable (migrated or
    interrupted state) probes the tiers above on the first turn after a restart, then carries a fresh probe_after
    one hour out; no indefinite stickiness."""
    for state in (None, 'not-a-time', ''):
        h, d = home(), fakes({f'claude-r2d2|{FABLE}': 'credits', f'claude-l|{FABLE}': 'credits'})
        sup = make(h, d)
        event(h, 'A')
        check(sup.run_once() is True and engine_file(h)['acc'] == 'codex', f'{state!r}: arrange: Astra')
        rt = S.read_engine_runtime(h)
        check(rt.get('active') and rt.get('probe_after'), f'{state!r}: arrange: an active episode with a probe pending: {rt}')
        if state is None:
            rt.pop('probe_after', None)
        else:
            rt['probe_after'] = state
        Path(h, 'engine-runtime.json').write_text(json.dumps(rt) + '\n')
        restarted = make(h, d)
        event(h, 'B')
        check(restarted.run_once() is True, f'{state!r}: turn B did not run')
        check(invoked(d)[3:] == [('claude-r2d2', FABLE), ('claude-l', FABLE), ('codex', ASTRA)],
              f'{state!r}: the tiers above are eligible at once after the restart: {invoked(d)[3:]}')
        rt = S.read_engine_runtime(h)
        due = (S.parse_iso(rt.get('probe_after')) - restarted.clock.now) if rt.get('probe_after') else None
        check(due is not None and HOUR - dt.timedelta(seconds=10) <= due <= HOUR, f'{state!r}: a fresh probe_after one hour out: {rt}')
        event(h, 'C')
        check(restarted.run_once() is True and invoked(d)[6:] == [('codex', ASTRA)], f'{state!r}: no second probe inside the hour: {invoked(d)[6:]}')
        no_downgrade(h)


SCENARIOS = [fault_notice_survives_crash_windows, move_notice_survives_crash_windows, fault_text_reaches_console_and_self_events, probe_due_during_fault_backoff_is_taken_on_the_retry, missing_or_invalid_probe_after_is_immediately_eligible, quota_then_fault_retries_the_unchanged_rotation, stated_reset_time_governs_the_probe, sticky_account_after_r2d2_out, l_out_tries_r2d2_then_both_out_lands_on_astra, hourly_probe_from_astra_returns_to_fable, session_error_on_l_changes_nothing, filtered_engine_accounts, mode_only_commands_enforce_policy, explicit_primary_reselection_closes_episode, alias_remapping_cannot_extend_allowlist, failure_success_persistence_matrix, persistent_model_switch_replaces_child, unavailable_reason_contract, fable_out_on_both_accounts_lands_on_astra, astra_out_lands_on_opus55, all_out_posts_one_unavailable_line,
             commands_refuse_outside_the_allowlist, stale_engine_file_never_runs_below_the_floor, status_line_contract,
             ladder_rotation_from_a_selected_lower_tier, prompt_too_long_skips_the_claude_family,
             legacy_claude_models_config_is_ignored, persistent_mode_covered, models_json_aliases,
             attach_refuses_outside_the_allowlist, configured_ladder_controls_execution_and_commands, documentation_and_help, policy_diagnostics_once_per_process, unavailable_notice_survives_crash_windows]


def main(argv):
    if argv and (len(argv) != 2 or argv[0] != '--only' or argv[1] not in {fn.__name__ for fn in SCENARIOS}):
        print('[b12.acceptance] FAIL: use --only with a known scenario name, or no arguments')
        return 2
    only = argv[1] if argv else None
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
