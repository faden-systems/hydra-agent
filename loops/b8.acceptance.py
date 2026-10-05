#!/usr/bin/env python3
"""Exit-owned b8 integration contracts."""
import subprocess, datetime, hashlib, json, os, runpy, shutil, signal, sys, tempfile, threading, time
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'manager'))
import supervisor as S

def present(pid):
    """True while /proc still lists the pid in any state, including an unreaped zombie."""
    return (Path('/proc')/str(pid)).exists()

def tree_gone(wire_rows, label, settle_s=0.5):
    """PR45 2.5: after a hang, the hung process must be reaped (no /proc entry, not a zombie) and its
    TERM-resistant descendant must be gone too, BEFORE the next request starts; cleanup never conceals a leak."""
    descendants=[r for r in wire_rows if r['kind']=='descendant']
    assert descendants, label+': fixture did not record a descendant'
    hung=descendants[-1]
    assert not present(hung['pid']), label+': hung process still present (killed but not reaped, or alive)'
    deadline=time.monotonic()+settle_s
    while present(hung['child']) and time.monotonic()<deadline:time.sleep(.01)
    assert not present(hung['child']), label+': TERM-resistant descendant leaked past recovery'
    return hung

def kill_fixture_tree(rows, binary):
    """Fixture-only cleanup, never a production PID: the fake CLI processes and their recorded descendants."""
    for row in rows:
        pids=[row['pid']] if row['kind']=='start' else [row['child']] if row['kind']=='descendant' else []
        for pid in pids:
            try:
                cmdline=(Path('/proc')/str(pid)/'cmdline').read_bytes().split(b'\0')
                if str(binary).encode() in cmdline or b'signal.SIG_IGN' in b' '.join(cmdline):
                    os.kill(pid,signal.SIGKILL)
            except (ProcessLookupError,FileNotFoundError):pass

def descendant_ready(wire_rows, seconds=5):
    """4.2: wait until the fixture recorded its descendant AND the descendant installed its signal handlers."""
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        rows=[r for r in wire_rows() if r['kind']=='descendant']
        if rows and Path(rows[-1]['ready']).exists():return rows[-1]
        time.sleep(.02)
    raise AssertionError('descendant did not become signal-ready in time')

def expected_usage(text):
    """The fixture's accounting for one request: context = first assistant usage input side (7+extra, 2, 11),
    input_tokens the same, tokens adds the 3 output tokens; `extra` varies with the request text."""
    extra=len(text)%3
    return {'context_tokens':20+extra,'input_tokens':20+extra,'tokens':23+extra}

def ledger_rows(home):
    path=Path(S.memory_dir_for(str(home)))/'LEDGER.jsonl'
    return S.read_jsonl(str(path)) if path.exists() else None

def before_fresh(home):
    """Snapshot attempts and ledger before a fresh recovery event is enqueued (round 4, 2.2)."""
    ledger=ledger_rows(home)
    return {'attempts':len(S.read_jsonl(str(Path(home)/'logs'/'attempts.jsonl'))),'ledger':len(ledger or [])}

def fresh_completed(home, event_id, marker, wire_rows, before, poster_texts=None):
    """2.3/2.2: a fresh recovery event is COMPLETED, not merely marked handled: exactly one fixture request
    carried it, exactly one NEW successful attempt and exactly one NEW ledger row match its error-free turn
    (turn number, engine, model), and (when the poster is observable) its reply was delivered."""
    home=Path(home)
    requests=[r for r in wire_rows() if r['kind']=='request' and marker in r.get('text','')]
    assert len(requests)==1,('fresh event must be served exactly once',marker,len(requests))
    turns=[t for t in S.read_jsonl(str(home/'logs'/'turns.jsonl')) if event_id in (t.get('events') or [])]
    assert len(turns)==1 and 'error' not in turns[0],('fresh event needs exactly one error-free turn',turns)
    turn=turns[0]
    attempts=S.read_jsonl(str(home/'logs'/'attempts.jsonl'))[before['attempts']:]
    assert len([a for a in attempts if a.get('success')])==1,('exactly one new successful attempt required',attempts)
    ledger=ledger_rows(home)
    assert ledger is not None,'ledger missing after the fresh event'
    new_rows=ledger[before['ledger']:]
    matching=[l for l in new_rows if l.get('turn')==turn.get('n')]
    assert len(new_rows)==1 and len(matching)==1,('exactly one new ledger row for the fresh turn required',new_rows)
    assert matching[0].get('engine')==turn.get('engine') and matching[0].get('model')==turn.get('model'),('ledger row disagrees with the turn',matching[0],turn)
    if poster_texts is not None:
        # The fixture echoes the whole wrapped prompt after 'reply:'; the delivered text must carry the request
        # marker and the fixture's reply prefix, whatever the production prompt wrapping and formatting add.
        assert any(marker in t and 'reply:' in t for t in poster_texts),'fresh event reply was not delivered'
    assert event_id in S.handled_ids(str(home))

def restarts(folder):
    """1.2: planned and unexpected restart accounting from persistent_status."""
    status=S.persistent_status(folder)
    acc=status.get('restarts')
    assert isinstance(acc,dict) and isinstance(acc.get('planned'),int) and isinstance(acc.get('unexpected'),int),('restart accounting missing from persistent_status',status)
    return acc

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
                want=expected_usage('turn-'+str(i))
                # 2.3: full accounting, varying between requests, duplicates and terminal aggregates never re-added.
                assert {k:usage.get(k) for k in want}==want,(usage,want)
            assert expected_usage('turn-9')!=expected_usage('turn-10'),'fixture usage must vary between requests'
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
            # 1.1 (round 4): native compaction inside the conversation is normal; the adapter keeps the same
            # process and the next request on the same conversation succeeds.
            assert call('B8_NATIVE_COMPACT')[0]==0
            assert any(r['kind']=='native_compaction' for r in wire()),'fixture did not record native compaction'
            assert call('after-compact')[0]==0
            assert len([r for r in wire() if r['kind']=='start'])==1,'native compaction must not restart the process'
            assert [r for r in wire() if r['kind']=='start'][0]['sid']==sid
            baseline_restarts=restarts(folder)
            assert call('switch','claude-l')[0]==0
            starts=[r for r in wire() if r['kind']=='start']
            assert len(starts)==2 and starts[-1]['sid']==sid
            after_switch=restarts(folder)
            assert after_switch['planned']==baseline_restarts['planned']+1 and after_switch['unexpected']==baseline_restarts['unexpected'],('account switch is a planned restart',after_switch)
            try:os.kill(old,0)
            except ProcessLookupError:pass
            else:raise AssertionError('old account process not reaped before switch')
            assert call('CRASH','claude-l')[0]!=0
            assert S.persistent_status(folder)['state']!='running','dead process reported healthy'
            assert call('after-crash','claude-l')[0]==0
            after_crash=restarts(folder)
            assert after_crash['unexpected']==after_switch['unexpected']+1 and after_crash['planned']==after_switch['planned'],('crash recovery is an unexpected restart',after_crash)
            assert (after_crash.get('last') or {}).get('category')=='crash' and (after_crash.get('last') or {}).get('reason'),after_crash
            begin=time.monotonic();assert call('HANG','claude-l')[0]!=0
            assert time.monotonic()-begin<4, 'hang cleanup exceeded timeout plus grace'
            hung=tree_gone(wire(),'lifecycle hang')
            assert call('after-hang','claude-l')[0]==0
            restarted=[r for r in wire() if r['kind']=='start'][-1]
            assert restarted['pid']!=hung['pid'],'hang recovery must start a fresh process'
            after_hang=restarts(folder)
            assert after_hang['unexpected']==after_crash['unexpected']+1 and after_hang['planned']==after_crash['planned'],('timeout recovery is an unexpected restart',after_hang)
            assert (after_hang.get('last') or {}).get('category')=='timeout',after_hang
            # Model changes and explicit rollback also close the previous writer.
            before=[r for r in wire() if r['kind']=='start']
            assert call('model-switch','claude-l','claude-fable-5-1')[0]==0
            after=[r for r in wire() if r['kind']=='start']
            assert len(after)==len(before)+1 and after[-1]['sid']==sid
            model_args=after[-1]['argv']
            assert model_args[model_args.index('--model')+1]=='claude-fable-5-1'
            after_model=restarts(folder)
            assert after_model['planned']==after_hang['planned']+1 and after_model['unexpected']==after_hang['unexpected'],('model switch is a planned restart',after_model)
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
            # 1.1: configured and effective modes plus the startup-failure reason are exposed truthfully.
            runtime=S.read_engine_runtime(folder)
            assert runtime['configured'].get('mode')=='persistent' and runtime['effective'].get('mode')=='per-turn',runtime
            assert 'start' in str(runtime.get('reason','')).lower(),('startup fallback reason missing',runtime)
            rendered=S.status_text(folder).lower()
            assert 'persistent' in rendered and 'per-turn' in rendered and str(runtime['reason']).strip().lower()[:60] in rendered,rendered
            assert call('startup-recovered','claude-l')[0]==0
            runtime=S.read_engine_runtime(folder)
            assert runtime['effective'].get('mode')=='persistent' and not runtime.get('reason'),('recovery must clear the transient fallback',runtime)
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
            leaked=[r['child'] for r in rows if r['kind']=='descendant' and present(r['child'])]
            assert not leaked,('descendants alive at the end of the lifecycle; cleanup must not conceal them',leaked)
        finally:
            kill_fixture_tree(wire(),binary)

def queued_recovery(fault):
    """No re-execution after a child has accepted an event and performed a side effect."""
    with tempfile.TemporaryDirectory(prefix='b8-queued-') as folder:
        home=Path(folder); binary=home/'fake-claude'
        shutil.copyfile(ROOT/'loops/b8.fake.py',binary);binary.chmod(0o755)
        engines={name:{'bin':str(binary),'kind':'claude','cred':name+'.env'}
                 for name in ('claude-r2d2','claude-l')}
        (home/'credentials').mkdir()
        for name,spec in engines.items():
            token='dummy-'+name;cred=home/'credentials'/spec['cred']
            cred.write_text('CLAUDE_CODE_OAUTH_TOKEN='+token+'\n');cred.chmod(0o600)
            Path(str(cred)+'.identity.json').write_text(json.dumps({'schema_version':1,'label':name,
                'account_id':'fixture-'+name,'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
                'verified_at':'2026-10-04T20:00:00Z','method':'private-window-and-usage-bar',
                'evidence':'https://app.slack.com/archives/Cfixture/p123'}))
        (home/'engine').write_text(json.dumps({'acc':'claude-r2d2','model':'claude-sonnet-5','mode':'persistent'}))
        alerts=[]
        def post(*args):
            alerts.append(str(args[-1]));return {'ok':True,'ts':'123.456'}
        def supervisor():
            return S.Supervisor(home=folder,engines=engines,config={'engine_fallback':{'claude_models':[]}},
                poster=post,buildlog_poster=post,reactor=S.DryReactor(folder),engine_timeout=.3,
                codex_home=str(home/'codex'))
        def rows():return S.read_jsonl(str(home/'wire.jsonl'))
        def effects():return [r for r in rows() if r['kind']=='side_effect']
        event=S.new_event('cli',{'text':'B8_QUEUED_'+fault},event_id='fault-'+fault)
        S.append_event(folder,event)
        try:
            sup=supervisor();sup.run_once()
            assert len(effects())==1,('inline routing replayed submitted event',fault,effects())
            if fault=='HANG':tree_gone(rows(),'queued hang')
            assert not any(r.get('success') for r in S.read_jsonl(str(home/'logs/attempts.jsonl'))), 'failed event claimed success'
            # Advance beyond backoff without modifying queue or retry records. Reconstruction
            # discards in-memory guards; durable event disposition must prevent another execution.
            future=time.time()+86400
            with patch.object(S.time,'time',return_value=future):
                for _ in range(2):sup.run_once()
                sup=supervisor()
                for _ in range(2):sup.run_once()
            assert len(effects())==1,('backoff/restart replayed submitted event',fault,effects())
            # A new event is still serviceable; quarantining an uncertain result must not
            # disable all future work or batch the failed event into this request.
            snapshot=before_fresh(folder)
            fresh=S.new_event('slack',{'channel':'C_B8','thread_ts':'1.0','ts':'1.1','user':'U_FOUNDER','text':'B8_FRESH_EVENT',
                                       'instructs':True,'addressed':True},event_id='fresh-'+fault)
            S.append_event(folder,fresh)
            with patch.object(S.time,'time',return_value=future+86400):
                for _ in range(3):sup.run_once()
            assert len(effects())==1,('fresh event replayed failed event',fault,effects())
            fresh_completed(folder,fresh['id'],'B8_FRESH_EVENT',rows,snapshot,alerts)
        finally:
            kill_fixture_tree(rows(),binary)

def recovery_worker(folder, binary):
    """A supervisor in its own process; the parent kills it after the fixture's side effect (8.1)."""
    engines={name:{'bin':str(binary),'kind':'claude','cred':name+'.env'} for name in ('claude-r2d2','claude-l')}
    sup=S.Supervisor(home=folder,engines=engines,config={'engine_fallback':{'claude_models':[]}},
                     poster=lambda *a:{'ok':True,'ts':'1.0'},buildlog_poster=lambda *a:None,reactor=S.DryReactor(folder),
                     engine_timeout=30,codex_home=str(Path(folder)/'codex'))
    while not (Path(folder)/'stop-worker').exists():
        sup.run_once();time.sleep(.02)

def supervisor_death():
    """8.1: the supervisor dies after the child performed its side effect but before failure handling;
    a restart against the same home retires the orphan, never re-executes, reports the event truthfully
    and still serves a fresh event."""
    import multiprocessing
    with tempfile.TemporaryDirectory(prefix='b8-supdeath-') as folder:
        home=Path(folder);binary=home/'fake-claude'
        shutil.copyfile(ROOT/'loops/b8.fake.py',binary);binary.chmod(0o755)
        engines={name:{'bin':str(binary),'kind':'claude','cred':name+'.env'} for name in ('claude-r2d2','claude-l')}
        (home/'credentials').mkdir()
        for name,spec in engines.items():
            token='dummy-'+name;cred=home/'credentials'/spec['cred']
            cred.write_text('CLAUDE_CODE_OAUTH_TOKEN='+token+'\n');cred.chmod(0o600)
            Path(str(cred)+'.identity.json').write_text(json.dumps({'schema_version':1,'label':name,
                'account_id':'fixture-'+name,'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
                'verified_at':'2026-10-04T20:00:00Z','method':'private-window-and-usage-bar',
                'evidence':'https://app.slack.com/archives/Cfixture/p123'}))
        (home/'engine').write_text(json.dumps({'acc':'claude-r2d2','model':'claude-sonnet-5','mode':'persistent'}))
        def rows():return S.read_jsonl(str(home/'wire.jsonl'))
        def effects():return [r for r in rows() if r['kind']=='side_effect']
        event=S.new_event('cli',{'text':'B8_QUEUED_HANG'},event_id='supdeath-hang')
        S.append_event(folder,event)
        ctx=multiprocessing.get_context('fork')
        worker=ctx.Process(target=recovery_worker,args=(folder,binary),daemon=True);worker.start()
        try:
            deadline=time.monotonic()+15
            while not effects() and time.monotonic()<deadline:time.sleep(.02)
            assert len(effects())==1,'fixture never recorded its side effect'
            hung=descendant_ready(rows)  # 4.2: both records present and the descendant signal-ready before the kill
            os.kill(worker.pid,signal.SIGKILL);worker.join(5)
            assert not worker.is_alive(),'supervisor worker survived SIGKILL'
            assert present(hung['pid']) and present(hung['child']),'fixture tree should still be alive when the supervisor dies'
            # Restart against the same home: orphan retirement, no second execution, truthful status.
            posted=[]
            def post(*args):
                posted.append(str(args[-1]));return {'ok':True,'ts':'1.0'}
            sup=S.Supervisor(home=folder,engines=engines,config={'engine_fallback':{'claude_models':[]}},
                             poster=post,buildlog_poster=lambda *a:None,reactor=S.DryReactor(folder),
                             engine_timeout=.3,codex_home=str(home/'codex'))
            for _ in range(3):sup.run_once()
            tree_gone(rows(),'supervisor death orphan')
            assert len(effects())==1,('restart re-executed the submitted event',effects())
            assert event['id'] in S.handled_ids(folder),'event left pending after supervisor death; it would run again'
            assert not any(r.get('success') for r in S.read_jsonl(str(home/'logs/attempts.jsonl'))),'orphaned event claimed success'
            status=S.status_text(folder).lower()
            assert 'running' not in status.split('persistent')[-1][:80] or S.persistent_status(folder)['state']!='running' or S.persistent_status(folder)['pid']!=hung['pid'],'dead supervisor child reported healthy'
            orphan=restarts(folder)
            assert orphan['unexpected']>=1 and (orphan.get('last') or {}).get('category')=='orphan',('orphan retirement is an unexpected restart',orphan)
            snapshot=before_fresh(folder)
            fresh=S.new_event('slack',{'channel':'C_B8','thread_ts':'1.0','ts':'1.2','user':'U_FOUNDER','text':'B8_FRESH_AFTER_DEATH',
                                       'instructs':True,'addressed':True},event_id='supdeath-fresh')
            S.append_event(folder,fresh)
            with patch.object(S.time,'time',return_value=time.time()+86400):
                for _ in range(3):sup.run_once()
            fresh_completed(folder,fresh['id'],'B8_FRESH_AFTER_DEATH',rows,snapshot,posted)
            assert len(effects())==1
        finally:
            (home/'stop-worker').write_text('1')
            if worker.is_alive():worker.kill();worker.join(5)
            kill_fixture_tree(rows(),binary)

def attach_worker(folder):
    sup=S.Supervisor(home=folder,poster=lambda *a:None,buildlog_poster=lambda *a:None,
                     reactor=S.DryReactor(folder),engine_timeout=8)
    while not (Path(folder)/'stop-worker').exists():
        sup.run_once();time.sleep(.02)

def separate_attach(exit_code=None):
    with tempfile.TemporaryDirectory(prefix='b8-attach-') as folder:
        home=Path(folder);binary=home/'fake-claude'
        shutil.copyfile(ROOT/'loops/b8.fake.py',binary);binary.chmod(0o755)
        (home/'credentials').mkdir();cred=home/'credentials/fixture.env'
        token='dummy-attach';cred.write_text('CLAUDE_CODE_OAUTH_TOKEN='+token+'\n')
        Path(str(cred)+'.identity.json').write_text(json.dumps({'schema_version':1,'label':'claude-r2d2',
            'account_id':'fixture-r2d2','token_sha256':hashlib.sha256(token.encode()).hexdigest(),
            'verified_at':'2026-10-04T20:00:00Z','method':'private-window-and-usage-bar',
            'evidence':'https://app.slack.com/archives/Cfixture/p123'}))
        (home/'config.json').write_text(json.dumps({'engines':{'claude-r2d2':{'bin':str(binary),'kind':'claude','cred':'fixture.env'}},
             'engine_fallback':{'claude_models':[]},'codex_home':str(home/'codex')}))
        (home/'engine').write_text(json.dumps({'acc':'claude-r2d2','model':'claude-sonnet-5','mode':'persistent'}))
        env={'PATH':os.environ['PATH'],'HOME':folder,'HYDRA_HOME':folder,'PYTHONPATH':str(ROOT/'manager')}
        processes=[]
        def launch(argv):
            p=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,start_new_session=True)
            processes.append(p);return p
        def rows():return S.read_jsonl(str(home/'wire.jsonl'))
        def wait_for(test,label):
            deadline=time.monotonic()+6
            while not test():
                assert time.monotonic()<deadline,label
                time.sleep(.02)
        def alive(pid):
            try:return (Path('/proc')/str(pid)/'stat').read_text().split(') ')[1].split()[0]!='Z'
            except FileNotFoundError:return False
        def enqueue(text,eid):S.append_event(folder,S.new_event('cli',{'text':text},event_id=eid))
        worker=launch([sys.executable,__file__,'--attach-worker',folder])
        try:
            enqueue('WAIT_FOR_ATTACH','busy')
            wait_for(lambda:any('WAIT_FOR_ATTACH' in r.get('text','') for r in rows()),'busy request never started')
            old=[r for r in rows() if r['kind']=='start'][-1]
            attached=launch([sys.executable,str(ROOT/'manager/hydra'),'attach'])
            time.sleep(.2)
            assert attached.poll() is None,'attach refused busy handoff'
            assert not any(r['kind']=='interactive_enter' for r in rows()),'attach overlapped busy turn'
            (home/'turn-release').write_text('release')
            wait_for(lambda:any(r['kind']=='interactive_enter' for r in rows()),'attach did not enter')
            interactive=[r for r in rows() if r['kind']=='interactive_enter'][-1]
            assert interactive['sid']==old['sid']
            assert not alive(old['pid']),'background writer survived attach'
            # A distinct process must fail to acquire the held writer lock.
            competing=launch([sys.executable,'-c',
                'import supervisor as S,sys\ntry:\n with S.acquire_writer(sys.argv[1],"competitor"): sys.exit(9)\nexcept S.WriterHeld: sys.exit(0)',folder])
            assert competing.wait(timeout=3)==0,'competing writer acquired attach lock'
            snapshot=before_fresh(folder)
            enqueue('AFTER_ATTACH','fresh')
            time.sleep(.2)
            assert not any('AFTER_ATTACH' in r.get('text','') for r in rows()),'supervisor wrote during attach'
            # 1.2: ownership is reported truthfully while the interactive writer holds the conversation.
            parked=S.persistent_status(folder)
            assert parked['state']=='parked',('attach ownership not reported as parked',parked)
            assert 'parked' in S.status_text(folder).lower(),'rendered status does not show the parked state'
            # Kill only CLI parent, leaving its interactive child alive at that instant.
            assert alive(interactive['pid'])
            if exit_code is None:
                attached.kill();attached.wait(timeout=3)
            else:
                (home/'attach-release').write_text(str(exit_code))
                assert attached.wait(timeout=3)==exit_code,'attach exit status changed'
            wait_for(lambda:'fresh' in S.handled_ids(folder),'orphan attach prevented recovery')
            fresh_completed(folder,'fresh','AFTER_ATTACH',rows,snapshot)  # 2.3/2.2: completed, not merely handled
            running=S.persistent_status(folder)
            assert running['state']=='running' and running['pid']==[r for r in rows() if r['kind']=='start'][-1]['pid'],running
            assert running['turns_served']>=1 and running['uptime_s']>=0 and 'parked' not in S.status_text(folder).lower()
            assert not alive(interactive['pid']),'orphan interactive writer survived recovery'
            starts=[r for r in rows() if r['kind']=='start']
            assert starts[-1]['sid']==old['sid'],'attach recovery changed conversation'
            assert worker.poll() is None,'supervisor died during attach recovery'
        finally:
            for p in processes:
                try:os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                p.wait(timeout=3)
            for r in rows():
                if r['kind']=='start':
                    try:
                        if str(binary).encode() in (Path('/proc')/str(r['pid'])/'cmdline').read_bytes().split(b'\0'):
                            os.kill(r['pid'],signal.SIGKILL)
                    except (FileNotFoundError,ProcessLookupError):pass

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--attach-worker':
        attach_worker(sys.argv[2]);sys.exit(0)
    elif len(sys.argv)>1 and sys.argv[1]=='--attach-only':
        separate_attach()
    elif len(sys.argv)>1 and sys.argv[1]=='--queued-only':
        queued_recovery(sys.argv[2])
    else:
        for fault in ('CRASH','HANG'):queued_recovery(fault)
        supervisor_death()
        for code in (0,17,None):separate_attach(code)
        lifecycle()
    print('b8 lifecycle contracts passed; rollover and real evidence are separate required exit gates')
