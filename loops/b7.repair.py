#!/usr/bin/env python3
"""Frozen repair regressions: isolated homes, fake engines, no live Slack/model."""
import runpy
from pathlib import Path
import json, sys, threading, logging
from types import SimpleNamespace, MethodType
from unittest.mock import patch
A=runpy.run_path(str(Path(__file__).with_name('b7.acceptance.py')))
S,B,H,home,write,event,fake_engine,HANDOFF=(A[k] for k in ('S','B','H','home','write','event','fake_engine','HANDOFF'))
class Crash(BaseException): pass

def status(h,turn=1,**kw):
    data=dict(turn=turn,mode='continue',track='dogfood',next_action='inspect evidence',channel='C',thread_ts='track-thread',message_ts='m')
    data.update(kw);write(Path(h)/'work-status.json',data);return data

def cap_recovery():
    h=home();now=[1000.0];status(h)
    def make():return S.Supervisor(home=h,clock=lambda:now[0],config={'continuation':{'max_per_hour':1}},poster=lambda *a:None)
    sup=make();hits=[]
    def crash(name):
        if name=='continuation_intent_saved':hits.append(name);raise Crash()
    with patch.object(sup,'persistence_checkpoint',side_effect=crash):
        try:sup.settle_work(1,[])
        except Crash:pass
    assert hits,'B1 missing actual intent-before-reservation checkpoint'
    now[0]=1100
    recovery_hits=[]
    def recovery_crash(self,name):
        if name=='continuation_reserved':recovery_hits.append(name);raise Crash()
    with patch.object(S.Supervisor,'persistence_checkpoint',recovery_crash):
        try:
            recovering=make()
            with patch.object(recovering,'turn',return_value=False):recovering.run_once()
        except Crash:pass
    assert recovery_hits,'B1 recovery did not expose durable reservation boundary'
    for _ in range(2):
        recovering=make()
        with patch.object(recovering,'turn',return_value=False):recovering.run_once()
    q=S.pending_events(h);assert [e['id'] for e in q]==['continue-1']
    reservations=S.read_jsonl(make().continuation_reservations_path())
    assert len(reservations)==1 and reservations[0]['at']==1000,'B1 reservation timestamp/count lost'
    sup.deliver({'slack':[],'cli':[]},['continue-1'],1)
    status(h,2);make().settle_work(2,[])
    assert not S.pending_events(h),'B1 hourly cap bypassed after recovery'

def delivery_settlement():
    for boundary in ('settlement_before_delivery','persist','settlement_completed','failed_delivery'):
        h=home();posted=[];status(h)
        extra=f"p=pathlib.Path({str(Path(h)/'engine-calls')!r});p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        engines=fake_engine(h,'repair reply\n'+HANDOFF,extra)
        fail_post=[boundary=='failed_delivery']
        def poster(*args):
            if fail_post[0]:raise RuntimeError('offline fixture')
            posted.append(args)
        def make():return S.Supervisor(home=h,engines=engines,poster=poster,config={'continuation':{'max_per_hour':1}})
        sup=make();S.append_event(h,event('origin',instructs=True));hits=[]
        def crash(name):
            if name==boundary:hits.append(name);raise Crash()
        if boundary=='persist':
            def crash_persist(*a):hits.append('persist');raise Crash()
            cm=patch.object(sup,'persist',side_effect=crash_persist)
        else:cm=patch.object(sup,'persistence_checkpoint',side_effect=crash)
        with cm:
            try:sup.run_once()
            except Crash:pass
        if boundary=='failed_delivery':
            assert Path(h,'inbox/pending-replies.jsonl').exists(),'B2 failed reply not retained'
            fail_post[0]=False
            recovery_hits=[]
            def recovery_crash(self,name):
                if name=='settlement_recovered':recovery_hits.append(name);raise Crash()
            with patch.object(S.Supervisor,'persistence_checkpoint',recovery_crash):
                try:make().run_once()
                except Crash:pass
            assert recovery_hits,'B2 recovered delivery boundary not reached'
        else:assert hits,'B2 missing boundary '+boundary
        for _ in range(2):
            restarted=make()
            with patch.object(restarted,'turn',return_value=False),patch.object(restarted,'compaction_due',return_value=(False,'')):
                restarted.run_once()
        assert Path(h,'engine-calls').read_text()=='x','B2 engine replayed'
        assert len([x for x in posted if x[1]=='origin' and 'repair reply' in x[2]])==1,'B2 confirmed reply lost/duplicated'
        assert [e['id'] for e in S.pending_events(h)]==['continue-1'],'B2 settlement lost/duplicated'

def rollover_service_recovery():
    for boundary in ('rollover_intent_saved','rollover_id_replaced','rollover_state_saved'):
        h=home();write(Path(h)/'config.json',{'compaction':{'rollover_enabled':True}})
        extra="import os\nm=pathlib.Path(os.environ['HYDRA_MEMORY_DIR'])/'MEMORY.md';m.write_text(m.read_text()+'\\nREPAIR_MEMORY\\n')\n"
        engines=fake_engine(h,'compacted\n'+HANDOFF,extra)
        def make():return S.Supervisor(home=h,engines=engines,poster=lambda *a:None,buildlog_poster=lambda *a:None)
        sup=make();sup.ensure_memory_layout();Path(h,'COMPACT').touch();hits=[]
        def crash(name):
            if name==boundary:hits.append(name);raise Crash()
        with patch.object(sup,'persistence_checkpoint',side_effect=crash):
            try:sup.run_compaction('forced')
            except Crash:pass
        assert hits,'B3 checkpoint not hit'
        intended=json.loads(Path(h,'rollover-intent.json').read_text())['new_id']
        recovery_hits=[]
        def recovery_crash(self,name):
            if name=='rollover_recovered':recovery_hits.append(name);raise Crash()
        with patch.object(S.Supervisor,'persistence_checkpoint',recovery_crash):
            try:make().run_once()
            except Crash:pass
        assert recovery_hits,'B3 recovery bookkeeping boundary not reached'
        for _ in range(2):
            restarted=make();restarted.run_once()
            assert restarted.session_id()==intended,'B3 replay changed intended UUID'
        assert Path(restarted.memory_dir,'MEMORY.md').read_text().count('REPAIR_MEMORY')==1,'B3 repeated memory turn'
        assert restarted.compaction_state().get('verify'),'B3 verification lost'
        assert not Path(h,'COMPACT').exists(),'B3 trigger not consumed'
        records=S.read_ledger(restarted.memory_dir)
        assert len([r for r in records if r.get('kind')=='compaction'])==1,'B3 successful memory bookkeeping lost/duplicated'

def stale_sdk_reconnect():
    from slack_sdk.socket_mode.client import BaseSocketModeClient
    import time
    for mode in ('silent','old-pong','fresh'):
        calls=[];offset=[0.0];stopped=threading.Event();started=time.monotonic()
        clock=lambda:time.monotonic()-started+offset[0]
        client=SimpleNamespace(connect_operation_lock=threading.Lock(),logger=logging.getLogger('repair'),
            socket_mode_request_listeners=[],auto_reconnect_enabled=True,default_auto_reconnect_enabled=True,
            is_connected=lambda:True,current_session=SimpleNamespace(last_ping_pong_time=1),
            issue_new_wss_url=lambda:(calls.append('url') or 'wss://fixture.invalid'))
        def connect():
            calls.append('connect')
            client.current_session=SimpleNamespace(last_ping_pong_time=1 if mode=='old-pong' else None)
            if mode=='fresh':
                threading.Timer(.005,lambda:setattr(client.current_session,'last_ping_pong_time',2)).start()
                threading.Timer(.03,stopped.set).start()
        client.connect=connect
        client.connect_to_new_endpoint=MethodType(BaseSocketModeClient.connect_to_new_endpoint,client)
        class Stop:
            def is_set(self):return stopped.is_set()
            def wait(self,seconds):
                if time.monotonic()-started>.4:raise AssertionError('B4 silent recovery did not fail within bound')
                if offset[0]==0:offset[0]=301
                return stopped.wait(.001)
        handler=SimpleNamespace(client=client,connect=lambda:None)
        failed=False
        try:B.run_socket_mode(handler,Stop(),clock=clock,poll_s=.001,reconnect_timeout_s=.05)
        except (RuntimeError,TimeoutError,SystemExit) as e:
            if isinstance(e,SystemExit):assert e.code not in (None,0)
            failed=True
        assert failed==(mode!='fresh'),'B4 fresh activity verdict wrong: '+mode
        assert calls==['url','connect'],'B4 must force exactly one bounded SDK reconnect: '+repr(calls)
        assert time.monotonic()-started<.4,'B4 recovery deadline exceeded'

def waiting_target_recovery():
    h=home();applied=set();failed=[True]
    def remove(*target):
        if target[1]=='old' and failed[0]:raise RuntimeError('offline')
        applied.discard(target)
    reactor=SimpleNamespace(add=lambda *t:applied.add(t),remove=remove)
    def make():return S.Supervisor(home=h,reactor=reactor,poster=lambda *a:None)
    sup=make();sup._set_desired_work_reaction(('C','old','timer_clock'))
    sup._set_desired_work_reaction(('C','new','timer_clock'))
    failed[0]=False;restarted=make();restarted._set_desired_work_reaction(None)
    for _ in range(2):make().run_once()
    assert not applied,'S1 old waiting clock orphaned across restart'

def incoming_turn_backoff():
    h=home();now=[1001.0];engines=fake_engine(h,'reply\n'+HANDOFF)
    # Real Git fixture, shared with the original frozen acceptance.
    import tempfile,subprocess
    root=Path(tempfile.mkdtemp(prefix='b7-repair-git-'));repo=root/'repo';remote=root/'remote.git'
    def git(*args):return subprocess.run(['git',*args],check=True,capture_output=True,text=True)
    git('init','--bare',str(remote));git('clone',str(remote),str(repo))
    (repo/'seed').write_text('seed');git('-C',str(repo),'add','.')
    git('-C',str(repo),'-c','user.name=fixture','-c','user.email=fixture@example.test','commit','-m','seed')
    git('-C',str(repo),'push','origin','HEAD')
    sup=S.Supervisor(home=h,repo=str(repo),engines=engines,clock=lambda:now[0],poster=lambda *a:None)
    sup._save_persistence_status(status='pending',retry_after=1060,last_turn=1)
    with patch.object(sup,'_git',wraps=sup._git) as wrapped:
        for i in range(2):S.append_event(h,event('turn'+str(i),instructs=True));assert sup.run_once()
        assert not wrapped.called,'S2 new turn bypassed backoff'
        assert sup._read_persistence_status()['retry_after']==1060,'S2 retry deadline changed'
        now[0]=1061;S.append_event(h,event('after',instructs=True));sup.run_once()
        assert any(c.args and c.args[0]=='fetch' for c in wrapped.call_args_list),'S2 retry never resumed'

def self_routing():
    h=home();posted=[];engines=fake_engine(h,'self progress\n'+HANDOFF)
    sup=S.Supervisor(home=h,engines=engines,poster=lambda *a:posted.append(a))
    status(h);sup.settle_work(1,[])
    queued=S.pending_events(h);assert len(queued)==1 and queued[0]['source']=='self','FLOOD auto event must be self'
    assert queued[0]['payload'].get('instructs') is False
    assert queued[0]['payload'].get('user')!='founder-console'
    assert sup.run_once()
    assert posted and all(x[1]=='track-thread' for x in posted),'FLOOD self reply escaped named thread'
    assert all('via console' not in x[2] and 'founder-console' not in x[2] for x in posted)
    # Missing destination must not fall back to the dev channel's top level.
    for payload in ({'text':'continue dogfood','track':'dogfood','instructs':False},
                    {'text':'continue dogfood','track':'dogfood','channel':'C','thread_ts':None,'instructs':False}):
        posted.clear();ev=S.new_event('self',payload)
        S.append_event(h,ev);assert sup.run_once();assert not posted,'FLOOD unthreaded self posted to Slack'
        assert ev['id'] in S.handled_ids(h)
        rows=S.read_jsonl(str(Path(h)/'logs/self-replies.jsonl'))
        assert any(r.get('id')==ev['id'] and 'self progress' in r.get('text','') for r in rows),'FLOOD local self reply lost'
    # Actual CLI entry: identical prompt text is still founder input.
    posted.clear()
    with patch.object(H,'supervisor_alive',return_value=False):assert H.cmd_say(h,['continue dogfood'])==0
    ev=S.pending_events(h)[0];assert ev['source']=='cli' and ev['payload']['user']=='founder-console' and ev['payload']['instructs'] is True
    assert sup.run_once()
    assert any(x[1] is None and 'from founder-console via console: continue dogfood' in x[2] for x in posted),'FLOOD founder CLI label changed'

def self_retry_mixed():
    for other in ('cli','slack'):
        h=home();posted=[];engines=fake_engine(h,'mixed progress\n'+HANDOFF)
        sup=S.Supervisor(home=h,engines=engines,poster=lambda *a:posted.append(a))
        S.append_event(h,S.new_event('self',{'text':'continue dogfood','track':'dogfood','channel':'C','thread_ts':'track','instructs':False}))
        if other=='cli':
            with patch.object(H,'supervisor_alive',return_value=False):H.cmd_say(h,['founder request'])
        else:S.append_event(h,event('human-thread',instructs=True))
        # Priority may process external and self in separate turns; both must finish.
        for _ in range(3):sup.run_once()
        assert len([x for x in posted if x[1]=='track'])==1,'FLOOD mixed self thread lost'
        tops=[x for x in posted if x[1] is None]
        if other=='cli':
            assert len(tops)==1 and 'from founder-console via console: founder request' in tops[0][2]
        else:assert not tops and len([x for x in posted if x[1]=='human-thread'])==1
        assert all('continue dogfood' not in x[2] for x in tops),'FLOOD self prompt mirrored as founder'
    h=home();posted=[];failed=[True]
    extra=f"p=pathlib.Path({str(Path(h)/'calls')!r});p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
    engines=fake_engine(h,'retry self progress\n'+HANDOFF,extra)
    def poster(*args):
        if failed[0]:raise RuntimeError('offline')
        posted.append(args)
    def make():return S.Supervisor(home=h,engines=engines,poster=poster)
    ev=S.new_event('self',{'text':'continue dogfood','track':'dogfood','channel':'C','thread_ts':'track','instructs':False})
    S.append_event(h,ev);make().run_once()
    assert Path(h,'inbox/pending-replies.jsonl').exists()
    failed[0]=False;make().run_once();make().run_once()
    assert Path(h,'calls').read_text()=='x','FLOOD delivery retry reran engine'
    assert len(posted)==1 and posted[0][1]=='track','FLOOD recovered reply escaped thread'

if __name__=='__main__':
    failures=[]
    for fn in (cap_recovery,delivery_settlement,rollover_service_recovery,stale_sdk_reconnect,waiting_target_recovery,incoming_turn_backoff,self_routing,self_retry_mixed):
        try:fn()
        except Exception as e:failures.append(fn.__name__);print('[b7 repair] FAIL',fn.__name__,type(e).__name__,str(e),flush=True)
        else:print('[b7 repair] OK',fn.__name__,flush=True)
    sys.exit(bool(failures))
