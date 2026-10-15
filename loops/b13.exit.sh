#!/usr/bin/env bash
set -euo pipefail
BASE=${BASE_REF:?}; SPEC=${SPEC_REF:?}; PYTHON=${PYTHON:-python3}
[[ $BASE =~ ^[0-9a-f]{40}$ && $SPEC =~ ^[0-9a-f]{40}$ ]]
[[ "$BASE" == "$SPEC" ]]
git merge-base --is-ancestor "$SPEC" "$BASE"
git merge-base --is-ancestor "$BASE" HEAD
unset PYTHONOPTIMIZE PYTEST_ADDOPTS
"$PYTHON" - "$BASE" "$SPEC" <<'PY'
import ast,subprocess,sys
from pathlib import Path
assert not sys.flags.optimize
base,spec=sys.argv[1:]
def show(ref,p):return subprocess.check_output(['git','show',f'{ref}:{p}'])
for p in ['loops/b13.md','loops/b13.exit.sh','loops/b13.acceptance.py']:
 assert show(base,p)==show(spec,p)==Path(p).read_bytes(),f'frozen file changed: {p}'
changed=set().union(*(set(subprocess.check_output(cmd).decode().splitlines()) for cmd in [['git','diff','--name-only',base,'HEAD'],['git','diff','--cached','--name-only','HEAD'],['git','diff','--name-only']]))|set(subprocess.check_output(['git','ls-files','--others','--exclude-standard']).decode().splitlines())
assert changed <= {'manager/supervisor.py','manager/README.md','tests/manager/test_b13_status.py','docs/notes/b13.md','tests/manager/test_memory.py','tests/manager/test_bridge.py','tests/manager/test_locks.py','tests/manager/test_cli.py','tests/manager/test_b13_correction.py'},changed
old=ast.parse(show(base,'manager/supervisor.py'));new=ast.parse(Path('manager/supervisor.py').read_text())
names=[x.name for x in new.body if isinstance(x,(ast.FunctionDef,ast.ClassDef))]
assert len(names)==len(set(names)),'duplicate definition'
for node in new.body:
 if isinstance(node,ast.FunctionDef) and (node.name=='status_text' or node.name.startswith('_status_')):
  assert not node.decorator_list,'status decorators execute at import'
  for default in node.args.defaults+node.args.kw_defaults:
   if default is not None: ast.literal_eval(default)
assert ast.dump(next(n for n in old.body if isinstance(n,ast.FunctionDef) and n.name=='status_text').args)==ast.dump(next(n for n in new.body if isinstance(n,ast.FunctionDef) and n.name=='status_text').args),'status signature changed'
def filtered(tree):
 return [ast.dump(x) for x in tree.body if not (isinstance(x,ast.FunctionDef) and (x.name=='status_text' or x.name.startswith('_status_')))]
assert filtered(old)==filtered(new),'runtime AST changed or executable statement added'
for p in subprocess.check_output(['git','ls-tree','-r','--name-only',base,'tests/manager']).decode().splitlines():
 assert show(base,p)==Path(p).read_bytes(),f'baseline test changed: {p}'
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
