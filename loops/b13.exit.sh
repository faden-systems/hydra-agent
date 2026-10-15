#!/usr/bin/env bash
set -euo pipefail
BASE=${BASE_REF:?}; SPEC=${SPEC_REF:?}; PYTHON=${PYTHON:-python3}
[[ $BASE =~ ^[0-9a-f]{40}$ && $SPEC =~ ^[0-9a-f]{40}$ ]]
[[ "$BASE" == "$SPEC" ]]
git merge-base --is-ancestor "$SPEC" "$BASE"
git merge-base --is-ancestor "$BASE" HEAD
unset PYTHONOPTIMIZE
"$PYTHON" - "$BASE" "$SPEC" <<'PY'
import ast,subprocess,sys
from pathlib import Path
assert not sys.flags.optimize
base,spec=sys.argv[1:]
def show(ref,p):return subprocess.check_output(['git','show',f'{ref}:{p}'])
for p in ['loops/b13.md','loops/b13.exit.sh','loops/b13.acceptance.py']:
 assert show(base,p)==show(spec,p)==Path(p).read_bytes(),f'frozen file changed: {p}'
changed=set(subprocess.check_output(['git','diff','--name-only',base]).decode().splitlines())|set(subprocess.check_output(['git','ls-files','--others','--exclude-standard']).decode().splitlines())
assert changed <= {'manager/supervisor.py','manager/README.md','tests/manager/test_b13_status.py','docs/notes/b13.md','tests/manager/test_memory.py','tests/manager/test_bridge.py','tests/manager/test_locks.py','tests/manager/test_cli.py'},changed
old=ast.parse(show(base,'manager/supervisor.py'));new=ast.parse(Path('manager/supervisor.py').read_text())
names=[x.name for x in new.body if isinstance(x,(ast.FunctionDef,ast.ClassDef))]
assert len(names)==len(set(names)),'duplicate definition'
def filtered(tree):
 return [ast.dump(x) for x in tree.body if not (isinstance(x,ast.FunctionDef) and (x.name=='status_text' or x.name.startswith('_status_')))]
assert filtered(old)==filtered(new),'runtime AST changed or executable statement added'
replacements={
 'tests/manager/test_memory.py':('assert "engine: claude-l (claude-sonnet-5)" in S.status_text(home)','assert "engine: configured claude-l (claude-sonnet-5)" in S.status_text(home)'),
 'tests/manager/test_bridge.py':('assert "engine: claude-r2d2" in poster.posted[-1][2] and "t1: building"','assert "engine: configured claude-r2d2" in poster.posted[-1][2] and "t1: building"'),
 'tests/manager/test_locks.py':('assert "engine: claude-r2d2" in text and "last turn:','assert "engine: configured claude-r2d2" in text and "last turn:')}
for p in subprocess.check_output(['git','ls-tree','-r','--name-only',base,'tests/manager']).decode().splitlines():
 expected=show(base,p)
 edits=[replacements[p]] if p in replacements else []
 if p=='tests/manager/test_bridge.py':edits.append(('assert "engine: codex (gpt-5.6-sol)" in poster.posted[-1][2]','assert "engine: configured codex (gpt-5.6-sol)" in poster.posted[-1][2]'))
 if p=='tests/manager/test_cli.py':edits.extend([('assert "engine: claude-r2d2" in r.stdout and "on codex','assert "engine: configured claude-r2d2" in r.stdout and "on codex'),('assert "engine: codex (gpt-5.6-sol)" in run(home, "status").stdout','assert "engine: configured codex (gpt-5.6-sol)" in run(home, "status").stdout')])
 for a,b in edits:
  assert expected.count(a.encode())==1;expected=expected.replace(a.encode(),b.encode())
 assert expected==Path(p).read_bytes(),f'baseline test changed beyond authorized assertion: {p}'
notes=Path('docs/notes/b13.md').read_text()
for heading in ['What changed','Evidence','Open questions']:assert '## '+heading in notes
assert '[b13.acceptance] PASS' in notes
import re
assert re.search(r'\b\d+ passed\b',notes),'notes need measured pytest count'
PY
RESULT=$(mktemp)
trap 'rm -f "$RESULT"' EXIT
"$PYTHON" -m pytest -q tests/manager | tee "$RESULT"
"$PYTHON" - "$RESULT" <<'PYCOUNT'
import re,sys
from pathlib import Path
counts=re.findall(r'(\d+) passed',Path(sys.argv[1]).read_text())
assert counts and re.search(r'\b'+counts[-1]+r' passed\b',Path('docs/notes/b13.md').read_text()),'notes count differs from measured pytest result'
PYCOUNT
BASE_REF="$BASE" "$PYTHON" loops/b13.acceptance.py
echo '[b13] PASS'
