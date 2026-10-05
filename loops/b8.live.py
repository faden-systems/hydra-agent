#!/usr/bin/env python3
"""Exit-owned real-evidence validator; missing or inconsistent evidence fails closed."""
import hashlib,json,os,re,runpy,subprocess,sys
from pathlib import Path

ATTESTATION_KEYS=('candidate_sha','binary_path','binary_sha256','binary_version','manifest_sha256','summary_sha256',
                  'attested_by','attested_at','evidence')

def pinned_binary(summary):
    """B4: the installed CLI is pinned by the operator out of band (B8_PINNED_BINARY_PATH/SHA256 from the recorded
    launch prerequisite), the summary must name exactly that binary, and the exit host re-hashes the installed
    file itself. Implementer-supplied fields can therefore never vouch for each other."""
    pin_path=os.environ.get('B8_PINNED_BINARY_PATH') or '';pin_sha=(os.environ.get('B8_PINNED_BINARY_SHA256') or '').lower()
    assert pin_path.startswith('/') and re.fullmatch('[0-9a-f]{64}',pin_sha),'operator-pinned installed binary path and sha256 required'
    assert summary['binary_path']==pin_path,'summary names a binary other than the pinned installed CLI'
    assert summary['binary_sha256']==pin_sha,'summary binary digest differs from the operator pin'
    installed=Path(pin_path)
    assert installed.is_file(),'pinned binary missing on the exit host: run the exit on the capture host'
    assert hashlib.sha256(installed.read_bytes()).hexdigest()==pin_sha,'installed binary differs from the operator pin'
    return pin_path,pin_sha

def operator_attestation(root,summary,manifest,pin_path,pin_sha):
    """B4: an operator-written attestation outside the manifest binds the candidate, the pinned binary and the exact
    bundle (manifest and summary digests). A bundle without it, or with digests that do not match, is rejected."""
    location=os.environ.get('B8_LIVE_ATTESTATION') or ''
    assert location.startswith('/'),'B8_LIVE_ATTESTATION must name the operator-controlled attestation file (never a bundle default)'
    path=Path(location).resolve()
    assert path.is_file(),'operator attestation missing'
    try:
        path.relative_to(root)
    except ValueError:pass
    else:raise AssertionError('attestation must live outside the implementer bundle')
    mode=path.stat().st_mode;parent_mode=path.parent.stat().st_mode
    assert not (mode&0o022) and not (parent_mode&0o022),'attestation file and directory must not be writable by group or others'
    manifest_bytes=(root/'manifest.json').read_bytes()
    try:rel=str(path.relative_to(root))
    except ValueError:rel=None
    assert rel not in manifest and 'attestation.json' not in manifest,'attestation must not be part of the implementer bundle'
    att=json.loads(path.read_text())
    assert isinstance(att,dict) and all(isinstance(att.get(k),str) and att[k].strip() for k in ATTESTATION_KEYS),'attestation fields incomplete'
    assert set(att)>=set(ATTESTATION_KEYS)
    assert att['candidate_sha']==summary['candidate_sha'],'attestation is for another candidate'
    assert att['binary_path']==pin_path and att['binary_sha256'].lower()==pin_sha,'attestation names another binary'
    assert att['binary_version']==summary['binary_version'],'attestation CLI version differs'
    assert att['manifest_sha256'].lower()==hashlib.sha256(manifest_bytes).hexdigest(),'attestation does not match this manifest'
    assert att['summary_sha256'].lower()==manifest['summary.json'],'attestation does not match this summary'
    assert att['attested_by']=='Hermes','attestation must come from the admitted VM operator'
    assert att['evidence'].startswith('https://'),'attestation needs a durable evidence link'
    return att

IDENTIFIER_KEYS=('email','account','account_id','account id','organization','organization_id','org','user','user_id','login','logged in as')
ABSENT_VALUES={'','-','none','null','n/a','not logged in','unknown','(none)','logged out'}
LOGGED_IN_RE=re.compile(r'^\s*(?P<key>logged in as)\s+(?P<value>\S.*?)\s*$',re.I)
IDENTIFIER_RE=re.compile(r'^\s*(?P<key>[a-z][a-z _-]{1,30}?)\s*[:=]\s*(?P<value>.*?)\s*$',re.I)

def identifier_values(text):
    """B1: documented account-identifier fields (key: value) in retained CLI output. Plan, tier or model lines
    never identify an account; empty, null or logged-out values are absence; redacted secrets never count."""
    found={}
    for line in text.splitlines():
        m=IDENTIFIER_RE.match(line) or LOGGED_IN_RE.match(line)
        if not m:continue
        key=m.group('key').strip().lower();value=m.group('value').strip()
        if key not in IDENTIFIER_KEYS or '[redacted]' in value.lower():continue
        if value.strip('"\'').lower() in ABSENT_VALUES:continue
        found[key]=value
    return found

def scan_wire(directory):
    """One record per recorded CLI process in a recorder directory (no manifest): finalized exit code,
    terminal results and retained diagnostics. Shared by the validator and the capture harness (B2)."""
    directory=Path(directory);records=[]
    for meta_path in sorted(directory.glob('*.json')):
        meta=json.loads(meta_path.read_text());stem=meta_path.with_suffix('')
        stdout=Path(str(stem)+'.stdout').read_text() if Path(str(stem)+'.stdout').exists() else ''
        stderr=Path(str(stem)+'.stderr').read_text() if Path(str(stem)+'.stderr').exists() else ''
        results=[];plain=[]
        for line in stdout.splitlines():
            try:row=json.loads(line)
            except json.JSONDecodeError:
                if line.strip():plain.append(line.strip())
                continue
            if isinstance(row,dict) and row.get('type')=='result':results.append(row)
        rc=meta.get('returncode')
        records.append({'name':str(stem),'pid':meta.get('child_pid'),'returncode':rc,
                        'finalized':isinstance(rc,int) and not isinstance(rc,bool) and bool(meta.get('ended_at')),
                        'results':results,'stderr':stderr.strip(),'stdout_text':'\n'.join(plain)})
    return records

def terminal_error_text(terminal):
    err=terminal.get('error')
    if isinstance(err,dict) and err.get('message'):return str(err['message'])
    if isinstance(err,str) and err.strip():return err
    result=terminal.get('result')
    return result if isinstance(result,str) and result.strip() else 'the engine reported is_error=true'

def failure_proof(record):
    """B5: a failed invocation is proven by exactly one error terminal (and no success terminal in that
    process), or by a finalized nonzero exit with retained stderr or plain stdout and no terminal at all.
    Returns the diagnostic text, or None when the process did not fail. One process counts once (B2)."""
    errors=[r for r in record['results'] if r.get('is_error') is True]
    if errors:
        assert len(errors)==1,'several error terminals in one process'
        assert not any(r.get('is_error') is False for r in record['results']),'error and success terminals in one process'
        return terminal_error_text(errors[0])
    if record['finalized'] and record['returncode']!=0:
        assert not record['results'],'nonzero exit after a terminal result is ambiguous'
        diagnostic=record['stderr'] or record['stdout_text']
        assert diagnostic,'nonzero exit without retained diagnostics'
        return diagnostic
    return None

def identity_discovery(root, manifest, pin_sha):
    """PR45 1.1: the discovery experiment is retained and the conclusion re-derived from the outputs."""
    assert 'identity-discovery.json' in manifest,'identity discovery record missing'
    record=json.loads((root/'identity-discovery.json').read_text())
    records=record.get('records') or []
    labels={(r.get('credential'),r.get('probe')) for r in records}
    assert {('claude-r2d2','auth-status'),('claude-l','auth-status'),('none','auth-status')}<=labels,'discovery needs both credentials and a no-credential control'
    outputs={}
    for r in records:
        prefix=r['wire'].rstrip('/')+'/'
        metas=[n for n in manifest if n.startswith(prefix) and n.endswith('.json')]
        assert len(metas)==1,('exactly one recorded CLI process per discovery probe',r['wire'])
        meta=json.loads((root/metas[0]).read_text())
        assert meta['binary_sha256']==pin_sha and meta['argv']==r['argv'],'discovery provenance mismatch'
        assert isinstance(meta.get('returncode'),int) or r.get('timed_out') is True,'discovery process not finalized'
        assert set(r.get('isolated') or [])>={'HOME','CLAUDE_CONFIG_DIR'},'discovery ran without cached-login isolation'
        stem=metas[0][:-5]
        assert stem+'.stdout' in manifest and stem+'.stderr' in manifest
        outputs[(r['credential'],r['probe'])]=(root/(stem+'.stdout')).read_text()+'\n'+(root/(stem+'.stderr')).read_text()
    control=identifier_values(outputs[('none','auth-status')])
    assert not control,('no-credential control shows an account identifier: cached login leaked into the isolation',sorted(control))
    rc={r['credential']:r.get('returncode') for r in records if r['probe']=='auth-status'}
    a=identifier_values(outputs[('claude-r2d2','auth-status')]);b=identifier_values(outputs[('claude-l','auth-status')])
    # token_bound only when both credential probes succeeded and report nonempty, distinct identifier values
    # for the same documented field; differing plan/tier lines or broad markers never count.
    shared=[k for k in a if k in b]
    distinct=any(a[k]!=b[k] for k in shared)
    conclusion='token_bound' if rc.get('claude-r2d2')==0 and rc.get('claude-l')==0 and shared and distinct else 'not_reported'
    return conclusion

def validate(root, require_attestation=True):
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
    pin_path,pin_sha=pinned_binary(summary)
    if require_attestation:
        operator_attestation(root,summary,manifest,pin_path,pin_sha)
    conclusion=identity_discovery(root,manifest,pin_sha)
    # 4.2: the whole capture ran under an exit-owned scratch environment, never the operator's login state.
    isolation=summary.get('isolation') or {}
    assert set(isolation.get('scratch') or [])>={'HOME','XDG_CONFIG_HOME','CLAUDE_CONFIG_DIR'},'capture did not isolate HOME/XDG/CLAUDE_CONFIG_DIR'
    assert isolation.get('inherited_auth_cleared') is True,'capture did not clear inherited authentication variables'
    # B3: processes the harness had to kill after an orderly shutdown failed; a successful scenario process may never be among them.
    forced=set()
    if 'forced-cleanup.json' in manifest:
        for row in json.loads((root/'forced-cleanup.json').read_text()):
            forced.add(row.get('child_pid'))
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
        def invocations(directory):
            """One record per recorded CLI process: finalized exit code, terminal results, retained diagnostics."""
            prefix=f'{mode}/{directory}/'
            records=[]
            for name in sorted(manifest):
                if not (name.startswith(prefix) and name.endswith('.json')):continue
                meta=json.loads((root/name).read_text());stem=name[:-5]
                stdout=(root/(stem+'.stdout')).read_text();stderr=(root/(stem+'.stderr')).read_text()
                results=[];plain=[]
                for line in stdout.splitlines():
                    try:row=json.loads(line)
                    except json.JSONDecodeError:
                        if line.strip():plain.append(line.strip())
                        continue
                    if isinstance(row,dict) and row.get('type')=='result':results.append(row)
                records.append({'name':stem,'pid':meta.get('child_pid'),'returncode':meta.get('returncode'),
                                'finalized':isinstance(meta.get('returncode'),int) and not isinstance(meta.get('returncode'),bool) and bool(meta.get('ended_at')),
                                'results':results,'stderr':stderr.strip(),'stdout_text':'\n'.join(plain)})
            return records
        def one_failure_then_success(directory, attempt):
            """Exactly one proven failed process, exactly one successful terminal in a different finalized
            process, and the recorded attempt reason reconciled with the retained CLI diagnostic."""
            records=invocations(directory)
            assert records,'CLI process records missing: '+directory
            failed=[(r,failure_proof(r)) for r in records];failed=[(r,d) for r,d in failed if d is not None]
            assert len(failed)==1,f'{directory}: expected one proven failed invocation, found {len(failed)}'
            successes=[r for r in records if r is not failed[0][0]]
            for r in successes:
                assert r['finalized'] and r['returncode']==0,'successful fallback process did not finish cleanly'
                assert r['pid'] not in forced,'successful process was terminated by forced cleanup, not an orderly shutdown (B3)'
                assert [x.get('is_error') for x in r['results']]==[False],'fallback process lacks exactly one successful terminal'
            assert len(successes)==1,f'{directory}: expected one successful fallback process, found {len(successes)}'
            reason=(attempt.get('reason') or '').strip();diagnostic=failed[0][1].strip()
            assert reason and (diagnostic[:120] in reason or reason[:120] in diagnostic),'attempt reason does not match the retained CLI diagnostic'
            assert attempt.get('success') is False
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
        one_failure_then_success('auth-fallback',auth[0])
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
            # 1.1: manual sidecars are acceptable only when discovery shows the CLI does not report accounts.
            assert identity.get('source') in ('cli','manual-sidecar'),identity
            if conclusion=='token_bound':
                assert identity['source']=='cli','CLI reports token identity; manual sidecars are not allowed'
            else:
                assert identity['source']=='manual-sidecar' and identity.get('method'),identity
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
            assert seq[0]['reason'].strip() and not seq[0]['success']
            one_failure_then_success(credit['wire'],seq[0])
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
