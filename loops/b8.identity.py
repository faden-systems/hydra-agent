#!/usr/bin/env python3
"""Exit-owned identity contracts using generated dummy secrets only."""
import hashlib,json,sys,tempfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'manager'))
import supervisor as S

def contracts():
    assert hasattr(S,'credential_identity'), 'missing credential identity resolver'
    with tempfile.TemporaryDirectory(prefix='b8-identity-') as tmp:
        h=Path(tmp); c=h/'credentials';c.mkdir()
        token='b8-dummy-token-not-a-credential'
        cred=c/'claude-l.env';cred.write_text('CLAUDE_CODE_OAUTH_TOKEN='+token+'\n');cred.chmod(0o600)
        spec={'bin':'unused-fixture','kind':'claude','cred':'claude-l.env'}
        meta=Path(str(cred)+'.identity.json')
        def identity(observed=None):
            return S.credential_identity(str(h),'claude-l',spec,observed=observed)
        def check(value,verified,allowed):
            assert value['verified'] is verified,value
            assert value['automatic_allowed'] is allowed,value
            assert token not in json.dumps(value), 'identity result leaked token'
        check(identity(),False,False)
        doc={'schema_version':1,'label':'claude-l','account_id':'fixture-L',
             'token_sha256':hashlib.sha256(token.encode()).hexdigest(),
             'verified_at':'2026-10-04T20:00:00Z','method':'private-window-and-usage-bar',
             'evidence':'https://app.slack.com/archives/Cfixture/p123'}
        meta.write_text(json.dumps(doc))
        result=identity();check(result,True,True)
        assert result['account_id']=='fixture-L' and result['verified_at']==doc['verified_at']
        assert result.get('source')=='manual-sidecar' and result.get('method')==doc['method'],'identity must report its source and method (PR45 1.1)'
        for key in ('verified_at','method','evidence','token_sha256','account_id'):
            broken=dict(doc);broken.pop(key);meta.write_text(json.dumps(broken))
            check(identity(),False,False)
        for change in ({'label':'claude-r2d2'},{'token_sha256':'0'*64},{'verified_at':'not-a-date'}):
            meta.write_text(json.dumps(dict(doc,**change)));check(identity(),False,False)
        meta.write_text(json.dumps(doc))
        # A reported cached-login identity is never token identity.
        check(identity({'account_id':'fixture-L','token_bound':False}),True,True)
        bad=identity({'account_id':'fixture-R2D2','token_bound':True})
        check(bad,False,False);assert bad['mismatch'] is True,bad
        good=identity({'account_id':'fixture-L','token_bound':True});check(good,True,True)
        cred.write_text('CLAUDE_CODE_OAUTH_TOKEN=replaced-dummy\n')
        check(identity(),False,False)
        # Malformed sidecar must fail closed rather than interrupt supervisor startup.
        meta.write_text('{');check(identity(),False,False)

        # Prove run_engines consults the guard BEFORE invoking a fallback credential.
        engines={n:{'bin':'unused-fixture','kind':'claude'} for n in ('claude-r2d2','claude-l')}
        S.set_engine(str(h),'claude-r2d2','claude-sonnet-5',engines)
        sup=S.Supervisor(home=str(h),engines=engines,poster=lambda *a:None,
                         config={'engine_fallback':{'claude_models':[]}})
        calls=[]
        def invoke(name,*a,**kw):
            calls.append(name)
            return (1,'','Invalid authentication credentials',None) if name=='claude-r2d2' else (0,'WRONG ACCOUNT','',{})
        def resolve(home,name,spec,**kw):
            return {'verified':name=='claude-r2d2','automatic_allowed':name=='claude-r2d2',
                    'account_id':'fixture-R2D2' if name=='claude-r2d2' else None,'mismatch':False,
                    'verified_at':'2026-10-04T20:00:00Z' if name=='claude-r2d2' else None}
        with patch.object(S,'credential_identity',side_effect=resolve),patch.object(sup,'invoke',side_effect=invoke):
            try:sup.run_engines('probe',[])
            except S.AllEnginesFailed:pass
            else:raise AssertionError('unverified fallback was accepted')
        assert calls==['claude-r2d2'],calls
    for text in ('generate a summary','separate accounts','moderate effort'):
        assert S.classify_engine_error(text) not in ('auth','credits','usage_limit','rate_limit'),text
    # 1.1 (round 3): an authoritative observed mismatch blocks session start in production routing, not only
    # in the resolver's return value. The adapter seam is supervisor.observe_identity(home, name, spec).
    assert hasattr(S,'observe_identity'),'missing identity adapter seam observe_identity (requirement 11)'
    with tempfile.TemporaryDirectory(prefix='b8-identity-start-') as tmp:
        h=Path(tmp);(h/'credentials').mkdir();(h/'logs').mkdir()
        token='b8-dummy-start-token';cred=h/'credentials'/'claude-r2d2.env'
        cred.write_text('CLAUDE_CODE_OAUTH_TOKEN='+token+'\n');cred.chmod(0o600)
        Path(str(cred)+'.identity.json').write_text(json.dumps({'schema_version':1,'label':'claude-r2d2','account_id':'fixture-R2D2',
            'token_sha256':hashlib.sha256(token.encode()).hexdigest(),'verified_at':'2026-10-04T20:00:00Z',
            'method':'private-window-and-usage-bar','evidence':'https://app.slack.com/archives/Cfixture/p123'}))
        engines={'claude-r2d2':{'bin':'unused-fixture','kind':'claude','cred':'claude-r2d2.env'}}
        S.set_engine(str(h),'claude-r2d2','claude-sonnet-5',engines)
        sup=S.Supervisor(home=str(h),engines=engines,poster=lambda *a:None,config={'engine_fallback':{'claude_models':[]}})
        calls=[]
        def invoke(name,*a,**kw):
            calls.append(name);return (0,'WRONG ACCOUNT REPLY','',{})
        def observe(home,name,spec,**kw):
            return {'account_id':'fixture-OTHER','token_bound':True}
        with patch.object(S,'observe_identity',side_effect=observe),patch.object(sup,'invoke',side_effect=invoke):
            try:sup.run_engines('must not reach the child',[])
            except S.AllEnginesFailed:pass
            else:raise AssertionError('mismatched token identity did not block session start')
        assert calls==[],'a user request reached the child despite an authoritative identity mismatch'
        attempts=S.read_jsonl(str(h/'logs'/'attempts.jsonl'))
        assert attempts and not any(a.get('success') for a in attempts),'no truthful failed attempt recorded for the blocked start'
        assert 'mismatch' in S.status_text(str(h)).lower(),'status does not report the identity mismatch'
        assert token not in S.status_text(str(h))

if __name__=='__main__':
    contracts()
    print('identity contracts PASS')
