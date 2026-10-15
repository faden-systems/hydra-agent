#!/usr/bin/env python3
import importlib.util,json,os,subprocess,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def module(path,name):
 s=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
S=module(ROOT/'manager/supervisor.py','b13_candidate')
def write(home,turns,attempts):
 (home/'logs').mkdir(exist_ok=True)
 (home/'engine.json').write_text(json.dumps({'acc':'claude-r2d2','model':'claude-fable-5-1'}))
 for name,rows in [('turns',turns),('attempts',attempts)]:
  (home/'logs'/f'{name}.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
def attempt(at,engine,success=False,classification='other',reason='Error: Session ID preserved-session is already in use.'):
 from datetime import datetime,timezone
 return dict(at=datetime.fromtimestamp(at,timezone.utc).isoformat(),engine=engine,model='gpt-6-astra' if engine=='codex' else 'claude-fable-5-1',rc=0 if success else 1,success=success,classification=None if success else classification,reason='' if success else reason)
def line(text,prefix):return next(x for x in text.splitlines() if x.startswith(prefix))
with tempfile.TemporaryDirectory() as d:
 h=Path(d); start=1792015200; turns=[]; attempts=[]
 for i in range(12):
  t=start+i*300
  turns.append(dict(n=1700+i,at=t+.25,duration_s=50,engine='codex',model='gpt-6-astra',events=[]))
  attempts.extend([attempt(t+1,'claude-r2d2'),attempt(t+6,'claude-l'),attempt(t+50,'codex',True)])
 write(h,turns,attempts)
 before={p:p.read_bytes() for p in h.rglob('*') if p.is_file()}; out=S.status_text(str(h))
 assert line(out,'engine:')=='engine: configured claude-r2d2 (claude-fable-5-1) | last turn ran on codex (gpt-6-astra) | MISMATCH'
 assert line(out,'off configured engine:')=='off configured engine: 12 turns since 2026-10-14 15:00:00 PDT, reason: session_in_use'
 expected='; '.join([f'{a}/claude-fable-5-1 session_in_use: Error: Session ID preserved-session is already in use.' for a in ['claude-r2d2','claude-l']]+['codex/gpt-6-astra ok'])
 assert line(out,'last turn attempts:')=='last turn attempts: '+expected
 assert line(out,'accounts:')=='accounts: claude-r2d2: session_in_use since 2026-10-14 15:00:01 PDT; claude-l: session_in_use since 2026-10-14 15:00:06 PDT; codex: ok'
 assert before=={p:p.read_bytes() for p in h.rglob('*') if p.is_file()},'status wrote files'
 # Existing lines must remain exactly as on the frozen base.
 base=os.environ.get('BASE_REF')
 if base:
  source=subprocess.check_output(['git','show',base+':manager/supervisor.py'],cwd=ROOT)
  bp=Path(d)/'baseline.py';bp.write_bytes(source);B=module(bp,'b13_baseline')
  prior=B.status_text(str(h)); added=('engine:','last turn attempts:','off configured engine:','accounts:')
  assert [x for x in out.splitlines() if not x.startswith(added)]==[x for x in prior.splitlines() if not x.startswith('engine:')]
 print('PASS real collision sequence and preserved status')
 # In-flight failures do not contaminate last completed turn attempts.
 attempts.append(attempt(start+12*300,'claude-r2d2',reason='new unfinished failure'))
 write(h,turns,attempts);assert line(S.status_text(str(h)),'last turn attempts:')=='last turn attempts: '+expected
 # Usage and credits normalize only in status; reset survives.
 t=start+13*300;turns.append(dict(n=1712,at=t,duration_s=20,engine='claude-l',model='claude-fable-5-1',events=[]))
 attempts += [attempt(t+1,'claude-r2d2',classification='usage_limit',reason='Usage limit reached; resets 2026-10-15T03:00:00Z'),attempt(t+20,'claude-l',True)]
 write(h,turns,attempts);out=S.status_text(str(h));assert 'claude-r2d2/claude-fable-5-1 usage_limit:' in out;assert 'reset 2026-10-14 20:00:00 PDT' in out;assert 'claude-l: ok' in out
 attempts[-2]['classification']='credits'; attempts[-2]['reason']='Credit balance is too low'
 write(h,turns,attempts);assert 'claude-r2d2/claude-fable-5-1 usage_limit:' in S.status_text(str(h))
 print('PASS usage and credit limit diagnostics')
 t+=300;turns.append(dict(n=1713,at=t,duration_s=10,engine='claude-r2d2',model='claude-fable-5-1',events=[]));attempts.append(attempt(t+10,'claude-r2d2',True))
 write(h,turns,attempts);out=S.status_text(str(h));assert 'MISMATCH' not in out;assert 'off configured engine:' not in out;assert 'claude-r2d2: ok' in out
 turns[-1]['model']='another-model';write(h,turns,attempts);assert 'MISMATCH' in S.status_text(str(h));assert 'off configured engine: 1 turns' in S.status_text(str(h))
 print('PASS recovery and model mismatch')
 write(h,[],[]);out=S.status_text(str(h));assert 'last turn ran on unknown (unknown)' in out;assert 'last turn attempts: none recorded' in out;assert 'claude-r2d2: unknown' in out
 # Malformed rows and partial final writes cannot break diagnostics.
 for name in ['turns','attempts']:
  (h/'logs'/f'{name}.jsonl').write_text('null\n[]\n{"at":"bad"}\n{"partial":')
 S.status_text(str(h))
 print('PASS missing and malformed evidence')
print('[b13.acceptance] PASS')
