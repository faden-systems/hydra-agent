#!/usr/bin/env python3
"""Exit-owned Claude wire fixture: no network or real credentials."""
import json, os, signal, sys, time
from pathlib import Path
home = Path(os.environ['HYDRA_HOME'])
log = home / 'wire.jsonl'
def record(kind, **kw):
    with log.open('a') as f:
        f.write(json.dumps(dict(kind=kind, pid=os.getpid(), **kw))+'\n')
def emit(obj):
    print(json.dumps(obj), flush=True)
def arg(name, default=''):
    return sys.argv[sys.argv.index(name)+1] if name in sys.argv else default
sid = arg('--resume', arg('--session-id', 'scratch'))
record('start', argv=sys.argv[1:], sid=sid)
# Codex fixture follows its existing per-turn exec/output-file interface.
if len(sys.argv)>1 and sys.argv[1]=='exec':
    assert '--input-format' not in sys.argv
    text=sys.stdin.read()
    record('codex_request', text=text)
    Path(arg('-o')).write_text('fixture codex reply')
    record('exit')
    sys.exit(0)
# Fail before init/input; this must be distinguishable from a submitted turn.
fault = home / 'fail-stream-starts'
if arg('--input-format') == 'stream-json' and fault.exists():
    remaining = int(fault.read_text())
    if remaining > 0:
        fault.write_text(str(remaining - 1))
        record('startup_failure')
        sys.exit(23)
if '-p' not in sys.argv:
    record('interactive_enter', sid=sid)
    release=home/'attach-release'
    deadline=time.monotonic()+10
    while not release.exists():
        if time.monotonic()>deadline:sys.exit(91)
        time.sleep(.01)
    code=int(release.read_text());release.unlink()
    record('interactive_exit', code=code)
    sys.exit(code)
emit(dict(type='system', subtype='init', session_id=sid))
def hang_with_descendant():
    """A hang that leaks a TERM-resistant tool child (PR45 2.5): reaping must cover the process tree."""
    import subprocess
    child=subprocess.Popen([sys.executable,'-c',
        'import signal,time;[signal.signal(s,signal.SIG_IGN) for s in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP)];time.sleep(120)'])
    record('descendant', child=child.pid)
    signal.signal(signal.SIGINT, lambda *_: record('interrupt'))
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True: time.sleep(.05)

def serve(text):
    record('request', text=text)
    # Queued-event fault marker survives the real supervisor prompt wrapper.
    for fault_kind in ('CRASH', 'HANG'):
        if 'B8_QUEUED_'+fault_kind in text:
            record('side_effect', fault=fault_kind)
            if fault_kind=='CRASH': os._exit(17)
            hang_with_descendant()
    failure=home/'resume-error.json'
    if '--resume' in sys.argv and failure.exists():
        fault=json.loads(failure.read_text())
        if fault['sid']==sid:
            emit(dict(type='result',subtype='error_during_execution',is_error=True,
                      result=fault['error'],session_id=sid))
            return
    if 'WAIT_FOR_ATTACH' in text:
        deadline=time.monotonic()+5
        while not (home/'turn-release').exists():
            if time.monotonic()>deadline:sys.exit(92)
            time.sleep(.01)
        (home/'turn-release').unlink()
        record('busy_turn_released')
    if text == 'CRASH': os._exit(17)
    if text == 'HANG':
        hang_with_descendant()
    error = text == 'ERROR'
    message = dict(id=f'{os.getpid()}-{time.monotonic_ns()}', type='message', role='assistant',
                   content=[dict(type='text', text='reply:'+text)],
                   usage=dict(input_tokens=7, output_tokens=3, cache_creation_input_tokens=2, cache_read_input_tokens=11))
    emit(dict(type='assistant', message=message, session_id=sid))
    # A duplicate must not double accounting; terminal aggregate is not another request.
    emit(dict(type='assistant', message=message, session_id=sid))
    emit(dict(type='result', subtype='error_during_execution' if error else 'success',
              is_error=error, result='fixture real error' if error else 'reply:'+text,
              session_id=sid, usage=dict(input_tokens=7, output_tokens=3,
              cache_creation_input_tokens=2, cache_read_input_tokens=11)))
if arg('--input-format') == 'stream-json':
    for line in sys.stdin:
        obj=json.loads(line)
        assert obj['type']=='user' and obj['message']['role']=='user'
        content=obj['message']['content']
        text=content if isinstance(content,str) else ''.join(x.get('text','') for x in content)
        serve(text)
else:
    serve(sys.stdin.read())
record('exit')
