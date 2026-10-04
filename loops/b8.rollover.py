#!/usr/bin/env python3
"""Exit-owned emergency rollover: real wire failure, production routing and idle tick."""
import hashlib,json,os,shutil,signal,sys,tempfile,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'manager'))
import supervisor as S

def scenario(error, working, expected, replacement=None):
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
        (home/'engine').write_text(json.dumps({'acc':account,'model':'claude-opus-5-5','mode':'per-turn'}))
        engines={account:{'bin':str(binary),'kind':'claude','cred':'fixture.env'}}
        config={'engine_fallback':{'claude_models':[]},'compaction':{'rollover_enabled':True}}
        sup=S.Supervisor(home=folder,engines=engines,config=config,poster=lambda *a:None,
                         buildlog_poster=lambda *a:None,engine_timeout=1)
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
        sup.run_once()
        new=(home/'session-id').read_text().strip()
        assert (new!=old)==expected,(error,working,old,new)
        assert (memory.read_bytes(),transcript.read_bytes())==before,'rollover lost durable state'
        attempts=S.read_jsonl(str(home/'logs'/'attempts.jsonl'))
        assert any(not x['success'] for x in attempts),'failed resume attempt missing'
        if expected:
            uuid.UUID(new)
            sup.run_engines('after emergency rollover',[])
            sup.run_once()
            assert (home/'session-id').read_text().strip()==new,'emergency replayed twice'
        # Per-turn children must all be reaped by invoke; no live fixture should remain.
        for row in S.read_jsonl(str(home/'wire.jsonl')):
            if row['kind']=='start':
                try:
                    argv=(Path('/proc')/str(row['pid'])/'cmdline').read_bytes().split(b'\0')
                    assert str(binary).encode() not in argv,'fixture child not reaped'
                except FileNotFoundError:pass

def contracts():
    for error in ('Invalid authentication token','Out of credits','Rate limit exceeded',
                  'Connection timed out','Unrecognized failure'):
        scenario(error,True,False)
    scenario('Prompt is too long',False,False)
    scenario('Prompt is too long',True,False,replacement='stale')
    scenario('Prompt is too long',True,False,replacement='verified')
    scenario('Prompt is too long',True,True)

if __name__=='__main__':contracts()
