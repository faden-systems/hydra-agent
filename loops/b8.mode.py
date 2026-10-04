#!/usr/bin/env python3
"""Exit-owned mode/rollback CLI contracts against isolated HYDRA_HOME."""
import hashlib,json,os,subprocess,sys,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'manager'))
import supervisor as S

def contracts():
    aliases=json.loads((ROOT/'manager/models.json').read_text())['claude']['aliases']
    assert aliases['opus5.5']=='claude-opus-5-5'
    with tempfile.TemporaryDirectory(prefix='b8-mode-') as tmp:
        home=Path(tmp)
        env={k:v for k,v in os.environ.items() if not k.startswith(('CLAUDE','ANTHROPIC','HYDRA_'))}
        env['HYDRA_HOME']=tmp
        def cli(*args):
            return subprocess.run([sys.executable,str(ROOT/'manager/hydra'),*args],env=env,
                                  text=True,capture_output=True,timeout=10)
        # Legacy files and empty homes default per-turn.
        assert S.read_engine(tmp)['mode']=='per-turn'
        (home/'engine').write_text('claude-r2d2\n')
        assert S.read_engine(tmp)['mode']=='per-turn'
        for mode in ('persistent','per-turn','persistent'):
            result=cli('engine','mode='+mode)
            assert result.returncode==0,(result.stdout,result.stderr)
            pair=S.read_engine(tmp)
            assert pair['mode']==mode and pair['acc']=='claude-r2d2',pair
            # A separate CLI process observes persisted selection.
            result=cli('engine');assert mode in result.stdout,result.stdout
        result=cli('engine','model=opus5.5')
        assert result.returncode==0,(result.stdout,result.stderr)
        assert S.read_engine(tmp)['model']=='claude-opus-5-5'
        saved=(home/'engine').read_bytes()
        result=cli('engine','mode=invalid')
        assert result.returncode!=0
        assert (home/'engine').read_bytes()==saved,'invalid mode partially mutated selection'
        result=cli('engine','acc=claude-l','model=sonnet5')
        assert result.returncode==0,(result.stdout,result.stderr)
        pair=S.read_engine(tmp);assert pair['mode']=='persistent' and pair['acc']=='claude-l',pair
        result=cli('engine','mode=per-turn');assert result.returncode==0
        assert S.read_engine(tmp)['mode']=='per-turn'
        result=cli('status');assert result.returncode==0
        assert 'per-turn' in result.stdout and 'unverified' in result.stdout,result.stdout
        # Metadata is generated from dummy values; no live credential is opened.
        cred=home/'credentials/claude-l.env';cred.parent.mkdir(exist_ok=True)
        token='b8-mode-dummy';cred.write_text('CLAUDE_CODE_OAUTH_TOKEN='+token+'\n');cred.chmod(0o600)
        Path(str(cred)+'.identity.json').write_text(json.dumps({'schema_version':1,'label':'claude-l',
          'account_id':'fixture-L','token_sha256':hashlib.sha256(token.encode()).hexdigest(),
          'verified_at':'2026-10-04T20:00:00Z','method':'private-window-and-usage-bar',
          'evidence':'https://app.slack.com/archives/Cfixture/p123'}))
        result=cli('status');assert result.returncode==0
        assert 'verified 2026-10-04' in result.stdout,result.stdout
        assert token not in result.stdout+result.stderr

if __name__=='__main__':
    contracts()
    print('mode contracts PASS')
