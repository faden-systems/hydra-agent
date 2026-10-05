#!/usr/bin/env python3
"""Draft real capture orchestration. Explicit scratch inputs only; never defaults to production."""
import argparse,hashlib,json,os,runpy,signal,subprocess,sys,tempfile,time,shutil
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'manager'))
import supervisor as S
MODEL='claude-opus-5-5'

def git(*args):return subprocess.check_output(['git','-C',str(ROOT),*args]).decode().strip()
def save(path,value):path.write_text(json.dumps(value,indent=2)+'\n')
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def write_manifest(output,candidate,binary):
    names={'account_switch':'account_switch.json','attempts':'auth-attempts.json',
           'status_before':'auth-fallback-runtime.json','status_after':'auth-recovery-runtime.json',
           'status_text_before':'auth-fallback-status.txt','status_text_after':'auth-recovery-status.txt',
           'identity_before':'auth-fallback-identity.json','identity_after':'auth-recovery-identity.json',
           'alerts':'auth-alerts-delivered.json','selection_writes':'auth-selection-writes.json',
           'ledger':'auth-ledger.json','turns':'auth-turns.json','replies':'auth-replies-delivered.json','credits':'credits.json',
           'model_audit':'model-audit.json'}
    summary={**candidate,'binary_path':str(binary),'binary_sha256':digest(binary),
             'binary_version':subprocess.check_output([str(binary),'--version'],timeout=15).decode().strip(),
             'modes':{mode:{key:f'{mode}/{name}' for key,name in names.items()}
                      for mode in ('per-turn','persistent')}}
    save(output/'summary.json',summary)
    manifest={str(p.relative_to(output)):digest(p) for p in sorted(output.rglob('*'))
              if p.is_file() and p.name!='manifest.json'}
    save(output/'manifest.json',manifest)
def wire_objects(wire):
    rows=[]
    for path in sorted(wire.glob('*.stdout')):
        for line in path.read_text().splitlines():
            try:row=json.loads(line)
            except json.JSONDecodeError:continue
            rows.append((path.name,row))
    return rows

def successful_model(wire,expected_results=1,requested_model=MODEL):
    rows=wire_objects(wire)
    terminal=[r for _,r in rows if r.get('type')=='result']
    assert len(terminal)==expected_results and all(r.get('is_error') is False for r in terminal),'missing successful terminal result'
    models={r['message']['model'] for _,r in rows if r.get('type')=='assistant'
            and isinstance(r.get('message'),dict) and r['message'].get('model')}
    assert models=={requested_model},'real assistant model differs from requested model or is unavailable'
    return requested_model

def discover_retries(config):
    """Routing observation only, never identity/model-availability evidence."""
    with tempfile.TemporaryDirectory(prefix='b8-route-only-') as folder:
        home=Path(folder);save(home/'engine',{'acc':'claude-l','model':'claude-fable-5-1','mode':'per-turn'})
        sup=S.Supervisor(home=folder,engines={'claude-l':{'kind':'claude','bin':'never-executed'}},
                         config={**config,'repo':None},poster=lambda *a:None,buildlog_poster=lambda *a:None)
        seen=[]
        def reject(name,spec,message,model=None,**kwargs):
            seen.append(model)
            return 1,'','Credit balance is too low',None
        with patch.object(sup,'invoke',reject):
            try:sup.run_engines('routing observation',[])
            except S.AllEnginesFailed:pass
            else:raise AssertionError('routing observation unexpectedly succeeded')
        assert seen and seen[0]=='claude-fable-5-1'
        return seen[1:]

PROBE_PROMPT='Please reply with the single word B8_MODEL_OK and nothing else.'

def synthetic_refusal(wire):
    """A vendor-side refusal: the CLI answers with a synthetic assistant row (model `<synthetic>`) or an error
    terminal that says the request was flagged by a safeguard. Not a model-availability fact; retried once in a
    new session (founder 2026-10-05, thread 1791229946.497799)."""
    for _,row in wire_objects(wire):
        message=row.get('message') if isinstance(row.get('message'),dict) else {}
        if row.get('type')=='assistant' and message.get('model')=='<synthetic>':return True
        if row.get('type')=='result' and row.get('is_error') and 'safeguards flagged' in str(row.get('result') or '').lower():return True
    return False

def capture_models(sup,home,engines,mode,mode_root):
    aliases=json.loads((ROOT/'manager/models.json').read_text())['claude']['aliases']
    assert aliases.get('opus5')=='claude-opus-5' and aliases.get('opus5.5')=='claude-opus-5-5','required alias mapping changed (2.2)'
    routing_config={'engine_fallback':sup.config.get('engine_fallback',{})}
    retries=discover_retries(routing_config)
    # Opus 5.5 before Opus 5 (founder 2026-10-04 1791148777.512759: the L non-Fable probe is claude-opus-5-5).
    models=list(dict.fromkeys([aliases['opus5.5'],aliases['opus5'],*retries]))
    records=[]
    for index,model in enumerate(models):
        attempts=[]
        for attempt in (0,1):
            wire=mode_root/(f'model-audit-{index}' if attempt==0 else f'model-audit-{index}-retry');wire.mkdir(mode=0o700)
            os.environ['B8_CAPTURE_WIRE_DIR']=str(wire)
            # Each probe starts a fresh session: with an empty session-id file invoke() runs `--session-id <new uuid>`
            # instead of resuming the scratch conversation filled by the earlier probes (attempt 4: a resumed session
            # was refused by a safeguard classifier, not by the model).
            S.write_text(home/'session-id','')
            save(home/'engine',{'acc':'claude-l','model':model,'mode':mode})
            try:
                rc,out,err,usage=sup.invoke('claude-l',engines['claude-l'],PROBE_PROMPT,model=model)
                reported=sorted({r['message']['model'] for _,r in wire_objects(wire)
                    if r.get('type')=='assistant' and isinstance(r.get('message'),dict) and r['message'].get('model')})
                refused=bool(rc) and synthetic_refusal(wire)
                attempts.append({'wire':wire.name,'returncode':rc,'reported_models':reported,'usage':usage,
                                 'synthetic_refusal':refused})
            finally:
                orderly_shutdown(sup);stop_recorded(wire)
            if rc==0 or not refused:break
        last=attempts[-1]
        records.append({'requested_model':model,'reported_models':last['reported_models'],'returncode':last['returncode'],
                        'wire':last['wire'],'usage':last['usage'],'attempts':attempts})
        save(mode_root/'model-audit.json',{'aliases':aliases,'routing_config':routing_config,'retry_models':retries,'probes':records})
        assert last['returncode']==0,'real model audit failed; inspect retained wire output'
        successful_model(mode_root/last['wire'],requested_model=model)

FORCED=[]
def orderly_shutdown(sup):
    """B3: ask the candidate to end any persistent CLI process by closing its input and waiting for exit;
    returns the candidate's report. Forced signals belong to stop_recorded and are recorded separately."""
    if hasattr(sup,'shutdown_persistent'):
        report=sup.shutdown_persistent()
        assert isinstance(report,dict) and 'orderly' in report,'shutdown_persistent must report orderly/returncode'
        return report
    return None

def stop_recorded(wire):
    # Start ticks prevent an old capture from targeting a reused PID. Child tracking
    # also covers a wrapper killed before it could forward the shutdown signal.
    owned=[]
    for path in wire.glob('*.json'):
        meta=json.loads(path.read_text())
        for role in ('child','wrapper'):
            owned.append((meta[role+'_pid'],meta[role+'_start'],role=='child'))
    def alive(item):
        pid,start,group=item
        try:
            fields=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
            return fields[19]==start and fields[0]!='Z'
        except FileNotFoundError:return False
    def stop(sig):
        for item in owned:
            if alive(item):
                try:
                    (os.killpg if item[2] else os.kill)(item[0],sig)
                except ProcessLookupError:pass
    survivors=[item for item in owned if alive(item)]
    for pid,start,group in survivors:
        FORCED.append({'child_pid' if group else 'wrapper_pid':pid,'start':start,'wire':str(wire)})
    if not survivors:return
    for sig in (signal.SIGTERM,signal.SIGKILL):
        stop(sig)
        deadline=time.monotonic()+3
        while any(alive(item) for item in owned) and time.monotonic()<deadline:time.sleep(.02)
        if not any(alive(item) for item in owned):return
    raise RuntimeError('scratch capture processes survived bounded cleanup')

def capture_auth(source_home, engines, mode, mode_root):
    with tempfile.TemporaryDirectory(prefix='b8-real-auth-') as folder:
        home=Path(folder)
        shutil.copytree(source_home/'credentials',home/'credentials')
        primary=home/'credentials'/engines['claude-r2d2']['cred']
        original=primary.read_bytes()
        sidecar=Path(str(primary)+'.identity.json')
        original_identity=sidecar.read_bytes() if sidecar.exists() else None
        primary.write_text('CLAUDE_CODE_OAUTH_TOKEN=b8-deliberately-invalid-token\n')
        if sidecar.exists():sidecar.unlink()
        save(home/'engine',{'acc':'claude-r2d2','model':MODEL,'mode':mode})
        delivered=[];alerts=[]
        def receive(target):
            def post(channel,thread_ts,text):
                target.append({'channel':channel,'thread_ts':thread_ts,'text':text})
                return {'ts':str(len(target))}
            return post
        sup=S.Supervisor(home=folder,engines=engines,poster=receive(delivered),
                         reactor=S.DryReactor(folder),config={'repo':None},
                         buildlog_poster=lambda *a:None,engine_timeout=90)
        wires=[];writes=[]
        original_set_engine=S.set_engine
        def observe_selection(*args,**kwargs):
            result=original_set_engine(*args,**kwargs)
            writes.append({'cause':'operator' if kwargs.get('explicit',True) else 'fallback',
                           'selection':json.loads((home/'engine').read_text())})
            return result
        observer=patch.object(S,'set_engine',observe_selection)
        observer.start()
        try:
            for phase in ('fallback','recovery'):
                wire=mode_root/('auth-'+phase);wire.mkdir(mode=0o700);wires.append(wire)
                os.environ['B8_CAPTURE_WIRE_DIR']=str(wire)
                if phase=='recovery':
                    primary.write_bytes(original)
                    if original_identity is not None:sidecar.write_bytes(original_identity)
                    S.set_engine(folder,'claude-r2d2',MODEL,engines)
                event={'id':'b8-auth-'+phase,'source':'slack','payload':{
                    'channel':'C_B8_SCRATCH','ts':str(len(delivered)+1),'thread_ts':'b8-auth',
                    'text':'Reply only B8_AUTH_OK. Do not use tools.','instructs':True,'addressed':True}}
                sup.turn([event])
                turns=sup.turns()
                assert len(turns)==(1 if phase=='fallback' else 2) and 'error' not in turns[-1]
                assert turns[-1]['engine']==('claude-l' if phase=='fallback' else 'claude-r2d2')
                assert turns[-1]['model']==MODEL
                S.drain_outbox(folder,receive(alerts))
                assert S.drain_outbox(folder,receive(alerts))==0,'notice was queued twice'
                save(mode_root/('auth-'+phase+'-runtime.json'),S.read_engine_runtime(folder))
                (mode_root/('auth-'+phase+'-status.txt')).write_text(S.status_text(folder))
                account='claude-l' if phase=='fallback' else 'claude-r2d2'
                identity=S.credential_identity(folder,account,engines[account])
                save(mode_root/('auth-'+phase+'-identity.json'),
                     {k:identity.get(k) for k in ('verified','verified_at','mismatch','account_id','source','method')})
            attempts=[json.loads(line) for line in (home/'logs/attempts.jsonl').read_text().splitlines()]
            save(mode_root/'auth-attempts.json',attempts)
            assert len(attempts)==3 and attempts[0]['classification']=='auth'
            assert not attempts[0]['success'] and attempts[0]['reason'].strip()
            assert all(a['success'] for a in attempts[1:])
            ledger=[json.loads(line) for line in (Path(sup.memory_dir)/'LEDGER.jsonl').read_text().splitlines()]
            save(mode_root/'auth-ledger.json',ledger)
            save(mode_root/'auth-turns.json',sup.turns())
            save(mode_root/'auth-alerts-delivered.json',alerts)
            save(mode_root/'auth-replies-delivered.json',delivered)
            assert len(ledger)==2 and [r['engine'] for r in ledger]==['claude-l','claude-r2d2']
            assert [r['turn'] for r in ledger]==[1,2]
            assert sum('fallback started' in r['text'] for r in alerts)==1
            assert sum('fallback ended' in r['text'] for r in alerts)==1
            assert len(delivered)==2
            save(mode_root/'auth-selection-writes.json',writes)
            assert len([w for w in writes if w['cause']=='fallback'])==1
        finally:
            observer.stop()
            orderly_shutdown(sup)
            for wire in wires:stop_recorded(wire)

def capture_credits(sup, home, engines, mode_root):
    """One L Fable probe through the actual candidate retry path."""
    wire=mode_root/'credits-wire';wire.mkdir(mode=0o700)
    # The candidate must restart at the model boundary before reading this destination.
    os.environ['B8_CAPTURE_WIRE_DIR']=str(wire)
    selected=json.loads((home/'engine').read_text())
    selected.update(acc='claude-l',model='claude-fable-5-1')
    save(home/'engine',selected)
    sup.engines={'claude-l':engines['claude-l']}
    sup.config['engine_fallback']={'claude_models':[MODEL]}
    try:
        winner,model,out,usage,transition=sup.run_engines('Reply only B8_CREDITS_OK. Do not use tools.',[])
        attempts=[json.loads(line) for line in (home/'logs/attempts.jsonl').read_text().splitlines()]
        save(mode_root/'credits-attempts.json',attempts)
        assert winner=='claude-l' and all(a['engine']=='claude-l' for a in attempts)
        rows=wire_objects(wire)
        terminal=[r for _,r in rows if r.get('type')=='result']
        # The CLI reports a failed request as a synthetic assistant row (model `<synthetic>`, the same row shape as a
        # safeguard refusal); it is not a model the candidate selected, so it is left out of the observed set. The
        # failed attempt is still proven below by the attempts log (classification credits) and failure_proof.
        observed={r['message']['model'] for _,r in rows if r.get('type')=='assistant'
                  and isinstance(r.get('message'),dict) and r['message'].get('model')
                  and r['message'].get('model')!='<synthetic>'}
        if len(attempts)==1:
            assert model=='claude-fable-5-1' and attempts[0]['success']
            assert len(terminal)==1 and terminal[0].get('is_error') is False
            assert observed=={'claude-fable-5-1'}
            outcome='reset'
        else:
            assert len(attempts)==2 and not attempts[0]['success']
            assert attempts[0]['model']=='claude-fable-5-1' and attempts[0]['classification']=='credits'
            assert attempts[0]['reason'].strip()
            assert attempts[1]['success'] and attempts[1]['model']==model==MODEL
            # B2/B5: one proven failed process and one clean-exit success process, by the validator's own rule.
            orderly_shutdown(sup)
            live=runpy.run_path(str(ROOT/'loops/b8.live.py'))
            processes=live['scan_wire'](wire)
            failed=[p for p in processes if live['failure_proof'](p) is not None]
            assert len(failed)==1,'exactly one proven Fable failure process required'
            successes=[p for p in processes if p is not failed[0]]
            assert len(successes)==1 and successes[0]['finalized'] and successes[0]['returncode']==0,'retry process must finish cleanly after orderly shutdown'
            assert [r.get('is_error') for r in successes[0]['results']]==[False],'retry process lacks exactly one successful terminal'
            assert MODEL in observed and observed <= {MODEL,'claude-fable-5-1'}
            outcome='exhausted'
        save(mode_root/'credits.json',{'outcome':outcome,'observed_at':sup.now(),
             'attempts':attempts,'wire':'credits-wire','usage':usage,
             'explanation':'Fable succeeded; exhaustion no longer reproduced.' if outcome=='reset'
                 else 'Fable credits error followed by same-account Opus5.5 success.'})
    finally:
        # B2 (round 3): orderly shutdown on every path, including a credits reset or an exception.
        orderly_shutdown(sup);stop_recorded(wire)

DISCOVERY_PROBES=(('auth-status',['auth','status']),)

def capture_identity_discovery(binary, sources, output):
    """PR45 1.1: does the installed CLI report the account behind an explicitly supplied token? Each probe runs
    in a fresh HOME and CLAUDE_CONFIG_DIR with only that credential's variables (no cached login can leak in),
    plus one control with no credential at all. Outputs are retained through the recorder (secrets redacted);
    the validator derives the conclusion from the retained output, never from this record alone."""
    root=output/'identity-discovery';root.mkdir(mode=0o700)
    records=[]
    for label,source in (('claude-r2d2',sources['claude-r2d2']),('claude-l',sources['claude-l']),('none',None)):
        for probe,argv in DISCOVERY_PROBES:
            wire=root/label/probe;wire.mkdir(mode=0o700,parents=True)
            with tempfile.TemporaryDirectory(prefix='b8-discovery-') as isolated:
                env={k:v for k,v in os.environ.items() if not k.startswith(('CLAUDE','ANTHROPIC'))}
                env.update(HOME=isolated,CLAUDE_CONFIG_DIR=str(Path(isolated)/'config'),XDG_CONFIG_HOME=str(Path(isolated)/'xdg'),
                           B8_REAL_CLAUDE=str(binary),B8_CAPTURE_WIRE_DIR=str(wire))
                if source is not None:
                    env.update({k:v for k,v in S.read_env_file(str(source)).items() if k.startswith(('CLAUDE','ANTHROPIC'))})
                try:
                    try:
                        proc=subprocess.run([sys.executable,str(ROOT/'loops/b8.capture-wire.py'),*argv],env=env,
                                            capture_output=True,text=True,timeout=60,stdin=subprocess.DEVNULL)
                        rc=proc.returncode;timeout=False
                    except subprocess.TimeoutExpired:
                        rc=None;timeout=True
                finally:
                    # 2.4: bounded cleanup on every path, including timeouts; the CLI child runs in its own session.
                    stop_recorded(wire)
                gone={'child_gone':True,'wrapper_gone':True}
                for meta_path in wire.glob('*.json'):
                    meta=json.loads(meta_path.read_text())
                    for role in ('child','wrapper'):
                        pid=meta.get(role+'_pid')
                        if pid and Path('/proc',str(pid)).exists():
                            try:
                                stat=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
                                if stat[19]==meta.get(role+'_start') and stat[0]!='Z':gone[role+'_gone']=False
                            except (FileNotFoundError,IndexError):pass
                records.append({'credential':label,'probe':probe,'argv':argv,'returncode':rc,'timed_out':timeout,
                                'wire':f'identity-discovery/{label}/{probe}','isolated':['HOME','CLAUDE_CONFIG_DIR','XDG_CONFIG_HOME'],
                                'cleanup':gone})
    save(output/'identity-discovery.json',{'records':records,
         'rule':'token_bound only if both credential probes print account-identifying lines that differ from each other and the no-credential control prints none; otherwise not_reported'})

def capture_switches(binary, sources, output, retry_config=None):
    assert not git('status','--porcelain','--untracked-files=all'),'commit a clean candidate first'
    candidate={'candidate_sha':git('rev-parse','HEAD'),'candidate_tree':git('rev-parse','HEAD^{tree}'),
               'capture_harness_sha256':digest(Path(__file__)),
               'wire_harness_sha256':digest(ROOT/'loops/b8.capture-wire.py')}
    output.mkdir(mode=0o700,parents=False,exist_ok=False)
    # 4.2: every scenario runs under an exit-owned scratch login environment; inherited auth never reaches a child.
    scratch=Path(tempfile.mkdtemp(prefix='b8-capture-env-'))
    for name in ('home','xdg','claude'):(scratch/name).mkdir(mode=0o700)
    cleared=[k for k in list(os.environ) if k.startswith(('CLAUDE','ANTHROPIC'))]
    for k in cleared:os.environ.pop(k,None)
    os.environ.update(HOME=str(scratch/'home'),XDG_CONFIG_HOME=str(scratch/'xdg'),CLAUDE_CONFIG_DIR=str(scratch/'claude'))
    candidate['isolation']={'scratch':['HOME','XDG_CONFIG_HOME','CLAUDE_CONFIG_DIR'],'inherited_auth_cleared':True,
                            'cleared_variable_count':len(cleared)}
    save(output/'candidate.json',candidate)
    capture_identity_discovery(binary,sources,output)
    for mode in ('per-turn','persistent'):
        mode_root=output/mode;mode_root.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix='b8-real-switch-') as folder:
            home=Path(folder);(home/'credentials').mkdir(mode=0o700)
            engines={}
            for account,source in sources.items():
                cred=home/'credentials'/(account+'.env')
                # Credential bytes and verification stay only in the private scratch home.
                cred.write_bytes(source.read_bytes());cred.chmod(0o600)
                sidecar=Path(str(source)+'.identity.json')
                if sidecar.exists():Path(str(cred)+'.identity.json').write_bytes(sidecar.read_bytes())
                engines[account]={'kind':'claude','bin':str(ROOT/'loops/b8.capture-wire.py'),'cred':cred.name}
                identity=S.credential_identity(folder,account,engines[account])
                assert identity['verified'] and not identity['mismatch'],'credential identity is unverified'
            save(home/'engine',{'acc':'claude-r2d2','model':MODEL,'mode':mode})
            sup=S.Supervisor(home=folder,engines=engines,poster=lambda *a:None,
                             buildlog_poster=lambda *a:None,engine_timeout=90)
            if retry_config is not None:sup.config['engine_fallback']=retry_config['engine_fallback']
            switches=[];sid=None;wires=[]
            try:
                for account in ('claude-r2d2','claude-l'):
                    wire=mode_root/account;wire.mkdir(mode=0o700);wires.append(wire)
                    os.environ['B8_REAL_CLAUDE']=str(binary)
                    os.environ['B8_CAPTURE_WIRE_DIR']=str(wire)
                    save(home/'engine',{'acc':account,'model':MODEL,'mode':mode})
                    usages=[]
                    for request in range(2):
                        rc,out,err,usage=sup.invoke(account,engines[account],
                            f'Reply only B8_ACCOUNT_SWITCH_OK_{request}. Do not use tools.',model=MODEL)
                        assert rc==0,'real account-switch invocation failed; inspect private wire evidence'
                        assert out.strip()==f'B8_ACCOUNT_SWITCH_OK_{request}','reply belongs to another request'
                        assert usage and isinstance(usage.get('input_tokens'),int),'per-request usage missing'
                        usages.append(usage)
                    effective=successful_model(wire,2)
                    processes=list(wire.glob('*.json'))
                    assert len(processes)==(1 if mode=='persistent' else 2),'unexpected process reuse'
                    current=sup.session_id()
                    assert current and (sid is None or sid==current),'account switch changed conversation'
                    sid=current
                    switches.append({'engine':account,'requested_model':MODEL,'effective_model':effective,
                                     'success':True,'session_id':sid,'usage':usages,'wire':account})
                save(mode_root/'account_switch.json',switches)
                capture_auth(home,dict(engines),mode,mode_root)
                capture_models(sup,home,engines,mode,mode_root)
                os.environ['B8_CAPTURE_WIRE_DIR']=str(mode_root/'claude-l')
                capture_credits(sup,home,engines,mode_root)
            finally:
                orderly_shutdown(sup)
                for wire in wires:stop_recorded(wire)
    save(output/'forced-cleanup.json',FORCED)
    assert candidate['candidate_sha']==git('rev-parse','HEAD')
    assert not git('status','--porcelain','--untracked-files=all'),'candidate changed during capture'
    write_manifest(output,candidate,binary)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--binary',type=Path,required=True)
    parser.add_argument('--r2d2-credential',type=Path,required=True)
    parser.add_argument('--l-credential',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--retry-config',type=Path,help='Sanitized JSON containing only engine_fallback.claude_models')
    args=parser.parse_args()
    os.umask(0o077)
    retry_config=None
    if args.retry_config:
        retry_config=json.loads(args.retry_config.read_text())
        assert set(retry_config)=={'engine_fallback'}
        assert set(retry_config['engine_fallback'])=={'claude_models'}
        models=retry_config['engine_fallback']['claude_models']
        assert isinstance(models,list) and all(isinstance(m,str) and m.startswith('claude-') for m in models)
    capture_switches(args.binary.resolve(strict=True),
                     {'claude-r2d2':args.r2d2_credential.resolve(strict=True),
                      'claude-l':args.l_credential.resolve(strict=True)},args.output.resolve(),retry_config)
    import runpy
    # Structural self-check only; the operator attestation (B4) is written afterwards and checked by the exit.
    runpy.run_path(str(ROOT/'loops/b8.live.py'))['validate'](args.output.resolve(),require_attestation=False)
    output=args.output.resolve()
    manifest=json.loads((output/'manifest.json').read_text())
    print('b8 capture complete: candidate evidence validated (unattested)')
    print('attest: candidate_sha', json.loads((output/'summary.json').read_text())['candidate_sha'])
    print('attest: manifest_sha256', digest(output/'manifest.json'))
    print('attest: summary_sha256', manifest['summary.json'])
