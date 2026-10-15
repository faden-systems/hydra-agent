#!/usr/bin/env python3
import importlib.util,json,os,subprocess,tempfile
from unittest.mock import patch
from contextlib import ExitStack
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def module(path,name):
 s=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
S=module(ROOT/'manager/supervisor.py','b13_candidate')
def status(home):
 # Fixture writes occur outside these guards. Existing status uses no subprocess/network.
 import builtins,io,socket
 real=builtins.open; realio=io.open
 def guarded(fn):
  def call(file,mode='r',*args,**kwargs):
   assert not any(c in mode for c in 'wax+'),'status attempted file mutation'
   return fn(file,mode,*args,**kwargs)
  return call
 def forbidden(*a,**kw):raise AssertionError('status attempted side effect')
 with ExitStack() as stack:
  stack.enter_context(patch('builtins.open',guarded(real)));stack.enter_context(patch('io.open',guarded(realio)))
  for name in ['subprocess.Popen','socket.socket','os.remove','os.unlink','os.rename','os.replace','os.mkdir','os.makedirs']:
   stack.enter_context(patch(name,forbidden))
  return S.status_text(home)
def write(home,turns,attempts):
 (home/'logs').mkdir(exist_ok=True)
 (home/'engine').write_text(json.dumps({'acc':'claude-r2d2','model':'claude-fable-5-1'}))
 for name,rows in [('turns',turns),('attempts',attempts)]:
  (home/'logs'/f'{name}.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in rows))
# Sanitized VM logs/attempts.jsonl, turn1728 Oct15 05:55-05:56 UTC; session UUID replaced.
REAL_ATTEMPTS = [{'at': '2026-10-15T05:55:19Z', 'engine': 'claude-r2d2', 'model': 'claude-fable-5-1', 'rc': 1, 'classification': 'other', 'reason': 'Error: Session ID preserved-session is already in use.', 'success': False}, {'at': '2026-10-15T05:55:24Z', 'engine': 'claude-l', 'model': 'claude-fable-5-1', 'rc': 1, 'classification': 'other', 'reason': 'Error: Session ID preserved-session is already in use.', 'success': False}, {'at': '2026-10-15T05:56:11Z', 'engine': 'codex', 'model': 'gpt-6-astra', 'rc': 0, 'classification': None, 'reason': '', 'success': True}]
def attempt(at,engine,success=False,classification='other',reason='Error: Session ID preserved-session is already in use.'):
 from datetime import datetime,timezone
 return dict(at=datetime.fromtimestamp(at,timezone.utc).isoformat(),engine=engine,model='gpt-6-astra' if engine=='codex' else 'claude-fable-5-1',rc=0 if success else 1,success=success,classification=None if success else classification,reason='' if success else reason)
def line(text,prefix):return next(x for x in text.splitlines() if x.startswith(prefix))
with tempfile.TemporaryDirectory() as d:
 h=Path(d); start=1792015200; turns=[]; attempts=[]
 for i in range(12):
  t=start+i*300
  turns.append(dict(n=1700+i,at=t+.25,duration_s=50,engine='codex',model='gpt-6-astra',events=[]))
  for recorded,offset in zip(REAL_ATTEMPTS,[1,6,50]):
   row=dict(recorded);row['at']=attempt(t+offset,recorded['engine'])['at'];attempts.append(row)
 write(h,turns,attempts)
 before={p:p.read_bytes() for p in h.rglob('*') if p.is_file()}; out=status(str(h))
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
 write(h,turns,attempts);assert line(status(str(h)),'last turn attempts:')=='last turn attempts: '+expected
 # Usage and credits normalize only in status; reset survives.
 t=start+13*300;turns.append(dict(n=1712,at=t,duration_s=20,engine='claude-l',model='claude-fable-5-1',events=[]))
 attempts += [attempt(t+1,'claude-r2d2',classification='other',reason='Usage limit reached; resets 2026-10-15T03:00:00Z'),attempt(t+20,'claude-l',True)]
 write(h,turns,attempts);out=status(str(h));assert 'claude-r2d2/claude-fable-5-1 usage_limit:' in out;assert 'reset 2026-10-14 20:00:00 PDT' in out;assert 'claude-l: ok' in out
 attempts[-2]['classification']='credits'; attempts[-2]['reason']='Credit balance is too low'
 write(h,turns,attempts);assert 'claude-r2d2/claude-fable-5-1 usage_limit:' in status(str(h))
 print('PASS usage and credit limit diagnostics')
 t+=300;turns.append(dict(n=1713,at=t,duration_s=10,engine='claude-r2d2',model='claude-fable-5-1',events=[]));attempts.append(attempt(t+10,'claude-r2d2',True))
 write(h,turns,attempts);out=status(str(h));assert 'MISMATCH' not in out;assert 'off configured engine:' not in out;assert 'claude-r2d2: ok' in out
 turns[-1]['model']='another-model';write(h,turns,attempts);assert 'MISMATCH' in status(str(h));assert 'off configured engine: 1 turns' in status(str(h))
 print('PASS recovery and model mismatch')
 write(h,[],[]);out=status(str(h));assert 'last turn ran on unknown (unknown)' in out;assert 'last turn attempts: none recorded' in out;assert 'claude-r2d2: unknown' in out
 # Exact order, normalization, Unicode cap, multiple successes and majority/tie.
 t=start; turns=[dict(at=t+.75,duration_s=20,engine='codex',model='gpt-6-astra')]
 long='  '+('é'*90)+'\n trailing '
 attempts=[attempt(t+10,'claude-r2d2',classification='auth',reason=long),attempt(t+1,'claude-r2d2',classification='rate_limit',reason='slow'),attempt(t+2,'codex',True),attempt(t+20,'codex',True)]
 write(h,turns,attempts);out=status(str(h))
 assert line(out,'last turn attempts:')=='last turn attempts: claude-r2d2/claude-fable-5-1 auth: '+('é'*80)+'; claude-r2d2/claude-fable-5-1 rate_limit: slow; codex/gpt-6-astra ok; codex/gpt-6-astra ok'
 assert line(out,'off configured engine:').endswith('reason: auth')
 attempts.insert(2,attempt(t+3,'claude-r2d2',classification='rate_limit',reason='slow'))
 write(h,turns,attempts);assert line(status(str(h)),'off configured engine:').endswith('reason: rate_limit')
 # Missing model/runner is a boundary, not a skipped row; compaction is excluded.
 turns=[dict(at=t,duration_s=20,engine='codex',model='gpt-6-astra'),dict(at=t+30,duration_s=2),dict(at=t+40,duration_s=10,engine='codex',model='gpt-6-astra'),dict(at=t+60,duration_s=1,engine='claude-r2d2',kind='compaction')]
 write(h,turns,[]);out=status(str(h));assert 'off configured engine: 1 turns' in out;assert 'reason: unknown' in out;assert 'last turn attempts: none recorded' in out
 turns[-2].pop('model');write(h,turns,[]);out=status(str(h));assert 'last turn ran on codex (unknown)' in out;assert 'off configured engine:' not in out
 # Floored endpoints and next-start exclusive ownership.
 turns=[dict(at=t+.9,duration_s=30,engine='codex',model='gpt-6-astra'),dict(at=t+20.8,duration_s=10,engine='codex',model='gpt-6-astra')]
 attempts=[attempt(t+19,'claude-r2d2'),attempt(t+20,'claude-l'),attempt(t+30,'codex',True),attempt(t+31,'codex',True)]
 write(h,turns,attempts);out=status(str(h));assert 'claude-r2d2/' not in line(out,'last turn attempts:');assert line(out,'last turn attempts:').count('codex/gpt-6-astra ok')==1;assert 'claude-l/' in line(out,'last turn attempts:')
 # Persistent suffix never changes account/model comparison.
 turns=[dict(at=t,duration_s=10,engine='claude-r2d2',model='claude-fable-5-1')];write(h,turns,[])
 (h/'engine').write_text(json.dumps(dict(acc='claude-r2d2',model='claude-fable-5-1',mode='persistent')))
 out=status(str(h));assert line(out,'engine:')=='engine: configured claude-r2d2 (claude-fable-5-1) [persistent] | last turn ran on claude-r2d2 (claude-fable-5-1)'
 # Account failure streak after a success, latest classification and reset conversions.
 for reset,expected_reset in [('2026-01-15T03:00:00+02:00','2026-01-14 18:00:00 PDT'),('8pm','8pm PDT'),('8pm XYZ','8pm XYZ')]:
  attempts=[attempt(t,'claude-r2d2',True),attempt(t+1,'claude-r2d2',classification='auth',reason='auth'),attempt(t+2,'claude-l',True),attempt(t+3,'claude-r2d2',classification='usage_limit',reason='Usage limit reached; resets '+reset)]
  write(h,[],attempts);out=status(str(h));assert line(out,'accounts:')=='accounts: claude-r2d2: usage_limit since 2026-10-14 15:00:01 PDT, reset '+expected_reset+'; claude-l: ok; codex: unknown'
 print('PASS ordering, boundaries, reset formats, persistent mode, unknown evidence')
 # Malformed rows and partial final writes cannot break diagnostics.
 for name in ['turns','attempts']:
  (h/'logs'/f'{name}.jsonl').write_text('null\n[]\n{"at":"bad"}\n{"partial":')
 status(str(h))
 print('PASS missing and malformed evidence')
print('[b13.acceptance] PASS')
