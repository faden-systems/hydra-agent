#!/usr/bin/env bash
set -euo pipefail
BASE=${BASE_REF:?}; SPEC=${SPEC_REF:?}; PYTHON=${PYTHON:-python3}
[[ $BASE =~ ^[0-9a-f]{40}$ && $SPEC =~ ^[0-9a-f]{40}$ ]]
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
assert changed <= {'manager/supervisor.py','manager/README.md','tests/manager/test_b13_status.py','docs/notes/b13.md'},changed
old=ast.parse(show(base,'manager/supervisor.py'));new=ast.parse(Path('manager/supervisor.py').read_text())
after=[ast.dump(x) for x in new.body]
for x in old.body:
 if isinstance(x,ast.FunctionDef) and x.name=='status_text':continue
 assert ast.dump(x) in after,'runtime code modified'
for p in subprocess.check_output(['git','ls-tree','-r','--name-only',base,'tests/manager']).decode().splitlines():
 assert show(base,p)==Path(p).read_bytes(),f'baseline test changed: {p}'
notes=Path('docs/notes/b13.md').read_text()
for heading in ['What changed','Evidence','Open questions']:assert '## '+heading in notes
assert '[b13.acceptance] PASS' in notes
PY
"$PYTHON" -m pytest -q tests/manager
BASE_REF="$BASE" "$PYTHON" loops/b13.acceptance.py
echo '[b13] PASS'
