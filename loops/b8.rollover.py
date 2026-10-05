#!/usr/bin/env python3
"""Exit-owned emergency rollover: real wire failure, production routing and idle tick."""
import datetime as dt
import hashlib,json,os,shutil,signal,sys,tempfile,uuid
from unittest.mock import patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'manager'))
import supervisor as S

def present(pid):return (Path('/proc')/str(pid)).exists()

def scenario(error, working, expected, replacement=None, replacement_failure=False, mode='per-turn'):
    with tempfile.TemporaryDirectory(prefix='b8-rollover-') as folder:
        home=Path(folder);binary=home/'fake-claude'
        shutil.copyfile(ROOT/'loops/b8.fake.py',binary);binary.chmod(0o755)
        account='claude-r2d2';token='dummy-rollover-token'
        (home/'credentials').mkdir();cred=home/'credentials'/'fixture.env'
        cred.write_text('CLAUDE_CODE_OAUTH_TOKEN='+token+'\n');cred.chmod(0o600)
        Path(str(cred)+'.identity.json').write_text(json.dumps({'schema_version':1,'label':account,
            'account_id':'fixture-r2d2','token_sha256':hashlib.sha256(token.encode()).hexdigest(),
            'verified_at':'2026-10-04T20:00:00Z','method':'private-window-and-usage-bar',
            'evidence':'https://app.slack.com/archives/Cfixture/p123'}))
        (home/'engine').write_text(json.dumps({'acc':account,'model':'claude-opus-5-5','mode':mode}))
        engines={account:{'bin':str(binary),'kind':'claude','cred':'fixture.env'}}
        config={'engine_fallback':{'claude_models':[]},'compaction':{'rollover_enabled':True}}
        now=[dt.datetime(2026,10,5,12,tzinfo=dt.timezone.utc)]
        notices=[]
        def buildlog(*args):notices.append(args)
        def supervisor():
            return S.Supervisor(home=folder,engines=engines,config=config,poster=lambda *a:None,
                                buildlog_poster=buildlog,engine_timeout=1,clock=lambda:now[0])
        sup=supervisor()
        sup.ensure_memory_layout()
        memory=Path(sup.memory_dir)/'MEMORY.md';memory.write_text('Durable sentinel: retain me.\n')
        transcript=home/'preserved-conversation.jsonl';transcript.write_text('old conversation sentinel\n')
        before=(memory.read_bytes(),transcript.read_bytes())
        old=str(uuid.uuid4());(home/'session-id').write_text(old+'\n')
        (home/'session-started').write_text('1\n')
        if working:
            sup.run_engines('warmup successful account',[])
        if replacement:
            cred.write_text('CLAUDE_CODE_OAUTH_TOKEN=dummy-replaced-token\n')
            if replacement=='verified':
                sidecar=Path(str(cred)+'.identity.json');meta=json.loads(sidecar.read_text())
                meta['token_sha256']=hashlib.sha256(b'dummy-replaced-token').hexdigest()
                sidecar.write_text(json.dumps(meta))
            # Even freshly verified metadata cannot transfer the old credential's successful probe.
        # A real resumed child now returns the wire error; no mocked classifier/engine.
        (home/'resume-error.json').write_text(json.dumps({'sid':old,'error':error}))
        failed=False
        try:sup.run_engines('failed resume request',[])
        except S.AllEnginesFailed:failed=True
        assert failed,'failed resume was silently reported as a success'
        assert (home/'session-id').read_text().strip()==old,'failure rotated inline before boundary'
        if replacement_failure:
            replace=os.replace
            writes=[]
            def fail_session_write(source,destination):
                if Path(destination)==home/'session-id':
                    writes.append(str(destination))
                    raise OSError('fixture session replacement unavailable')
                return replace(source,destination)
            with patch.object(os,'replace',side_effect=fail_session_write):
                sup.run_once()
                assert len(writes)==1,'qualified emergency did not attempt atomic replacement'
                assert (home/'session-id').read_text().strip()==old
                assert len(notices)==1,'rollover failure must produce one notice'
                for _ in range(3):sup.run_once()
                assert len(writes)==1,'failed rollover retried before backoff'
                sup=supervisor()
                sup.run_once()
                assert len(writes)==1,'restart forgot emergency rollover backoff'
            assert (memory.read_bytes(),transcript.read_bytes())==before
            assert not any(row.get('kind')=='compaction' for row in S.read_jsonl(str(home/'logs'/'turns.jsonl')))
            now[0]+=dt.timedelta(hours=7)
        def rows():return S.read_jsonl(str(home/'wire.jsonl'))
        def starts():return [r for r in rows() if r['kind']=='start']
        old_requests=len([r for r in rows() if r['kind']=='request' and r['pid'] in {x['pid'] for x in starts() if x['sid']==old}])
        sup.run_once()
        new=(home/'session-id').read_text().strip()
        assert (new!=old)==expected,(error,working,old,new)
        assert (memory.read_bytes(),transcript.read_bytes())==before,'rollover lost durable state'
        attempts=S.read_jsonl(str(home/'logs'/'attempts.jsonl'))
        assert any(not x['success'] for x in attempts),'failed resume attempt missing'
        if expected:
            uuid.UUID(new)
            old_pids={x['pid'] for x in starts() if x['sid']==old}
            if mode=='persistent':
                # PR45 8.1: the old conversation's process is reaped at the rollover boundary; it can never
                # keep writing the old conversation after the new session id is in place.
                assert old_pids,'persistent process for the old conversation never started'
                assert not any(present(pid) for pid in old_pids),'old persistent child still present after rollover'
            sup.run_engines('after emergency rollover',[])
            fresh=[x for x in starts() if x['sid']==new]
            assert fresh and fresh[-1]['pid'] not in old_pids,'next request did not run in a fresh process on the new session'
            assert '--session-id' in fresh[-1]['argv'],'new conversation must start with --session-id, never --resume'
            after=[r for r in rows() if r['kind']=='request' and r['pid'] in old_pids]
            assert len(after)==old_requests,'old process served a request after the rollover'
            sup.run_once()
            assert (home/'session-id').read_text().strip()==new,'emergency replayed twice'
            # Restart bookkeeping: a reconstructed supervisor keeps the new session, never revives the old
            # process or rolls over again, and reports the live process truthfully.
            sup=supervisor();sup.run_once()
            assert (home/'session-id').read_text().strip()==new,'restart lost the rolled-over session'
            sup.run_engines('after restart',[])
            assert (home/'session-id').read_text().strip()==new
            assert not any(x['sid']==old for x in starts()[len(starts())-2:]),'restart revived the old conversation'
            assert (memory.read_bytes(),transcript.read_bytes())==before
            if mode=='persistent':
                status=S.persistent_status(folder)
                assert status['state']=='running' and status['pid']==starts()[-1]['pid'],status
                assert not any(present(pid) for pid in old_pids)
        if mode=='persistent':
            # Fixture-only cleanup of the one legitimate live writer; anything else alive is a leak.
            live=[x['pid'] for x in starts() if present(x['pid'])]
            allowed={S.persistent_status(folder).get('pid')} if hasattr(S,'persistent_status') else set()
            leaked=[pid for pid in live if pid not in allowed]
            for pid in live:
                try:os.killpg(pid,signal.SIGKILL)
                except (ProcessLookupError,PermissionError):
                    try:os.kill(pid,signal.SIGKILL)
                    except ProcessLookupError:pass
            assert not leaked,('persistent fixture processes leaked',leaked)
        else:
            # Per-turn children must all be reaped by invoke; no live fixture should remain.
            for row in rows():
                if row['kind']=='start':
                    try:
                        argv=(Path('/proc')/str(row['pid'])/'cmdline').read_bytes().split(b'\0')
                        assert str(binary).encode() not in argv,'fixture child not reaped'
                    except FileNotFoundError:pass

def contracts():
    # PR45 8.1: the whole emergency matrix runs in BOTH modes; persistent adds old-child reaping,
    # fresh-process/new-session checks and restart bookkeeping.
    for mode in ('per-turn','persistent'):
        for error in ('Invalid authentication token','Out of credits','Rate limit exceeded',
                      'Connection timed out','Unrecognized failure'):
            scenario(error,True,False,mode=mode)
        scenario('Prompt is too long',False,False,mode=mode)
        scenario('Prompt is too long',True,False,replacement='stale',mode=mode)
        scenario('Prompt is too long',True,False,replacement='verified',mode=mode)
        scenario('Prompt is too long',True,True,mode=mode)
        scenario('Prompt is too long',True,True,replacement_failure=True,mode=mode)
        print('emergency rollover matrix PASS:',mode)

if __name__=='__main__':contracts()
