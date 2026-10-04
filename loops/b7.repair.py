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
    for _ in range(2):make()._reconcile_continuation_intent()
    q=S.pending_events(h);assert [e['id'] for e in q]==['continue-1']
    reservations=S.read_jsonl(make().continuation_reservations_path())
    assert len(reservations)==1 and reservations[0]['at']==1000,'B1 reservation timestamp/count lost'
    sup.deliver({'slack':[],'cli':[]},['continue-1'],1)
    status(h,2);make().settle_work(2,[])
    assert not S.pending_events(h),'B1 hourly cap bypassed after recovery'

def delivery_settlement():
    for boundary in ('settlement_before_delivery','persist','settlement_completed'):
        h=home();posted=[];status(h)
        extra=f"p=pathlib.Path({str(Path(h)/'engine-calls')!r});p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
        engines=fake_engine(h,'repair reply\n'+HANDOFF,extra)
        def make():return S.Supervisor(home=h,engines=engines,poster=lambda *a:posted.append(a),config={'continuation':{'max_per_hour':1}})
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
        assert hits,'B2 missing boundary '+boundary
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
    calls=[]
    client=SimpleNamespace(connect_operation_lock=threading.Lock(),logger=logging.getLogger('repair'),
        is_connected=lambda:True,current_session=SimpleNamespace(last_ping_pong_time=None),
        issue_new_wss_url=lambda:(calls.append('url') or 'wss://fixture.invalid'),connect=lambda:calls.append('connect'))
    client.connect_to_new_endpoint=MethodType(BaseSocketModeClient.connect_to_new_endpoint,client)
    # A silent replacement may correctly raise after its bounded grace period.
    try:B._reconnect_or_die(client,.05)
    except (RuntimeError,TimeoutError):pass
    assert calls==['url','connect'],'B4 SDK connected-but-stale reconnect was a no-op'

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
        assert not any(c.args and c.args[0]=='fetch' for c in wrapped.call_args_list),'S2 new turn bypassed backoff'
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
    # Actual CLI entry: identical prompt text is still founder input.
    posted.clear()
    with patch.object(H,'supervisor_alive',return_value=False):assert H.cmd_say(h,['continue dogfood'])==0
    ev=S.pending_events(h)[0];assert ev['source']=='cli' and ev['payload']['user']=='founder-console' and ev['payload']['instructs'] is True
    assert sup.run_once()
    assert any(x[1] is None and 'from founder-console via console: continue dogfood' in x[2] for x in posted),'FLOOD founder CLI label changed'

if __name__=='__main__':
    failures=[]
    for fn in (cap_recovery,delivery_settlement,rollover_service_recovery,stale_sdk_reconnect,waiting_target_recovery,incoming_turn_backoff,self_routing):
        try:fn()
        except Exception as e:failures.append(fn.__name__);print('[b7 repair] FAIL',fn.__name__,type(e).__name__,str(e),flush=True)
        else:print('[b7 repair] OK',fn.__name__,flush=True)
    sys.exit(bool(failures))
