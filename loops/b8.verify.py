#!/usr/bin/env python3
"""Exit-owned acceptance bounds and immutable build fence."""
import os,re,signal,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)
BASE=sys.argv[1]
FROZEN=('b8.md','b8.exit.sh','b8.verify.py','b8.acceptance.py','b8.fake.py','b8.identity.py','b8.mode.py','b8.live.py','b8.rollover.py','b8.capture-wire.py','b8.capture.py')
def git(*args):return subprocess.check_output(['git',*args])
def fence():
    changes=set()
    for args in [('diff','--name-only',BASE+'...HEAD'),('diff','--name-only'),('diff','--cached','--name-only'),('ls-files','--others','--exclude-standard')]:
        changes.update(git(*args).decode().splitlines())
    allowed=re.compile(r'^(manager/(supervisor\.py|bridge\.py|hydra|models\.json|persistent\.py|README\.md|CLAUDE\.md)|tests/manager/(?!fixtures/).+|docs/notes/b8\.md)$')
    assert all(allowed.fullmatch(p) for p in changes),f'outside build fence: {sorted(p for p in changes if not allowed.fullmatch(p))}'
    for name in FROZEN:
        file='loops/'+name
        assert git('show',BASE+':'+file)==Path(file).read_bytes(),f'frozen source changed: {file}'
def run(argv,seconds):
    proc=subprocess.Popen(argv,start_new_session=True)
    try:
        rc=proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid,signal.SIGKILL);proc.wait()
        raise SystemExit('acceptance watchdog expired: '+str(argv))
    finally:
        # Group is dedicated to this check; remove lingering fixture descendants.
        try:os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    assert rc==0, f'acceptance failed {argv}: {rc}'
fence()
run([sys.executable,'-m','pytest','-q','tests/manager'],300)
for file in ('b8.identity.py','b8.mode.py','b8.acceptance.py','b8.rollover.py'):
    run([sys.executable,'loops/'+file],120)
# The real gate runs only with explicit scratch evidence input, never production defaults.
run([sys.executable,'loops/b8.live.py'],30)
fence()
print('[b8] PASS')
