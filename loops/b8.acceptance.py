#!/usr/bin/env python3
"""Exit-owned b8 integration contracts."""
import datetime, hashlib, json, os, runpy, shutil, signal, sys, tempfile, threading, time
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'manager'))
import supervisor as S

def lifecycle():
    with tempfile.TemporaryDirectory(prefix='b8-offline-') as folder:
        home=Path(folder); binary=home/'fake-claude'
        shutil.copyfile(ROOT/'loops/b8.fake.py', binary); binary.chmod(0o755)
        engines={name:{'bin':str(binary),'kind':'claude','cred':name+'.env'} for name in ('claude-r2d2','claude-l')}
        (home/'credentials').mkdir()
        for name in engines:
            token='dummy-'+name
            cred=home/'credentials'/engines[name]['cred']
            cred.write_text('CLAUDE_CODE_OAUTH_TOKEN='+token+'\n');cred.chmod(0o600)
            Path(str(cred)+'.identity.json').write_text(json.dumps({'schema_version':1,'label':name,
                'account_id':'fixture-'+name,'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
                'verified_at':'2026-10-04T20:00:00Z','method':'private-window-and-usage-bar',
                'evidence':'https://app.slack.com/archives/Cfixture/p123'}))
        (home/'engine').write_text(json.dumps({'acc':'claude-r2d2','model':'claude-sonnet-5','mode':'persistent'}))
        alerts=[]
        def post(*args):
            alerts.append(args[-1])
            return {'ok':True,'ts':str(time.time())}
        sup=S.Supervisor(home=folder,engines=engines,poster=post,
                         buildlog_poster=post,engine_timeout=.5)
        def wire():
            p=home/'wire.jsonl'
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        def call(text, account='claude-r2d2', model='claude-sonnet-5'):
            return sup.invoke(account,engines[account],text,model=model)
        try:
            for i in range(12):
                rc,out,err,usage=call('turn-'+str(i))
                assert rc==0 and out=='reply:turn-'+str(i),(rc,out,err)
                assert usage['context_tokens']==20,usage
            starts=[r for r in wire() if r['kind']=='start']
            assert len(starts)==1, 'successive turns must share one process'
            assert len([r for r in wire() if r['kind']=='request'])==12
            sid=starts[0]['sid']; old=starts[0]['pid']
            live=S.persistent_status(folder)
            assert live['state']=='running' and live['pid']==old
            assert live['account']=='claude-r2d2' and live['model']=='claude-sonnet-5'
            assert live['turns_served']==12 and live['uptime_s']>=0
            rendered=S.status_text(folder)
            assert all(word in rendered for word in ('persistent','uptime','12','claude-sonnet-5'))
            rc,out,err,_=call('ERROR')
            assert rc!=0 and 'fixture real error' in err,(rc,out,err)
            assert call('after-error')[0]==0
            assert len([r for r in wire() if r['kind']=='start'])==1
            assert call('switch','claude-l')[0]==0
            starts=[r for r in wire() if r['kind']=='start']
            assert len(starts)==2 and starts[-1]['sid']==sid
            try:os.kill(old,0)
            except ProcessLookupError:pass
            else:raise AssertionError('old account process not reaped before switch')
            assert call('CRASH','claude-l')[0]!=0
            assert S.persistent_status(folder)['state']!='running','dead process reported healthy'
            assert call('after-crash','claude-l')[0]==0
            begin=time.monotonic();assert call('HANG','claude-l')[0]!=0
            assert time.monotonic()-begin<4, 'hang cleanup exceeded timeout plus grace'
            assert call('after-hang','claude-l')[0]==0
            # Model changes and explicit rollback also close the previous writer.
            before=[r for r in wire() if r['kind']=='start']
            assert call('model-switch','claude-l','claude-fable-5-1')[0]==0
            after=[r for r in wire() if r['kind']=='start']
            assert len(after)==len(before)+1 and after[-1]['sid']==sid
            model_args=after[-1]['argv']
            assert model_args[model_args.index('--model')+1]=='claude-fable-5-1'
            try:os.kill(before[-1]['pid'],0)
            except ProcessLookupError:pass
            else:raise AssertionError('model switch retained old writer')
            (home/'engine').write_text(json.dumps({'acc':'claude-l','model':'claude-sonnet-5','mode':'per-turn'}))
            assert call('rollback-1','claude-l')[0]==0
            assert call('rollback-2','claude-l')[0]==0
            rolled=[r for r in wire() if r['kind']=='start']
            assert len(rolled)==len(after)+2
            assert all('--input-format' not in r['argv'] for r in rolled[-2:])
            assert all(r['sid']==sid for r in rolled[-2:])
            # Two pre-init failures allow one per-turn request, with no replay.
            (home/'engine').write_text(json.dumps({'acc':'claude-l','model':'claude-sonnet-5','mode':'persistent'}))
            (home/'fail-stream-starts').write_text('2')
            start_count=len(rolled); alert_count=len(alerts)
            assert call('startup-fallback','claude-l')[0]==0
            fallback=[r for r in wire() if r['kind']=='start'][start_count:]
            assert len(fallback)==3, fallback
            assert all('--input-format' in r['argv'] for r in fallback[:2])
            assert '--input-format' not in fallback[-1]['argv']
            notices=[str(x).lower() for x in alerts[alert_count:]]
            assert len(notices)==1 and 'per-turn' in notices[0], notices
            assert json.loads((home/'engine').read_text())['mode']=='persistent'
            assert call('startup-recovered','claude-l')[0]==0
            recovered=[r for r in wire() if r['kind']=='start']
            assert len(recovered)==start_count+4
            assert '--input-format' in recovered[-1]['argv']
            assert call('recovered-reuse','claude-l')[0]==0
            assert len([r for r in wire() if r['kind']=='start'])==len(recovered)
            # Drive the production idle tick with a controlled clock, not a timer stub.
            tick=[datetime.datetime(2026,10,5,1,59,tzinfo=datetime.timezone.utc)]
            sup.clock=lambda:tick[0]
            sup.config['compaction']={'quiet_hours':[2,3],'quiet_hours_tz':'UTC',
                                      'rollover_enabled':True,'threshold_tokens':100}
            sup.config['persistent']={'daily_restart':True}
            pid=recovered[-1]['pid']; daily_alerts=len(alerts)
            sup.run_once()
            assert call('before-quiet','claude-l')[0]==0
            assert len([r for r in wire() if r['kind']=='start'])==len(recovered)
            tick[0]+=datetime.timedelta(minutes=2)
            # An attach writer prevents maintenance from killing its conversation.
            with S.acquire_writer(folder,'attach-fixture'):
                sup.run_once()
                os.kill(pid,0)
            sup.run_once()
            try:os.kill(pid,0)
            except ProcessLookupError:pass
            else:raise AssertionError('daily quiet restart did not reap the old child')
            assert call('after-daily','claude-l')[0]==0
            daily=[r for r in wire() if r['kind']=='start']
            assert len(daily)==len(recovered)+1 and daily[-1]['sid']==sid
            sup.run_once();sup.run_once()
            assert call('same-day-reuse','claude-l')[0]==0
            assert len([r for r in wire() if r['kind']=='start'])==len(daily)
            planned=[str(x).lower() for x in alerts[daily_alerts:] if 'planned' in str(x).lower()]
            assert len(planned)==1, planned
            # Reconstruct the supervisor from disk on the same quiet-window date.
            # Kill only the live fixture child to model loss of process ownership.
            os.kill(daily[-1]['pid'],signal.SIGKILL)
            os.waitpid(daily[-1]['pid'],0)
            config=dict(sup.config)
            sup=S.Supervisor(home=folder,engines=engines,poster=post,buildlog_poster=post,
                             engine_timeout=.5,config=config,clock=lambda:tick[0])
            assert call('after-supervisor-restart','claude-l')[0]==0
            restart_starts=len([r for r in wire() if r['kind']=='start'])
            restart_alerts=len(alerts)
            sup.run_once()
            assert call('same-date-after-restart','claude-l')[0]==0
            assert len([r for r in wire() if r['kind']=='start'])==restart_starts
            assert not any('planned' in str(x).lower() for x in alerts[restart_alerts:])
            tick[0]+=datetime.timedelta(days=1)
            sup.run_once()
            assert call('next-day','claude-l')[0]==0
            assert len([r for r in wire() if r['kind']=='start'])==restart_starts+1
            # Execute the production attach command with a real interactive child.
            # Only configuration lookup and the external message sink are replaced.
            cli=runpy.run_path(str(ROOT/'manager/hydra'),run_name='b8_attach_cli')
            for exit_code in (0,17):
                previous=[r for r in wire() if r['kind']=='start'][-1]['pid']
                entered_before=len([r for r in wire() if r['kind']=='interactive_enter'])
                result=[];busy_result=[];busy=None
                if exit_code==0:
                    sup.engine_timeout=5
                    def busy_turn():
                        try:
                            with S.acquire_writer(folder,'supervisor'):
                                busy_result.append(call('WAIT_FOR_ATTACH','claude-l'))
                        except BaseException as exc:busy_result.append(exc)
                    busy=threading.Thread(target=busy_turn,daemon=True);busy.start()
                    deadline=time.monotonic()+3
                    while not any(r.get('text')=='WAIT_FOR_ATTACH' for r in wire()):
                        assert time.monotonic()<deadline and busy.is_alive(),busy_result
                        time.sleep(.01)
                def attach():
                    try:result.append(cli['cmd_attach'](folder,[]))
                    except BaseException as exc:result.append(exc)
                with patch.object(S,'default_engines',return_value=engines), \
                     patch.object(S,'load_config',return_value=dict(sup.config)), \
                     patch.object(S,'default_poster',return_value=post):
                    worker=threading.Thread(target=attach,daemon=True);worker.start()
                    if busy is not None:
                        try:
                            time.sleep(.1)
                            assert worker.is_alive(),'attach refused instead of waiting for current turn'
                            assert len([r for r in wire() if r['kind']=='interactive_enter'])==entered_before
                            os.kill(previous,0)
                        finally:
                            (home/'turn-release').write_text('release')
                            busy.join(timeout=5)
                        assert not busy.is_alive() and len(busy_result)==1,busy_result
                        assert isinstance(busy_result[0],tuple) and busy_result[0][0]==0,busy_result
                    deadline=time.monotonic()+5
                    while len([r for r in wire() if r['kind']=='interactive_enter'])==entered_before:
                        assert worker.is_alive(),result
                        assert time.monotonic()<deadline,'attach never acquired session'
                        sup.run_once();time.sleep(.01)
                    try:
                        try:os.kill(previous,0)
                        except ProcessLookupError:pass
                        else:raise AssertionError('background Claude still alive during attach')
                        assert S.persistent_status(folder)['state']=='parked'
                        assert 'parked' in S.status_text(folder)
                        entered=[r for r in wire() if r['kind']=='interactive_enter'][-1]
                        assert entered['sid']==sid
                        try:
                            with S.acquire_writer(folder,'second-writer'):
                                raise AssertionError('attach did not hold exclusive writer')
                        except S.WriterHeld:pass
                        sup.run_once()
                        assert len([r for r in wire() if r['kind']=='interactive_enter'])==entered_before+1
                    finally:
                        (home/'attach-release').write_text(str(exit_code))
                        worker.join(timeout=5)
                    assert not worker.is_alive() and result==[exit_code],result
                sup.run_once()
                assert call('after-attach-'+str(exit_code),'claude-l')[0]==0
                assert [r for r in wire() if r['kind']=='start'][-1]['sid']==sid
            # Old token/byte pressure must no longer schedule automatic rollover.
            sup.schedule_compaction(1000000)
            assert sup.compaction_due()[0] is False, 'ordinary usage scheduled rollover'
            sup.save_compaction_state({'pending':{'reason':'bytes','value':1000000000,
                                                  'limit':100,'since':sup.now()}})
            assert sup.compaction_due()[0] is False, 'legacy byte pressure scheduled rollover'
            assert (home/'session-id').read_text().strip()==sid
            # Vendor boundaries must close Claude even when Claude mode remains persistent.
            old=[r for r in wire() if r['kind']=='start'][-1]['pid']
            engines['codex']={'bin':str(binary),'kind':'codex','cred':None}
            (home/'engine').write_text(json.dumps({'acc':'codex','model':'gpt-6-astra','mode':'persistent'}))
            for i in range(2):
                rc,out,err,_=sup.invoke('codex',engines['codex'],'vendor-'+str(i),model='gpt-6-astra')
                assert rc==0 and out=='fixture codex reply',(rc,out,err)
            try:os.kill(old,0)
            except ProcessLookupError:pass
            else:raise AssertionError('Claude writer survived vendor switch')
            codex_starts=[r for r in wire() if r['kind']=='start' and r['argv'][0]=='exec']
            assert len(codex_starts)==2 and codex_starts[0]['pid']!=codex_starts[1]['pid']
            assert all('--input-format' not in r['argv'] for r in codex_starts)
            assert 'resume' not in codex_starts[0]['argv'] and 'resume' in codex_starts[1]['argv']
            assert S.persistent_status(folder)['state']!='running'
            (home/'engine').write_text(json.dumps({'acc':'claude-l','model':'claude-sonnet-5','mode':'persistent'}))
            assert call('return-from-codex','claude-l')[0]==0
            assert [r for r in wire() if r['kind']=='start'][-1]['sid']==sid
            assert S.persistent_status(folder)['turns_served']==1
            rows=wire()
            assert len([r for r in rows if r.get('text')=='startup-fallback'])==1
            assert len([r for r in rows if r['kind']=='startup_failure'])==2
            assert len([r for r in rows if r.get('text')=='CRASH'])==1
            assert len([r for r in rows if r.get('text')=='HANG'])==1
            assert any(r['kind']=='interrupt' for r in rows)
        finally:
            # Fixture-only cleanup, never a production PID.
            for row in wire():
                if row['kind']=='start':
                    try:
                        argv=Path('/proc')/str(row['pid'])/'cmdline'
                        if str(binary).encode() in argv.read_bytes().split(b'\0'):
                            os.kill(row['pid'],signal.SIGKILL)
                    except (ProcessLookupError,FileNotFoundError):pass

if __name__=='__main__':
    lifecycle()
    print('b8 lifecycle contracts passed; rollover and real evidence are separate required exit gates')
