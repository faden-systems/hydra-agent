#!/usr/bin/env python3
"""Exit-owned offline b7 contracts. No real Slack, credentials, or model calls."""
import importlib.machinery
import json
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
    return {'id': ts, 'source': 'slack', 'payload': {'channel': 'C', 'thread_ts': ts,
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
        'cache_creation': {'ephemeral_5m_input_tokens': 20}, 'output_tokens': 8}}}
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
    assert '--verbose' in args and args[args.index('--output-format')+1] == 'stream-json'

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
    before = {str(p):p.read_bytes() for p in repo.rglob('*.md')}
    S.migrate_track_history(h, str(repo))
    assert before == {str(p):p.read_bytes() for p in repo.rglob('*.md')}, 'migration duplicated history'
    # Simulate death after archives were committed but before compact state replace.
    write(Path(h)/'state.json', state)
    S.migrate_track_history(h, str(repo))
    assert before == {str(p):p.read_bytes() for p in repo.rglob('*.md')}, 'crash retry duplicated archive'
    assert json.loads((Path(h)/'state.json').read_text()) == updated
    summary = S.tracks_summary(updated)
    assert len(summary) == 2 and all('\n' not in s and len(s) <= 250 for s in summary), summary
    text = S.status_text(h, str(repo))
    assert all(line in text.splitlines() for line in summary), 'status needs separate track lines'
    assert 'old: waiting' not in text
    write(Path(h)/'state.json', {'tracks':{'b':{'now':'running','stage':'old\nhistory'}}})
    assert S.tracks_summary(json.loads((Path(h)/'state.json').read_text())) == ['b: running']
    write(Path(h)/'state.json', {'tracks':[{'id':'valid','stage':'must not archive yet'},{'id':'../escape','stage':'secret history'}]})
    old = (Path(h)/'state.json').read_bytes()
    archives={str(p):p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    try: S.migrate_track_history(h,str(repo))
    except ValueError: pass
    else: raise AssertionError('unsafe id accepted')
    assert (Path(h)/'state.json').read_bytes() == old
    assert archives=={str(p):p.read_bytes() for p in repo.rglob('*') if p.is_file()}

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
    code='import sys,json;sys.path.insert(0,sys.argv[1]);import supervisor as S;print(json.dumps(S.post_receipt(sys.argv[2],sys.argv[3])))'
    fresh=subprocess.check_output([sys.executable,'-c',code,str(ROOT/'manager'),h,item['id']],text=True)
    assert json.loads(fresh)['status']=='posted', 'receipt was not durable before removal'
    S.drain_outbox(h,lambda *a:calls.append(a))
    assert len(calls)==1 and not S.read_outbox(h), 'receipt retry reposted accepted Slack message'
    for receipt,code in (({'status':'posted','ts':'done'},0),({'status':'failed','error':'fixture'},1)):
        with patch.object(B,'bridge_alive',return_value=True), patch.object(S,'post_receipt',return_value=receipt):
            assert H.cmd_post(h,['C','1','receipt'])==code
    # No queue record and no receipt is not success; emulate a CLI verification race.
    with patch.object(B,'bridge_alive',return_value=True), patch.object(S,'read_outbox',return_value=[]), \
         patch.object(S,'post_receipt',return_value=None), patch.dict(os.environ,{'HYDRA_POST_TIMEOUT':'.01'}):
        assert H.cmd_post(h,['C','1','raced']) == 1, 'CLI falsely inferred delivery from absence'

if __name__=='__main__':
    if '--serve-child' in sys.argv: serve_child()
    failures=[]
    for fn in (liveness,context,tracks,empty,blocks,outbox):
        try: fn()
        except (AssertionError, Exception) as exc:
            failures.append(fn.__name__); print(f'[b7] FAIL {fn.__name__}: {type(exc).__name__}: {exc}',flush=True)
        else: print(f'[b7] OK {fn.__name__}',flush=True)
    if failures: sys.exit(1)
    print('[b7 acceptance] PASS')
