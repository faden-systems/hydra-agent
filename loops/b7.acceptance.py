#!/usr/bin/env python3
"""Exit-owned offline b7 contracts. No real Slack, credentials, or model calls."""
import importlib.machinery
import json
import io
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'manager'))
import supervisor as S
import bridge as B
H = importlib.machinery.SourceFileLoader('hydra_cli_b7', str(ROOT / 'manager/hydra')).load_module()

def home():
    p = Path(tempfile.mkdtemp(prefix='b7-'))
    for name in ('logs', 'inbox', 'mirror'):
        (p / name).mkdir()
    return str(p)

def write(p, value):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(value))

def event(ts, **kw):
    return {'id': ts, 'source': 'slack', 'payload': {'channel': 'C', 'thread_ts': ts, 'ts': ts,
            'text': 'report', 'instructs': False, 'addressed': False, **kw}}

def liveness():
    assert hasattr(B, 'SocketHealth'), 'b7 SocketHealth missing'
    now = [0.0]
    client = SimpleNamespace(current_session=SimpleNamespace(last_ping_pong_time=None), is_connected=lambda: True)
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    assert health.check() is True
    now[0] = 299; assert health.check() is True
    now[0] = 300; assert health.check() is False, 'established but silent must recover'
    health.note_envelope(); assert health.check() is True
    now[0] = 599; assert health.check() is True
    now[0] = 600; assert health.check() is False
    client.current_session.last_ping_pong_time = 1234567
    assert health.check() is True, 'observed pong refreshes monotonic activity'
    now[0] = 900; assert health.check() is False, 'same pong cannot refresh every poll'
    client.is_connected = lambda: False
    health.note_envelope(); assert health.check() is False, 'disconnected is immediately unhealthy'
    assert hasattr(B, 'run_socket_mode'), 'service monitoring seam missing'
    class Client:
        def __init__(self):
            self.socket_mode_request_listeners = []
            self.current_session = SimpleNamespace(last_ping_pong_time=None)
            self.auto_reconnect_enabled = True
            self.default_auto_reconnect_enabled = True
            self.connected = False
            self.reconnects = 0
        def is_connected(self): return self.connected
        def connect_to_new_endpoint(self):
            self.reconnects += 1
            raise RuntimeError('offline-fixture')
        def close(self): pass
    class Handler:
        def __init__(self): self.client = Client()
        def connect(self): pass
        def close(self): self.client.close()
        def start(self): raise AssertionError('blocking start is forbidden')
    handler = Handler()
    try:
        B.run_socket_mode(handler, threading.Event(), poll_s=.001, reconnect_timeout_s=.05)
    except SystemExit as exc:
        assert exc.code not in (None, 0)
    except RuntimeError:
        pass
    else:
        raise AssertionError('failed reconnect must escape service main')
    assert handler.client.reconnects == 1
    assert handler.client.socket_mode_request_listeners, 'service must wire real SDK receipt seam'
    # A successful reconnect returns to monitoring without a second recovery; a
    # timeout or a disconnected return must fail the service main thread.
    for mode in ('success', 'disconnected', 'timeout'):
        handler = Handler(); stop = threading.Event(); release = threading.Event()
        def recover(mode=mode, client=handler.client):
            client.reconnects += 1
            if mode == 'timeout': release.wait(1)
            elif mode == 'success':
                client.connected = True
                threading.Timer(.02, stop.set).start()
        handler.client.connect_to_new_endpoint = recover
        started = time.monotonic()
        try:
            B.run_socket_mode(handler, stop, poll_s=.001, reconnect_timeout_s=.05)
        except (RuntimeError, TimeoutError, SystemExit) as exc:
            assert mode != 'success', 'successful reconnect was fatal'
            if isinstance(exc, SystemExit): assert exc.code not in (0, None)
        else:
            assert mode == 'success', f'{mode} reconnect incorrectly succeeded'
        finally:
            release.set()
        assert time.monotonic() - started < .5, 'reconnect bound not enforced'
        assert handler.client.reconnects == 1, 'recovery loop repeats without monitoring'
        assert not handler.client.auto_reconnect_enabled
        assert not handler.client.default_auto_reconnect_enabled
    # Actual serve/main subprocess, actual Bolt handler dispatch/ACK, fake network only.
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--serve-child'],
                           capture_output=True, text=True, timeout=10)
    assert child.returncode != 0, 'production bridge main must fail recovery'
    for marker in ('DISPATCHED', 'ACKED', 'CLOSED', 'PUMP_STOPPED', 'MAIN_FAILED', 'RECOVERY_ATTEMPT'):
        assert marker in child.stdout, (marker, child.stdout, child.stderr)
    # Old timestamps on replacement sessions cannot buy endless freshness.
    client.is_connected = lambda: True
    now[0] = 1201
    client.current_session = SimpleNamespace(last_ping_pong_time=1234567)
    assert health.check() is False
    now[0] = 1502
    client.current_session = SimpleNamespace(last_ping_pong_time=1234567)
    assert health.check() is False
    client.current_session.last_ping_pong_time = 1234568
    assert health.check() is True

def serve_child():
    import logging
    import slack_bolt
    import slack_bolt.adapter.socket_mode.builtin as adapter
    from slack_bolt.response import BoltResponse
    from slack_sdk.socket_mode.request import SocketModeRequest
    h=home(); write(Path(h)/'allowlist.json', {'U':{'instructs':True}})
    (Path(h)/'credentials').mkdir()
    (Path(h)/'credentials/slack.env').write_text('SLACK_BOT_TOKEN=fake\nSLACK_APP_TOKEN=fake\n')
    class App:
        def __init__(self, **kw):
            self.logger=logging.getLogger('b7'); self.events={}
            self.client=SimpleNamespace(proxy=None, auth_test=lambda:{'user_id':'M'})
        def event(self,name):
            def register(fn): self.events[name]=fn; return fn
            return register
        def dispatch(self,req):
            self.events['message'](req.body['event'])
            print('DISPATCHED', flush=True)
            return BoltResponse(status=200,body='')
    class Client:
        def __init__(self,**kw):
            self.logger=logging.getLogger('b7-client')
            self.socket_mode_request_listeners=[]
            self.current_session=SimpleNamespace(last_ping_pong_time=None)
            self.auto_reconnect_enabled=self.default_auto_reconnect_enabled=True
        def connect(self):
            req=SocketModeRequest(type='events_api', envelope_id='env', payload={'event':{
                'type':'message','user':'U','channel':'C','ts':'child','text':'<@M> report'}})
            for listener in list(self.socket_mode_request_listeners): listener(self,req)
        def is_connected(self): return False
        def connect_to_new_endpoint(self):
            print('RECOVERY_ATTEMPT',flush=True)
            raise RuntimeError('fixture recovery failure')
        def send_socket_mode_response(self,response): print('ACKED',flush=True)
        def close(self): print('CLOSED',flush=True)
        def disconnect(self): self.close()
    def pump(self,stop):
        stop.wait(5)
        assert stop.is_set(), 'pump not stopped'
        print('PUMP_STOPPED',flush=True)
    with patch.object(slack_bolt,'App',App), patch.object(adapter,'SocketModeClient',Client), \
         patch.object(B.Bridge,'pump_outbox',pump):
        try: rc=B.main(['--home',h])
        except SystemExit as exc: rc=exc.code or 0
        for thread in threading.enumerate():
            if thread.name=='outbox': thread.join(.5); assert not thread.is_alive()
        queued=S.read_jsonl(str(Path(h)/'inbox/events.jsonl'))
        assert queued and queued[0]['payload']['addressed'], 'dispatch did not reach production bridge'
        assert rc != 0
        print('MAIN_FAILED',flush=True)
        raise SystemExit(rc)

def context():
    first = {'type': 'assistant', 'message': {'id': 'msg1', 'usage': {'input_tokens': 100,
        'cache_creation_input_tokens': 20, 'cache_read_input_tokens': 1000,
        'cache_creation': {'ephemeral_5m_input_tokens': 20}, 'output_tokens': 8},
        'content':[{'type':'tool_use','id':'tool1','name':'read','input':{}}]}}
    later = {'type': 'assistant', 'message': {'id': 'msg2', 'usage': {'input_tokens': 900000,
        'cache_read_input_tokens': 900000, 'output_tokens': 30}}}
    result = {'type': 'result', 'result': 'done', 'session_id': 's', 'usage': {
        'input_tokens': 2000000, 'cache_read_input_tokens': 3000000, 'output_tokens': 60}}
    out, usage, err, sid = S.Supervisor._parse_claude_output('\n'.join(map(json.dumps, [first, first, later, result])))
    assert out == 'done' and sid == 's' and not err, 'JSONL first-request stream not parsed'
    assert usage.get('context_tokens') == 1120, usage
    assert usage.get('input_tokens') == 5000000, 'accounting must retain aggregate'
    _, unknown, _, _ = S.Supervisor._parse_claude_output(json.dumps(result))
    assert unknown.get('context_tokens') is None, 'aggregate alone is not context'
    first['message']['usage'] = {'input_tokens': 0, 'cache_read_input_tokens': 0}
    _, zero, _, _ = S.Supervisor._parse_claude_output('\n'.join(map(json.dumps,[first,result])))
    assert zero.get('context_tokens') == 0
    first['message']['usage'] = {'input_tokens': -1}
    _, bad, _, _ = S.Supervisor._parse_claude_output('\n'.join(map(json.dumps,[first,later,result])))
    assert bad.get('context_tokens') is None, 'later valid request must not replace invalid first request'
    for invalid in (True, float('inf'), float('nan')):
        first['message']['usage']={'input_tokens':invalid}
        _,bad,_,_=S.Supervisor._parse_claude_output('\n'.join(map(json.dumps,[first,later,result])))
        assert bad.get('context_tokens') is None, invalid
    del first['message']['usage']
    _,missing,_,_=S.Supervisor._parse_claude_output('\n'.join(map(json.dumps,[first,later,result])))
    assert missing.get('context_tokens') is None, 'missing first usage must remain unknown'
    first['message']['usage']={'input_tokens':7}
    error_result={**result,'is_error':True}
    _,valid,error,sid=S.Supervisor._parse_claude_output('broken line\n'+json.dumps(first)+'\n'+json.dumps(error_result))
    assert valid.get('context_tokens')==7 and error and sid=='s'
    # Exercise actual invocation + schedule/verification through run_once, not only parser.
    h = home(); exe = Path(h)/'fake-claude'
    first['message']['usage'] = {'input_tokens': 100, 'cache_read_input_tokens': 1000}
    reply = 'done\n---HANDOFF---\ntracks: b\nwaiting on: none\nlast decision: test\nnext action: none\nopen question: none'
    result['result'] = reply
    lines = '\n'.join(map(json.dumps,[first,later,result]))
    exe.write_text('#!/usr/bin/env python3\nimport sys,json\nsys.stdin.read()\n'
        + f'open({str(Path(h)/"argv.json")!r},"w").write(json.dumps(sys.argv))\nprint({lines!r})\n')
    exe.chmod(0o755)
    write(Path(h)/'engine', {'acc':'claude-r2d2','model':'claude-fable-5-1'})
    write(Path(h)/'config.json', {'compaction': {'threshold_tokens': 300000}})
    S.append_event(h, event('100'))
    sup = S.Supervisor(home=h, engines={'claude-r2d2': {'bin':str(exe), 'cred':None}}, poster=lambda *a:None)
    assert sup.run_once()
    row = S.read_jsonl(str(Path(h)/'logs/turns.jsonl'))[-1]
    assert row.get('context_tokens') == 1100 and not sup.compaction_pending(), row
    # An old aggregate-only stream after compaction must be unverified, not failed.
    state = sup.compaction_state()
    state['verify'] = {'before_tokens': 400000, 'at': 'fixture', 'reason': 'tokens'}
    sup.save_compaction_state(state)
    exe.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint(' + repr(json.dumps(result)) + ')\n')
    S.append_event(h, event('101'))
    assert sup.run_once()
    assert sup.compaction_state()['last']['ok'] is None, 'aggregate falsely verified compaction'
    assert not sup.compaction_pending(), 'aggregate scheduled another compaction'
    args = json.loads((Path(h)/'argv.json').read_text())
    for measured, expected in ((1000, True), (400000, False)):
        mh=home(); mexe=Path(mh)/'engine-fixture'
        first['message']['usage']={'input_tokens':measured}
        result['usage']={'input_tokens':9000000}
        mexe.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint(' +
            repr('\n'.join(map(json.dumps,[first,result]))) + ')\n'); mexe.chmod(0o755)
        write(Path(mh)/'engine',{'acc':'claude-r2d2','model':'claude-fable-5-1'})
        ms=S.Supervisor(home=mh,engines={'claude-r2d2':{'bin':str(mexe),'cred':None}},poster=lambda *a:None)
        cs=ms.compaction_state(); cs['verify']={'before_tokens':500000,'at':'fixture','reason':'tokens'}
        ms.save_compaction_state(cs); S.append_event(mh,event('measured'))
        assert ms.run_once()
        assert ms.compaction_state()['last']['ok'] is expected
        assert ms.compaction_pending() is (not expected), 'high context must schedule; low context must not'
    assert '--include-partial-messages' not in args
    assert '--verbose' in args and args[args.index('--output-format')+1] == 'stream-json'
    lh=home(); ls=S.Supervisor(home=lh)
    S.append_jsonl(str(Path(lh)/'logs/turns.jsonl'),{'engine':'claude-r2d2','input_tokens':9000000})
    assert ls.last_claude_context_tokens() is None
    usage_run=subprocess.run([sys.executable,str(ROOT/'manager/hydra'),'usage'],env={**os.environ,'HYDRA_HOME':lh},capture_output=True,text=True)
    assert usage_run.returncode==0 and 'context_tokens=unknown' in usage_run.stdout
    S.append_jsonl(str(Path(lh)/'logs/turns.jsonl'),{'engine':'claude-r2d2','input_tokens':8000000,'context_tokens':1234})
    assert ls.last_claude_context_tokens()==1234
    usage_run=subprocess.run([sys.executable,str(ROOT/'manager/hydra'),'usage'],env={**os.environ,'HYDRA_HOME':lh},capture_output=True,text=True)
    assert usage_run.returncode==0 and 'context_tokens=1234' in usage_run.stdout

def tracks():
    assert hasattr(S, 'migrate_track_history'), 'track migration missing'
    h = home(); repo = Path(tempfile.mkdtemp(prefix='b7-repo-'))
    history = 'old: waiting\n2026-10-02 deployed after verification'
    state = {'schema_version':2, 'other': {'preserve':True}, 'tracks': [
        {'id':'flow', 'stage':history, 'now':'deployed; evidence pending', 'owner':'manager'},
        {'id':'R1', 'stage':'old\n' + 'last ' * 100}]}
    write(Path(h)/'state.json', state)
    original=(Path(h)/'state.json').read_bytes()
    S.migrate_track_history(h, None)
    assert (Path(h)/'state.json').read_bytes()==original, 'no-repo migration must retain state untouched'
    S.migrate_track_history(h, str(repo))
    updated = json.loads((Path(h)/'state.json').read_text())
    tr = updated['tracks'][0]
    assert tr['now'] == 'deployed; evidence pending' and tr['stage'] == tr['now']
    assert tr['history_file'] == 'factory/log/tracks/flow.md'
    archive = repo/tr['history_file']; assert history in archive.read_text()
    assert updated['other'] == state['other'] and tr['owner'] == 'manager'
    for item in updated['tracks']:
        assert len(item['now'])<=240 and '\n' not in item['now'] and item['stage']==item['now']
    assert updated['tracks'][1]['now'].endswith(('…','...'))
    cli_home=home();write(Path(cli_home)/'state.json',state);write(Path(cli_home)/'config.json',{'repo':str(repo)})
    cli=subprocess.run([sys.executable,str(ROOT/'manager/hydra'),'migrate-tracks'],env={**os.environ,'HYDRA_HOME':cli_home},capture_output=True,text=True)
    assert cli.returncode==0 and '2' in cli.stdout,(cli.stdout,cli.stderr)
    assert json.loads(Path(cli_home,'state.json').read_text())==updated
    before = {str(p):p.read_bytes() for p in repo.rglob('*.md')}
    S.migrate_track_history(h, str(repo))
    assert before == {str(p):p.read_bytes() for p in repo.rglob('*.md')}, 'migration duplicated history'
    # Interrupt the actual atomic state replacement and observe archive-before-state ordering.
    crash_home=home(); crash_repo=Path(tempfile.mkdtemp(prefix='b7-crash-repo-'))
    write(Path(crash_home)/'state.json',state);old_bytes=Path(crash_home,'state.json').read_bytes()
    replace=os.replace; interrupted=[]
    def crash_replace(src,dst,*args,**kwargs):
        if Path(dst)==Path(crash_home)/'state.json':
            interrupted.append(True)
            assert history in (crash_repo/'factory/log/tracks/flow.md').read_text()
            assert state['tracks'][1]['stage'] in (crash_repo/'factory/log/tracks/R1.md').read_text()
            assert Path(crash_home,'state.json').read_bytes()==old_bytes
            raise OSError('injected state replace failure')
        return replace(src,dst,*args,**kwargs)
    with patch.object(S.os,'replace',side_effect=crash_replace):
        try:S.migrate_track_history(crash_home,str(crash_repo))
        except OSError:pass
    assert interrupted and Path(crash_home,'state.json').read_bytes()==old_bytes
    saved={str(p.relative_to(crash_repo)):p.read_bytes() for p in crash_repo.rglob('*.md')}
    S.migrate_track_history(crash_home,str(crash_repo))
    assert saved=={str(p.relative_to(crash_repo)):p.read_bytes() for p in crash_repo.rglob('*.md')}
    assert json.loads(Path(crash_home,'state.json').read_text())==updated
    summary = S.tracks_summary(updated)
    assert len(summary) == 2 and all('\n' not in s and len(s) <= 250 for s in summary), summary
    text = S.status_text(h, str(repo))
    assert all(line in text.splitlines() for line in summary), 'status needs separate track lines'
    assert 'old: waiting' not in text
    newer=json.loads((Path(h)/'state.json').read_text()); newer['tracks'][0]['stage']='new-unseen-history'
    write(Path(h)/'state.json',newer); S.migrate_track_history(h,str(repo))
    appended=archive.read_text(); assert history in appended and appended.count('new-unseen-history')==1
    S.migrate_track_history(h,str(repo)); assert archive.read_text()==appended
    write(Path(h)/'state.json', {'tracks':{'b' :{'now':'running','stage':'old\nhistory'}}})
    assert S.tracks_summary(json.loads((Path(h)/'state.json').read_text())) == ['b: running']
    dh=home(); dr=Path(tempfile.mkdtemp(prefix='b7-dict-'))
    ds={'other':{'keep':1},'tracks':{'b':{'now':'running','stage':'dict old\nhistory','owner':'manager'}}}
    write(Path(dh)/'state.json',ds);write(Path(dh)/'config.json',{'repo':str(dr)})
    cli_args=[sys.executable,str(ROOT/'manager/hydra'),'migrate-tracks']
    env={**os.environ,'HYDRA_HOME':dh}
    result=subprocess.run(cli_args,env=env,capture_output=True,text=True)
    assert result.returncode==0,(result.stdout,result.stderr)
    du=json.loads(Path(dh,'state.json').read_text());dt=du['tracks']['b']
    assert du['other']==ds['other'] and dt['owner']=='manager'
    assert dt['now']==dt['stage']=='running'
    da=dr/dt['history_file'];assert 'dict old\nhistory' in da.read_text()
    saved=da.read_bytes();state_saved=Path(dh,'state.json').read_bytes()
    S.migrate_track_history(dh,str(dr))
    result=subprocess.run(cli_args,env=env,capture_output=True,text=True)
    assert result.returncode==0 and da.read_bytes()==saved
    assert Path(dh,'state.json').read_bytes()==state_saved
    write(Path(h)/'state.json', {'tracks':[{'id':'valid','stage':'must not archive yet'},{'id':'../escape','stage':'secret history'}]})
    old = (Path(h)/'state.json').read_bytes()
    archives={str(p):p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    try: S.migrate_track_history(h,str(repo))
    except ValueError: pass
    else: raise AssertionError('unsafe id accepted')
    assert (Path(h)/'state.json').read_bytes() == old
    assert archives=={str(p):p.read_bytes() for p in repo.rglob('*') if p.is_file()}

def fake_engine(h,reply,extra=''):
    exe=Path(h)/'fake-claude'
    exe.write_text('#!/usr/bin/env python3\nimport sys,json,pathlib\nprompt=sys.stdin.read()\n'+
        f'pathlib.Path({str(Path(h)/"prompt")!r}).write_text(prompt)\n'+extra+
        'print('+repr(reply)+')\n')
    exe.chmod(0o755); write(Path(h)/'engine',{'acc':'claude-r2d2','model':'claude-fable-5-1'})
    return {'claude-r2d2':{'bin':str(exe),'cred':None}}

HANDOFF='---HANDOFF---\ntracks: b7\nwaiting on: none\nlast decision: fixture\nnext action: none\nopen question: none'

def migration_integration():
    h=home(); repo=Path(tempfile.mkdtemp(prefix='b7-git-'))
    remote=Path(tempfile.mkdtemp(prefix='b7-bare-'))
    def git(*args): subprocess.run(['git',*args],cwd=repo,check=True,capture_output=True)
    git('init','--bare',str(remote)); git('init'); git('config','user.name','Fixture'); git('config','user.email','fixture@example.test')
    git('commit','--allow-empty','-m','base'); git('remote','add','origin',str(remote)); git('push','-u','origin','HEAD')
    write(Path(h)/'state.json',{'tracks':[{'id':'b7','stage':'BEFORE-PROMPT-HISTORY\nlatest'}]})
    extra=f"p=pathlib.Path({str(Path(h)/'state.json')!r}); data=json.loads(p.read_text()); data['tracks'][0]['stage']='AFTER-ENGINE-HISTORY'; p.write_text(json.dumps(data))\n"
    engines=fake_engine(h,'done\n'+HANDOFF,extra)
    sup=S.Supervisor(home=h,repo=str(repo),engines=engines,poster=lambda *a:None)
    S.append_event(h,event('integration')); assert sup.run_once()
    prompt=(Path(h)/'prompt').read_text()
    assert 'BEFORE-PROMPT-HISTORY' not in prompt and 'latest' in prompt and 'history_file' in prompt, 'prompt saw verbose state'
    state=json.loads((Path(h)/'state.json').read_text())
    assert state==json.loads((repo/'factory/state.json').read_text())
    assert state['tracks'][0]['stage']==state['tracks'][0]['now']
    archive=(repo/'factory/log/tracks/b7.md').read_text()
    assert 'BEFORE-PROMPT-HISTORY' in archive and 'AFTER-ENGINE-HISTORY' in archive

def empty():
    h = home(); posted=[]
    sup=S.Supervisor(home=h, poster=lambda *args:posted.append(args))
    events=[event('1'),event('2',instructs=True),event('3',addressed=True)]
    plan=sup.plan_deliveries(events,' \n\t',99)
    assert len(plan['slack']) == 2, plan
    assert {i['thread_ts'] for i in plan['slack']} == {'2','3'}
    assert all(i['text']=='(turn produced no reply text)' for i in plan['slack'])
    shared=[event('share'),event('share',addressed=True),event('other')]
    grouped=sup.plan_deliveries(shared,'',100)['slack']
    assert len(grouped)==1 and grouped[0]['thread_ts']=='share'
    assert sup.deliver({'slack':[{'channel':'C','thread_ts':'legacy','text':' \n'}], 'cli':[]}, ['old'],99)
    assert not posted, 'legacy pending blank reached poster'
    eh=home(); outputs=[]; removed=[]; logs=[]
    reactor=SimpleNamespace(add=lambda *a:None,remove=lambda *a:removed.append(a))
    es=S.Supervisor(home=eh,engines=fake_engine(eh,HANDOFF),poster=lambda *a:outputs.append(a),reactor=reactor)
    mixed=[event('indirect'),event('direct',instructs=True),
           S.new_event('cli',{'text':'console prompt','channel':'C'},event_id='console')]
    for ev in mixed: S.append_event(eh,ev)
    with patch.object(S,'log',side_effect=lambda msg:logs.append(str(msg))): assert es.run_once()
    assert set(S.handled_ids(eh))=={'indirect','direct','console'} and not S.pending_events(eh)
    assert any('delivery skipped' in line for line in logs) and any('placeholder sent' in line for line in logs)
    assert {args[1] for args in removed} >= {'indirect','direct'}
    assert 'tracks: b7' in Path(es.handoff_path()).read_text()
    assert (Path(eh)/'inbox/replies/console.txt').read_text()=='\n'
    assert len([p for p in outputs if p[1]=='direct' and p[2]=='(turn produced no reply text)'])==1
    assert not any(p[1]=='indirect' for p in outputs) and all(p[2].strip() for p in outputs)

def blocks():
    h=home(); b=B.Bridge(h, {'BOT': {'instructs':False}}, lambda *a:None, {}, bot_user_id='MANAGER')
    ev={'type':'message','subtype':'bot_message','channel':'C','ts':'10','bot_id':'BOT','text':'',
        'blocks':[{'type':'rich_text','elements':[{'type':'rich_text_section','elements':[
            {'type':'user','user_id':'MANAGER'},{'type':'text','text':' iOS build ready '},
            {'type':'link','url':'https://example.test/report','text':'report'}]}]}]}
    queued=b.handle_message(ev)
    assert queued and queued['payload']['addressed'] and queued['payload']['instructs'] is False, queued
    text=queued['payload']['text']; assert '<@MANAGER>' in text and 'iOS build ready' in text and 'report' in text
    assert S.read_jsonl(str(Path(h)/'mirror/C.jsonl'))[-1]['text']==text
    assert b.joined('C','10')
    ev['ts']='families'
    ev['blocks']=[{'type':'rich_text','elements':[
        {'type':'rich_text_list','elements':[{'type':'rich_text_section','elements':[
            {'type':'user','user_id':'MANAGER'},{'type':'text','text':' list-marker '}, {'type':'emoji','name':'tada'}]}]},
        {'type':'rich_text_quote','elements':[{'type':'text','text':' quote-marker '}]},
        {'type':'rich_text_preformatted','elements':[{'type':'text','text':' code-marker '}]}]},
        {'type':'section','text':{'type':'mrkdwn','text':'section-marker'}},
        {'type':'context','elements':[{'type':'plain_text','text':'context-marker'}]}]
    rich=b.handle_message(ev); assert rich and rich['payload']['addressed']
    for marker in ('list-marker','tada','quote-marker','code-marker','section-marker','context-marker'):
        assert marker in rich['payload']['text'], marker
    assert S.read_jsonl(str(Path(h)/'mirror/C.jsonl'))[-1]['text']==rich['payload']['text']
    # Human assignment and bot command routing consume the normalized blocks too.
    posted=[]; human=B.Bridge(h,{'U':{'instructs':True}},lambda *a:posted.append(a),{},bot_user_id='MANAGER')
    assigned={'channel':'C','ts':'assigned','user':'U','text':'','blocks':[
        {'type':'section','text':{'type':'plain_text','text':'assignee: manager | track: ios'}}]}
    routed=human.handle_message(assigned)
    assert routed and routed['payload']['instructs'] and human.joined('C','assigned')
    assigned['ts']='command'; assigned['blocks']=[{'type':'section','text':{
        'type':'mrkdwn','text':'<@MANAGER> status'}}]
    command=human.handle_message(assigned)
    assert command and command.get('command')=='status' and posted
    ev['ts']='11'; ev['text']='authoritative plain text'
    assert b.handle_message(ev) is None, 'blocks must not override nonblank text to wake a bot'
    ev['ts']='12'; ev['text']=''; ev['bot_id']='STRANGER'
    assert b.handle_message(ev) is None, 'blocks must not bypass allowlist'
    ev['ts']='13'; ev['blocks']=[None,{'type':'rich_text','elements':[None,{'type':'unknown'}]}]
    assert b.handle_message(ev) is None

def outbox():
    h=home(); p=Path(h)/'race.jsonl'; p.write_text('{}\n')
    real_open=open
    def raced_open(file,*a,**kw):
        if str(file)==str(p):
            p.unlink(missing_ok=True)
        return real_open(file,*a,**kw)
    with patch('builtins.open', raced_open):
        assert S.read_jsonl(str(p)) == [], 'unlink between exists/open must not raise'
    assert hasattr(S,'post_receipt'), 'durable post receipt missing'
    rec=S.queue_post(h,'C','1','hello')
    assert S.post_receipt(h,rec['id']) is None
    S.drain_outbox(h,lambda *a:{'ok':True,'ts':'123'})
    ack=S.post_receipt(h,rec['id'])
    assert ack and ack['status']=='posted' and ack.get('ts')=='123', ack
    assert not S.read_outbox(h)
    # Receipt survives process reopening and interruption before queue removal.
    item=S.queue_post(h,'C','2','interrupted'); calls=[]
    def interrupted(home,items):
        # Queue removal must remain protected by the existing lock.
        lock_code="import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
        held=subprocess.run([sys.executable,'-c',lock_code,S.outbox_path(home)+'.lock'],capture_output=True)
        assert held.returncode != 0, 'queue removal outside outbox lock'
        raise InterruptedError('after durable receipt, before removal')
    with patch.object(S,'_write_outbox',interrupted):
        try: S.drain_outbox(h,lambda *a:(calls.append(a) or {'ts':'456'}))
        except InterruptedError: pass
        else: raise AssertionError('interruption seam not reached')
    receipt_probe='import sys,json;sys.path.insert(0,sys.argv[1]);import supervisor as S;print(json.dumps(S.post_receipt(sys.argv[2],sys.argv[3])))'
    fresh=subprocess.check_output([sys.executable,'-c',receipt_probe,str(ROOT/'manager'),h,item['id']],text=True)
    assert json.loads(fresh)['status']=='posted', 'receipt was not durable before removal'
    S.drain_outbox(h,lambda *a:calls.append(a))
    assert len(calls)==1 and not S.read_outbox(h), 'receipt retry reposted accepted Slack message'
    for receipt,code in (({'status':'posted','ts':'done'},0),({'status':'failed','error':'fixture'},1)):
        with patch.object(B,'bridge_alive',return_value=True), patch.object(S,'post_receipt',return_value=receipt):
            assert H.cmd_post(h,['C','1','receipt'])==code
    fh=home(); failed=S.queue_post(fh,'C','3','cannot post')
    def fail_post(*args): raise RuntimeError('terminal failure fixture')
    for _ in range(S.OUTBOX_MAX_ATTEMPTS): S.drain_outbox(fh,fail_post)
    fresh=subprocess.check_output([sys.executable,'-c',receipt_probe,
        str(ROOT/'manager'),fh,failed['id']],text=True)
    failure=json.loads(fresh); assert failure['status']=='failed' and 'terminal failure fixture' in failure.get('error','')
    captured=io.StringIO()
    with patch.object(B,'bridge_alive',return_value=True),patch.object(S,'queue_post',return_value=failed),contextlib.redirect_stderr(captured),contextlib.redirect_stdout(captured):
        assert H.cmd_post(fh,['C','3','cannot post'])==1
    assert 'terminal failure fixture' in captured.getvalue()
    # No queue record and no receipt is not success; emulate a CLI verification race.
    with patch.object(B,'bridge_alive',return_value=True), patch.object(S,'read_outbox',return_value=[]), \
         patch.object(S,'post_receipt',return_value=None), patch.dict(os.environ,{'HYDRA_POST_TIMEOUT':'.01'}):
        assert H.cmd_post(h,['C','1','raced']) == 1, 'CLI falsely inferred delivery from absence'

def engine_errors():
    assert hasattr(S,'classify_engine_error'), 'missing precise error classifier'
    credit="You're out of usage credits. Switch to another model to continue."
    for text,kind in [(credit,'credits'),('usage limit reached','usage_limit'),('rate limit HTTP 429','rate_limit'),
            ('HTTP 401 Unauthorized','auth'),('status 403 forbidden','auth'),('Prompt is too long','prompt_too_long'),
            ('Prompt is too long; automatic compaction failed: '+credit,'credits'),
            ('generate a separate report','other'),('record 14013 failed','other')]:
        assert S.classify_engine_error(text)==kind, (text,kind)
    h=home(); exe=Path(h)/'error-claude'; sup=S.Supervisor(home=h,poster=lambda *a:None)
    Path(h,'session-id').write_text('original\n')
    for stream in (False,True):
        terminal=json.dumps({'type':'result','is_error':True,'result':credit,'session_id':'bad-id'})
        output=(json.dumps({'type':'system','subtype':'init'})+'\n' if stream else '')+terminal
        exe.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint('+repr(output)+')\nsys.exit(1)\n');exe.chmod(0o755)
        rc,out,err,usage=sup.invoke('claude-r2d2',{'bin':str(exe),'cred':None},'fixture','claude-sonnet-5')
        assert rc!=0 and credit in err, (rc,out,err)
        assert Path(h,'session-id').read_text()=='original\n', 'failed invocation replaced session'


def engine_fallback():
    h=home(); write(Path(h)/'engine',{'acc':'claude-r2d2','model':'claude-fable-5-1'})
    cfg={'dev_channel':'C_FIXTURE_DEV','engine_fallback':{'claude_models':['claude-sonnet-5']}}
    engines={'claude-r2d2':{'bin':'fixture'},'claude-l':{'bin':'fixture'},'codex':{'bin':'fixture','kind':'codex'}}
    sup=S.Supervisor(home=h,engines=engines,config=cfg,poster=lambda *a:None)
    calls=[]; credit="You're out of usage credits. Switch to another model to continue."
    def invoke(name,spec,message,model=None,preamble=''):
        calls.append((name,model))
        if model=='claude-fable-5-1': return 1,'',credit,None
        return 0,'ok\n'+HANDOFF,'',{'context_tokens':10}
    with patch.object(sup,'invoke',side_effect=invoke):
        result=sup.run_engines('fixture',[])
    assert calls==[('claude-r2d2','claude-fable-5-1'),('claude-r2d2','claude-sonnet-5')], calls
    assert result[:2]==('claude-r2d2','claude-sonnet-5')
    assert S.read_engine(h,engines)['model']=='claude-sonnet-5'
    attempts=S.read_jsonl(str(Path(h)/'logs/attempts.jsonl'))
    assert len(attempts)==2 and attempts[0]['classification']=='credits' and attempts[1]['success']
    assert credit in attempts[0]['reason']
    runtime=json.loads(Path(h,'engine-runtime.json').read_text())
    assert runtime.get('since') and credit in runtime.get('reason','')
    notices=S.read_outbox(h); assert len(notices)==1, notices
    assert not notices[0].get('thread_ts'), 'fallback notice must be top-level'
    assert notices[0]['channel']=='C_FIXTURE_DEV' and 'fallback started' in notices[0]['text'].lower()
    episode=runtime.get('episode_id'); assert episode and episode in notices[0]['text']
    sup2=S.Supervisor(home=h,engines=engines,config=cfg,poster=lambda *a:None)
    with patch.object(sup2,'invoke',side_effect=invoke): sup2.run_engines('next',[])
    assert len(S.read_outbox(h))==1, 'restart duplicated fallback alert'
    status=S.status_text(h)
    assert 'configured' in status.lower() and 'effective' in status.lower() and 'claude-sonnet-5' in status and 'credits' in status
    S.set_engine(h,'claude-r2d2','claude-fable-5-1',engines)
    with patch.object(sup2,'invoke',return_value=(0,'recovered','',None)):
        sup2.run_engines('recovered',[]); sup2.run_engines('again',[])
    assert len(S.read_outbox(h))==2, 'missing or repeated recovery alert'
    end=S.read_outbox(h)[1]
    assert end['channel']=='C_FIXTURE_DEV' and not end.get('thread_ts')
    assert 'fallback ended' in end['text'].lower() and episode in end['text']
    # All Claude models fail: successful Codex fallback persists, then no Claude retry next turn.
    h2=home();write(Path(h2)/'engine',{'acc':'claude-r2d2','model':'claude-fable-5-1'})
    sx=S.Supervisor(home=h2,engines=engines,config=cfg,poster=lambda *a:None); seen=[]
    def cross(name,spec,message,model=None,preamble=''):
        seen.append((name,model))
        return (0,'ok','',None) if name=='codex' else (1,'',credit,None)
    with patch.object(sx,'invoke',side_effect=cross): sx.run_engines('first',[])
    assert S.read_engine(h2,engines)['acc']=='codex' and len(seen)==len(set(seen)), seen
    seen.clear()
    with patch.object(sx,'invoke',side_effect=cross): sx.run_engines('second',[])
    assert len(seen)==1 and seen[0][0]=='codex', seen


def ledger_failures():
    h=home();sup=S.Supervisor(home=h,poster=lambda *a:None);sup.ensure_memory_layout()
    p=Path(sup.memory_dir)/'LEDGER.jsonl'
    rows=[{'at':'2026-10-02T08:00:00Z','engine':'claude-r2d2','kind':'turn'},
          {'at':'2026-10-02T09:00:00Z','engine':'codex','kind':'turn'},
          {'at':'2026-10-02T10:00:00Z','engine':'claude-r2d2','kind':'compaction','compaction':{'ok':False}},
          {'at':'2026-10-02T11:00:00Z','engine':'claude-r2d2','kind':'attempt','success':False},
          {'at':'2026-10-02T12:00:00Z','engine':'claude-r2d2','error':'credits'}]
    for row in rows:S.append_jsonl(str(p),row)
    original=p.read_bytes()
    assert sup.family_last_at('claude')==rows[0]['at']
    assert sup.switch_for('codex') is None
    preamble=sup.preamble_for('codex')
    assert rows[1]['at'] in preamble and rows[-1]['at'] not in preamble, preamble
    with patch.object(sup,'transcript_source',return_value=('fixture-transcript','fixture')), \
         patch.object(S,'flatten_codex',return_value=[]) as flatten:
        _,record=sup.transition_read('codex','claude','2026-10-02T13:00:00Z')
    assert record['since']==rows[0]['at']
    assert flatten.call_args.args[1]==rows[0]['at'], 'transition window advanced by failed attempt'
    assert p.read_bytes()==original


def rollover():
    # Real invoke/argv on temp homes only; never inspect the manager's real transcript.
    for mode in ('success','failure','held','default','empty-config','no-handoff','bad-handoff','no-memory','identical-memory','memory-error','id-error','rollover_intent_saved','rollover_id_replaced','rollover_state_saved'):
        h=home();exe=Path(h)/'rollover-claude';old='old-fixture-session'
        Path(h,'session-id').write_text(old+'\n')
        transcript=Path(h)/'.claude/projects'/h.replace('/','-')/(old+'.jsonl')
        transcript.parent.mkdir(parents=True);transcript.write_bytes(b'OLD TRANSCRIPT SENTINEL\n')
        write(Path(h)/'engine',{'acc':'claude-r2d2','model':'claude-sonnet-5'})
        if mode=='empty-config':write(Path(h)/'config.json',{'compaction':{}})
        elif mode!='default':write(Path(h)/'config.json',{'compaction':{'rollover_enabled':mode!='held'}})
        handoff=HANDOFF if mode not in ('no-handoff','bad-handoff') else ('---HANDOFF---\ntracks: incomplete' if mode=='bad-handoff' else '')
        memory_code=" m=pathlib.Path(os.environ['HYDRA_MEMORY_DIR'])/'MEMORY.md';m.write_text(m.read_text()+'\\nrollover memory marker\\n')\n"
        if mode=='no-memory':memory_code=' pass\n'
        if mode=='identical-memory':memory_code=" m=pathlib.Path(os.environ['HYDRA_MEMORY_DIR'])/'MEMORY.md';m.write_bytes(m.read_bytes())\n"
        if mode=='memory-error':memory_code=" raise OSError('fixture memory write failed')\n"
        if mode=='failure':memory_code=' sys.exit(1)\n'
        exe.write_text('#!/usr/bin/env python3\nimport sys,json,os,pathlib\nprompt=sys.stdin.read()\n'
            +f'p=pathlib.Path({h!r})\n'
            +"with (p/'calls').open('a') as f:f.write(json.dumps({'argv':sys.argv,'prompt':prompt})+'\\n')\n"
            +"if (p/'fail-start').exists():sys.exit(1)\n"
            +"if '[compaction]' in prompt:\n"+memory_code
            +"print("+repr('compacted\n'+handoff)+")\n")
        exe.chmod(0o755);engines={'claude-r2d2':{'bin':str(exe),'cred':None}}
        sup=S.Supervisor(home=h,engines=engines,poster=lambda *a:None,buildlog_poster=lambda *a:None)
        sup.ensure_memory_layout()
        interrupted=[]
        if mode.startswith('rollover_'):
            assert hasattr(sup,'persistence_checkpoint'), 'rollover crash checkpoint missing'
            def stop(name):
                if name==mode:interrupted.append(name);raise SystemExit('fixture process death')
            with patch.object(sup,'persistence_checkpoint',side_effect=stop):
                try:sup.run_compaction('force')
                except SystemExit:pass
            assert interrupted==[mode]
            intent=json.loads(Path(h,'rollover-intent.json').read_text());intended=intent['new_id']
            assert intent['old_id']==old
            recovered=S.Supervisor(home=h,engines=engines,poster=lambda *a:None)
            rc,*_=recovered.invoke('claude-r2d2',engines['claude-r2d2'],'recover','claude-sonnet-5',preamble=recovered.preamble_for('claude-r2d2'))
            assert rc==0
            calls=[json.loads(x) for x in Path(h,'calls').read_text().splitlines()]
            assert len(calls)==2 and '--session-id' in calls[-1]['argv'] and '--resume' not in calls[-1]['argv']
            new=Path(h,'session-id').read_text().strip();assert new==intended and new!=old and new in calls[-1]['argv']
            assert recovered.compaction_state()['last']['new_id']==intended
            assert transcript.read_bytes()==b'OLD TRANSCRIPT SENTINEL\n'
            continue
        if mode=='id-error':
            assert hasattr(sup,'replace_session_id'), 'atomic session replacement seam missing'
            with patch.object(sup,'replace_session_id',side_effect=OSError('fixture atomic replace failure')):sup.run_compaction('force')
        else:sup.run_compaction('force')
        assert transcript.read_bytes()==b'OLD TRANSCRIPT SENTINEL\n'
        sid=Path(h,'session-id').read_text().strip()
        if mode!='success':
            assert sid==old,mode
            if mode=='id-error':
                restarted=S.Supervisor(home=h,engines=engines,poster=lambda *a:None)
                rc,*_=restarted.invoke('claude-r2d2',engines['claude-r2d2'],'after caught failure','claude-sonnet-5')
                assert rc==0 and Path(h,'session-id').read_text().strip()==old
                call=json.loads(Path(h,'calls').read_text().splitlines()[-1])
                assert '--resume' in call['argv'] and old in call['argv']
            cs=sup.compaction_state()
            if mode in ('held','default','empty-config'):
                assert not Path(h,'calls').exists()
                assert 'held' in cs.get('last',{}).get('error','').lower(),cs
            else:
                assert cs.get('failed_at') and cs.get('last',{}).get('ok') is False,(mode,cs)
                if mode in ('failure','memory-error'):assert sup.family_last_at('claude') is None
            continue
        import uuid
        uuid.UUID(sid);assert sid!=old
        record=sup.compaction_state()['last']
        assert record['old_id']==old and record['new_id']==sid and record['ok'] is None,record
        sup.verify_compaction(None)
        assert sup.compaction_state()['last']['ok'] is None
        sup.verify_compaction(10)
        record=sup.compaction_state()['last']
        assert record['ok'] is True and record['old_id']==old and record['new_id']==sid,record
        # Fail first startup, restart again, retry fresh, then resume after success.
        Path(h,'fail-start').touch()
        for fail in (True,False,False):
            restarted=S.Supervisor(home=h,engines=engines,poster=lambda *a:None)
            rc,*_=restarted.invoke('claude-r2d2',engines['claude-r2d2'],'next','claude-sonnet-5',preamble=restarted.preamble_for('claude-r2d2'))
            assert (rc!=0)==fail
            if fail:Path(h,'fail-start').unlink()
        calls=[json.loads(x) for x in Path(h,'calls').read_text().splitlines()]
        assert all('/compact' not in c['argv'] for c in calls)
        for call in calls[-3:-1]:
            argv=call['argv'];assert '--session-id' in argv and '--resume' not in argv and sid in argv
        assert '--resume' in calls[-1]['argv'] and sid in calls[-1]['argv']
        assert 'MEMORY.md' in calls[-1]['prompt'] and 'tracks: b7' in calls[-1]['prompt']


def attribution():
    h=home();posted=[]
    b=B.Bridge(h,{'U':{'instructs':True},'OP':{'instructs':False}},lambda *a:posted.append(a),{},bot_user_id='MANAGER')
    for n,footer in enumerate(('\n*Sent using* <@APP>',' *Sent using* <@APP|Claude>')):
        result=b.handle_message({'channel':'C','ts':str(n),'user':'U','text':'<@MANAGER> engine acc=codex model=gpt6'+footer})
        assert result and result.get('ok'), posted
        assert S.read_engine(h)['model']=='gpt-6-astra'
    before=Path(h,'engine').read_bytes()
    for n,text,user in [('op','<@MANAGER> engine acc=claude-l model=sonnet5\n*Sent using* <@APP>','OP'),
            ('bad','<@MANAGER> engine acc=claude-l\ninvalid extra\n*Sent using* <@APP>','U'),
            ('inside','<@MANAGER> engine acc=claude-l *Sent using* <@APP> trailing','U'),
            ('quote','<@MANAGER> engine acc=claude-l\n> *Sent using* <@APP>','U'),
            ('fence','<@MANAGER> engine acc=claude-l\n```\n*Sent using* <@APP>','U')]:
        result=b.handle_message({'channel':'C','ts':n,'user':user,'text':text})
        assert result and result.get('ok') is False
        assert Path(h,'engine').read_bytes()==before


def fallback_matrix():
    engines={'claude-r2d2':{'bin':'fixture'},'claude-l':{'bin':'fixture'},'codex':{'bin':'fixture','kind':'codex'}}
    for kind,error,models in [
            ('usage_limit','Usage limit. Switch to another model to continue.',['claude-sonnet-5']),
            ('credits',"Out of usage credits. Switch to another model to continue.",[]),
            ('duplicates',"Out of usage credits. Switch to another model to continue.",['claude-fable-5-1','claude-sonnet-5','claude-sonnet-5']),
            ('auth','HTTP 401 Unauthorized',['claude-sonnet-5']),
            ('prompt','Prompt is too long',['claude-sonnet-5']),
            ('rate','Rate limit 429',['claude-sonnet-5']),
            ('other','generate a separate report failed',['claude-sonnet-5'])]:
        h=home();S.set_engine(h,'claude-r2d2','claude-fable-5-1',engines);seen=[]
        sup=S.Supervisor(home=h,engines=engines,config={'engine_fallback':{'claude_models':models}},poster=lambda *a:None)
        def inv(name,spec,message,model=None,preamble=''):
            seen.append((name,model))
            return (0,'done','',None) if name=='codex' else (1,'',error,None)
        with patch.object(sup,'invoke',side_effect=inv):sup.run_engines('matrix',[])
        first=('claude-r2d2','claude-fable-5-1');other=('claude-l','claude-fable-5-1');codex=('codex','gpt-6-astra')
        expected=[first,other,codex]
        if kind in ('usage_limit','duplicates'):
            expected=[first,('claude-r2d2','claude-sonnet-5'),other,('claude-l','claude-sonnet-5'),codex]
        if kind=='prompt':expected=[first,codex]
        assert seen==expected,(kind,seen)
        expected_acc='codex' if kind in ('credits','usage_limit','duplicates','auth') else 'claude-r2d2'
        assert S.read_engine(h,engines)['acc']==expected_acc,kind
    h=home();S.set_engine(h,'codex','gpt-6-astra',engines)
    sup=S.Supervisor(home=h,engines=engines,poster=lambda *a:None)
    with patch.object(sup,'invoke',return_value=(0,'deliberately selected','',None)):sup.run_engines('codex',[])
    assert not S.read_outbox(h),'explicit Codex selection must not announce fallback'


def failure_diagnostics():
    for stream in (False,True):
        h=home();exe=Path(h)/'diagnostics';Path(h,'session-id').write_text('old\n')
        sup=S.Supervisor(home=h,poster=lambda *a:None)
        for body,stderr,needle in [
                ({'type':'result','result':'looks successful','is_error':False},'process failed','process failed'),
                ({'type':'result','error':{'message':'usage limit reached'},'is_error':True},'stderr-marker','usage limit reached')]:
            out=json.dumps(body)
            if stream:out=json.dumps({'type':'system','subtype':'init'})+'\n'+out
            exe.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint('+repr(out)+')\nprint('+repr(stderr)+',file=sys.stderr)\nsys.exit(1)\n');exe.chmod(0o755)
            rc,_,err,_=sup.invoke('claude-r2d2',{'bin':str(exe),'cred':None},'fixture','claude-sonnet-5')
            assert rc!=0 and needle in err and stderr in err and len(err)<=4000,err
            assert Path(h,'session-id').read_text()=='old\n'


def atomic_session_replace():
    h=home();sup=S.Supervisor(home=h,poster=lambda *a:None)
    assert hasattr(sup,'replace_session_id'), 'atomic session replacement seam missing'
    path=Path(h,'session-id');path.write_bytes(b'ORIGINAL-ID\n');called=[];replace=os.replace
    def fail(src,dst,*args,**kwargs):
        if Path(dst)==path:
            called.append(True);assert path.read_bytes()==b'ORIGINAL-ID\n'
            raise OSError('injected session replace failure')
        return replace(src,dst,*args,**kwargs)
    with patch.object(S.os,'replace',side_effect=fail):
        try:sup.replace_session_id('new-fixture-id')
        except OSError:pass
        else:raise AssertionError('session replacement swallowed write failure')
    assert called and path.read_bytes()==b'ORIGINAL-ID\n'


def explicit_fallback_selection():
    h=home();engines={'claude-r2d2':{'bin':'fixture'},'codex':{'bin':'fixture','kind':'codex'}}
    S.set_engine(h,'claude-r2d2','claude-sonnet-5',engines)
    cfg={'dev_channel':'C_EPISODE','engine_fallback':{'claude_models':[]}}
    def supervisor():return S.Supervisor(home=h,engines=engines,config=cfg,poster=lambda *a:None)
    def invoke(name,spec,message,model=None,preamble=''):
        return (0,'ok','',None) if name=='codex' else (1,'','Out of usage credits',None)
    sup=supervisor()
    with patch.object(sup,'invoke',side_effect=invoke):sup.run_engines('first',[])
    episode=json.loads(Path(h,'engine-runtime.json').read_text())['episode_id']
    assert len(S.read_outbox(h))==1
    S.set_engine(h,'claude-r2d2','claude-sonnet-5',engines)
    def auth(name,spec,message,model=None,preamble=''):
        return (0,'ok','',None) if name=='codex' else (1,'','HTTP 401 Unauthorized after explicit selection',None)
    sup=supervisor()
    with patch.object(sup,'invoke',side_effect=auth):sup.run_engines('failed explicit',[])
    runtime=json.loads(Path(h,'engine-runtime.json').read_text())
    assert runtime['episode_id']==episode and '401' in runtime['reason']
    assert len(S.read_outbox(h))==1, 'failed explicit selection falsely closed/reopened episode'
    S.set_engine(h,'codex','gpt-6-astra',engines)
    sup=supervisor()
    with patch.object(sup,'invoke',return_value=(0,'chosen effective','',None)):
        sup.run_engines('close',[]);sup.run_engines('again',[])
    notices=S.read_outbox(h);assert len(notices)==2
    assert episode in notices[-1]['text'] and 'fallback ended' in notices[-1]['text'].lower()
    assert notices[-1]['channel']=='C_EPISODE' and not notices[-1].get('thread_ts')


def notice_crashes():
    for checkpoint in ('fallback_start_saved','fallback_start_queued','fallback_end_saved','fallback_end_queued'):
        h=home(); engines={'claude-r2d2':{'bin':'fixture'},'codex':{'bin':'fixture','kind':'codex'}}
        cfg={'dev_channel':'C_CRASH','engine_fallback':{'claude_models':[]}}
        S.set_engine(h,'claude-r2d2','claude-fable-5-1',engines)
        def make():return S.Supervisor(home=h,engines=engines,config=cfg,poster=lambda *a:None)
        def invoke(name,*a,**kw):return (0,'ok','',None) if name=='codex' else (1,'','Out of usage credits',None)
        sup=make();assert hasattr(sup,'persistence_checkpoint'),'notice checkpoint missing'
        ending='_end_' in checkpoint
        if ending:
            with patch.object(sup,'invoke',side_effect=invoke):sup.run_engines('start',[])
            S.set_engine(h,'codex','gpt-6-astra',engines)
            sup=make()
        hits=[]
        def stop(name):
            if name==checkpoint:hits.append(name);raise SystemExit('fixture process death')
        with patch.object(sup,'invoke',side_effect=invoke),patch.object(sup,'persistence_checkpoint',side_effect=stop):
            try:sup.run_engines('crash',[])
            except SystemExit:pass
        assert hits==[checkpoint],hits
        runtime=json.loads(Path(h,'engine-runtime.json').read_text())
        delivered=[]
        if checkpoint.endswith('_queued'):
            queued=S.read_outbox(h);assert queued
            S.drain_outbox(h,lambda *a:(delivered.append(a) or {'ok':True,'ts':str(len(delivered))}))
            assert not S.read_outbox(h)
            assert all(S.post_receipt(h,n['id'])['status']=='posted' for n in queued)
        for _ in range(2):
            sup=make()
            with patch.object(sup,'invoke',side_effect=invoke):sup.run_engines('recover',[])
        notices=S.read_outbox(h)
        if checkpoint.endswith('_queued'):
            assert not notices,'recovery requeued an already delivered notice'
            S.drain_outbox(h,lambda *a:(delivered.append(a) or {'ok':True,'ts':'duplicate'}))
            assert len(delivered)==(2 if ending else 1),'recovery reposted delivered transition'
            notices=queued
        assert len(notices)==(2 if ending else 1),(checkpoint,notices)
        assert sum('fallback started' in n['text'].lower() for n in notices)==1
        if ending:assert sum('fallback ended' in n['text'].lower() for n in notices)==1
        assert all(n['channel']=='C_CRASH' and not n.get('thread_ts') for n in notices)
        # Start and end must carry the same stable episode id.
        ids=[n['text'] for n in notices]
        if not ending:assert runtime['episode_id'] in ids[0]


if __name__=='__main__':
    if '--serve-child' in sys.argv: serve_child()
    failures=[]
    for fn in (liveness,context,tracks,migration_integration,empty,blocks,outbox,engine_errors,engine_fallback,ledger_failures,rollover,attribution,fallback_matrix,failure_diagnostics,atomic_session_replace,explicit_fallback_selection,notice_crashes):
        try: fn()
        except (AssertionError, Exception) as exc:
            failures.append(fn.__name__); print(f'[b7] FAIL {fn.__name__}: {type(exc).__name__}: {exc}',flush=True)
        else: print(f'[b7] OK {fn.__name__}',flush=True)
    if failures: sys.exit(1)
    print('[b7 acceptance] PASS')
