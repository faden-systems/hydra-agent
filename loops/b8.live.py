#!/usr/bin/env python3
"""Exit-owned real-evidence validator; missing or inconsistent evidence fails closed."""
import hashlib,json,os,re,runpy,subprocess,sys
from pathlib import Path

def validate(root):
    root=Path(root).resolve()
    summary=json.loads((root/'summary.json').read_text())
    assert re.fullmatch('[0-9a-f]{40}',summary['candidate_sha'])
    candidate=Path(__file__).resolve().parents[1]
    def git(*args):
        return subprocess.check_output(['git','-C',str(candidate),*args]).decode().strip()
    assert summary['candidate_sha']==git('rev-parse','HEAD'), 'evidence is for another candidate'
    assert not git('status','--porcelain','--untracked-files=all'), 'candidate must be committed and clean'
    assert summary['candidate_tree']==git('rev-parse','HEAD^{tree}'), 'candidate tree differs'
    assert summary['capture_harness_sha256']==hashlib.sha256((candidate/'loops/b8.capture.py').read_bytes()).hexdigest(), 'capture harness differs'
    assert summary['wire_harness_sha256']==hashlib.sha256((candidate/'loops/b8.capture-wire.py').read_bytes()).hexdigest()
    assert summary['binary_version'] and summary['binary_path'].startswith('/')
    assert re.fullmatch('[0-9a-f]{64}',summary['binary_sha256'])
    manifest=json.loads((root/'manifest.json').read_text())
    assert manifest
    for name,digest in manifest.items():
        p=(root/name).resolve()
        assert p.is_relative_to(root) and p.is_file(),name
        assert hashlib.sha256(p.read_bytes()).hexdigest()==digest,name
    assert 'summary.json' in manifest
    assert set(summary['modes'])=={'per-turn','persistent'}
    for mode,record in summary['modes'].items():
        # Each item references independently retained real supervisor output, not boolean claims.
        def artifact(key):
            name=record[key];assert name in manifest,(mode,key)
            return json.loads((root/name).read_text())
        def wire_rows(directory):
            prefix=f'{mode}/{directory}/'
            rows=[]
            for name in manifest:
                if name.startswith(prefix) and name.endswith('.stdout'):
                    for line in (root/name).read_text().splitlines():
                        try:row=json.loads(line)
                        except json.JSONDecodeError:continue
                        rows.append(row)
            return rows
        def provenance(directory, count=None):
            prefix=f'{mode}/{directory}/'
            metadata=[name for name in manifest if name.startswith(prefix) and name.endswith('.json')]
            assert metadata,'CLI provenance missing'
            if count is not None:assert len(metadata)==count,'unexpected CLI process count'
            for name in metadata:
                meta=json.loads((root/name).read_text())
                assert meta['binary_path']==summary['binary_path']
                assert meta['binary_sha256']==summary['binary_sha256']
                assert meta['child_pid'] and meta['wrapper_pid'] and meta['child_start']
                argv=meta['argv']
                assert ('--input-format' in argv)==(mode=='persistent')
                if mode=='persistent':assert argv[argv.index('--input-format')+1]=='stream-json'
                assert name[:-5]+'.stdout' in manifest and name[:-5]+'.stderr' in manifest
        audit=artifact('model_audit')
        config=audit['routing_config']
        assert set(config)=={'engine_fallback'}
        assert set(config['engine_fallback']) <= {'claude_models'}
        discover=runpy.run_path(str(candidate/'loops/b8.capture.py'))['discover_retries']
        assert audit['retry_models']==discover(config),'retry models differ from captured routing configuration'
        aliases=json.loads((candidate/'manager/models.json').read_text())['claude']['aliases']
        assert audit['aliases']==aliases
        required=set([aliases['opus5'],aliases['opus5.5'],*audit['retry_models']])
        assert required=={r['requested_model'] for r in audit['probes']}
        assert len(audit['probes'])==len(required),'duplicate model probes'
        assert len({r['wire'] for r in audit['probes']})==len(required),'reused model evidence'
        for probe in audit['probes']:
            provenance(probe['wire'], count=1)
            assert probe['returncode']==0 and probe['reported_models']==[probe['requested_model']]
            rows=wire_rows(probe['wire'])
            results=[r for r in rows if r.get('type')=='result']
            assert len(results)==1 and results[0].get('is_error') is False
            models={r['message']['model'] for r in rows if r.get('type')=='assistant'
                    and isinstance(r.get('message'),dict) and r['message'].get('model')}
            assert models=={probe['requested_model']}
        switches=artifact('account_switch')
        assert len(switches)==2
        assert {x['engine'] for x in switches}=={'claude-l','claude-r2d2'}
        assert all(x['requested_model']==x['effective_model']=='claude-opus-5-5' and x['success'] for x in switches)
        assert switches[0]['session_id'] and switches[0]['session_id']==switches[1]['session_id']
        for switch in switches:
            rows=wire_rows(switch['wire'])
            results=[r for r in rows if r.get('type')=='result']
            assert len(results)==2 and all(r.get('is_error') is False for r in results)
            assert len(switch['usage'])==2 and all(isinstance(u.get('input_tokens'),int) for u in switch['usage'])
            # Split at actual terminal boundaries within each process, never file/PID order.
            segments=[]
            for name in manifest:
                if not (name.startswith(f"{mode}/{switch['wire']}/") and name.endswith('.stdout')):continue
                first_usage=None
                for line in (root/name).read_text().splitlines():
                    try:row=json.loads(line)
                    except json.JSONDecodeError:continue
                    if row.get('type')=='assistant' and first_usage is None:
                        first_usage=(row.get('message') or {}).get('usage')
                    if row.get('type')=='result':
                        segments.append((row,first_usage));first_usage=None
            for index,reported in enumerate(switch['usage']):
                matches=[pair for pair in segments if pair[0].get('result','').strip()==f'B8_ACCOUNT_SWITCH_OK_{index}']
                assert len(matches)==1,'missing or duplicate request terminal'
                terminal,context=matches[0]
                assert terminal.get('session_id')==switch['session_id'],'terminal conversation mismatch'
                raw=terminal.get('usage');assert isinstance(raw,dict) and 'input_tokens' in raw
                values={k:v for k,v in raw.items() if 'tokens' in k and isinstance(v,(int,float)) and not isinstance(v,bool)}
                assert all(v>=0 for v in values.values())
                assert reported['input_tokens']==int(sum(v for k,v in values.items() if 'input' in k))
                assert reported['tokens']==int(sum(values.values()))
                assert isinstance(context,dict) and 'input_tokens' in context,'first assistant usage missing'
                assert reported['context_tokens']==sum(context.get(k,0) for k in
                    ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens'))
            process_records=[name for name in manifest if name.startswith(f"{mode}/{switch['wire']}/") and name.endswith('.json')]
            assert len(process_records)==(1 if mode=='persistent' else 2)
            models={r['message']['model'] for r in rows if r.get('type')=='assistant'
                    and isinstance(r.get('message'),dict) and r['message'].get('model')}
            assert models=={'claude-opus-5-5'}
        attempts=artifact('attempts')
        auth=[a for a in attempts if a['classification']=='auth' and not a['success']]
        assert len(auth)==1 and auth[0]['reason'].strip(),(mode,auth)
        fallback_results=[r for r in wire_rows('auth-fallback') if r.get('type')=='result']
        assert len(fallback_results)==2 and sum(r.get('is_error') is True for r in fallback_results)==1
        recovery_rows=wire_rows('auth-recovery')
        recovery_results=[r for r in recovery_rows if r.get('type')=='result']
        assert len(recovery_results)==1 and recovery_results[0].get('is_error') is False
        for phase_rows in (wire_rows('auth-fallback'),recovery_rows):
            models={r['message']['model'] for r in phase_rows if r.get('type')=='assistant'
                    and isinstance(r.get('message'),dict) and r['message'].get('model')}
            assert models=={'claude-opus-5-5'},'auth scenario model mismatch'
        replies=artifact('replies')
        assert len(replies)==2 and all('B8_AUTH_OK' in r['text'] for r in replies)
        before,after=artifact('status_before'),artifact('status_after')
        assert before['configured']['acc']!=before['effective']['acc']
        assert before['reason']
        for key,account in (('status_text_before','claude-l'),('status_text_after','claude-r2d2')):
            name=record[key];assert name in manifest
            status=(root/name).read_text()
            assert account in status and 'claude-opus-5-5' in status
            assert 'verified ' in status and mode in status
            if key=='status_text_before':
                assert 'configured' in status and 'effective' in status and 'claude-r2d2' in status
                assert before['reason'].strip()[:200] in status
        for key in ('identity_before','identity_after'):
            identity=artifact(key)
            assert identity['verified'] and not identity['mismatch'] and identity['verified_at']
        assert after['configured']['acc']==after['effective']['acc']
        alerts=artifact('alerts')
        assert sum('fallback started' in a['text'] for a in alerts)==1
        assert sum('fallback ended' in a['text'] for a in alerts)==1
        writes=artifact('selection_writes')
        assert len([x for x in writes if x['cause']=='fallback'])==1
        ledger=artifact('ledger');turns=artifact('turns')
        # Production ledger rows carry turn numbers, not invented request_id/success fields.
        assert len(attempts)==3 and len(ledger)==len(turns)==2
        assert [x['turn'] for x in ledger]==[1,2]
        assert [x['n'] for x in turns]==[1,2] and all('error' not in x for x in turns)
        assert [x['engine'] for x in ledger]==['claude-l','claude-r2d2']
        assert [x['engine'] for x in turns]==[x['engine'] for x in ledger]
        assert all(x['model']=='claude-opus-5-5' for x in ledger+turns)
        credit=artifact('credits')
        assert credit['outcome'] in ('exhausted','reset')
        wire_prefix=f"{mode}/{credit['wire']}/"
        raw=[name for name in manifest if name.startswith(wire_prefix) and name.endswith('.stdout')]
        assert raw,'credits wire evidence missing'
        for directory in ('claude-l','claude-r2d2','auth-fallback','auth-recovery',credit['wire']):
            provenance(directory)
        if credit['outcome']=='exhausted':
            seq=credit['attempts']
            assert seq[0]['model']=='claude-fable-5-1' and seq[0]['classification']=='credits'
            assert seq[1]['engine']==seq[0]['engine']=='claude-l'
            assert seq[1]['model']!='claude-fable-5-1' and seq[1]['success']
            results=[r for r in wire_rows(credit['wire']) if r.get('type')=='result']
            assert len(results)==2 and sum(r.get('is_error') is True for r in results)==1
        else:
            assert credit['observed_at'] and credit['explanation']
            seq=credit['attempts']
            assert len(seq)==1 and seq[0]['success'] and seq[0]['engine']=='claude-l'
            assert seq[0]['model']=='claude-fable-5-1'
            results=[r for r in wire_rows(credit['wire']) if r.get('type')=='result']
            assert len(results)==1 and results[0].get('is_error') is False
    return summary

if __name__=='__main__':
    path=os.environ.get('B8_LIVE_EVIDENCE')
    if not path:raise SystemExit('BLOCKED: B8_LIVE_EVIDENCE missing; real binary acceptance required')
    validate(path)
    print('b8 real evidence validated')
