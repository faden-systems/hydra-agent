#!/usr/bin/env bash
# b15 exit (loops/b15.md): ancestry, frozen files, fence, baseline tests byte-identical, seams, pytest, the frozen
# b12 and b14 acceptances (regression guards for founder rules 1-5 and the valve), the b15 acceptance, the notes gate.
set -euo pipefail
BASE="${BASE_REF:?set BASE_REF to the full SHA of the spec squash merge}"
SPEC="${SPEC_REF:?set SPEC_REF to the full SHA of the spec squash merge}"
fail() { echo "[b15] FAIL: $*"; exit 1; }
[[ "$BASE" =~ ^[0-9a-f]{40}$ && "$SPEC" =~ ^[0-9a-f]{40}$ ]] || fail 'BASE_REF and SPEC_REF must be full SHAs'
[[ "$BASE" == "$SPEC" ]] || fail 'BASE_REF must equal SPEC_REF (loops/b15.md Exit)'
git rev-parse --verify "$BASE" >/dev/null || fail "missing base $BASE"
git merge-base --is-ancestor "$BASE" HEAD || fail 'candidate does not descend from the spec merge'
git merge-base --is-ancestor 2922bc216fa5747b999e1fafa3107dc31a332639 "$BASE" || fail 'base predates the b14 build merge'
FROZEN="loops/b15.md loops/b15.exit.sh loops/b15.acceptance.py loops/b14.md loops/b14.exit.sh loops/b14.acceptance.py loops/b12.md loops/b12.exit.sh loops/b12.acceptance.py"
for f in $FROZEN; do git show "$SPEC:$f" | cmp -s - "$f" || fail "exit-owned file changed: $f"; done
fence() {
  local changed bad f
  changed=$( (git diff --no-renames --name-only "$BASE" HEAD; git diff --no-renames --name-only "$BASE"; git diff --no-renames --name-only; git diff --no-renames --cached --name-only; git ls-files --others --exclude-standard) | sort -u)
  bad=$(printf '%s\n' "$changed" | grep -Ev '^$|^manager/(supervisor\.py|README\.md)$|^tests/manager/test_b15_compaction\.py$|^docs/notes/b15\.md$' || true)
  test -z "$bad" || fail "outside fence: $bad"
  for f in $(git ls-tree -r --name-only "$BASE" tests/manager); do git show "$BASE:$f" | cmp -s - "$f" || fail "baseline test changed: $f"; done
}
fence
test -f docs/notes/b15.md || fail 'docs/notes/b15.md missing'
unset PYTHONOPTIMIZE PYTEST_ADDOPTS
PYTHON="${PYTHON:-python3}"
PYTHON=$("$PYTHON" -c 'import sys; sys.exit("optimized Python disables exit gates") if sys.flags.optimize else print(sys.executable)') || fail 'cannot resolve the Python interpreter'
"$PYTHON" -c 'import pytest, slack_bolt, slack_sdk' || fail 'install pytest, slack_bolt and slack_sdk in the selected Python environment'
"$PYTHON" - <<'PY' || fail 'required seams missing'
import ast,re
from pathlib import Path
src=Path('manager/supervisor.py').read_text()
tree=ast.parse(src)
consts={n.targets[0].id:n.value for n in tree.body if isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name)}
msg=ast.literal_eval(consts['COMPACTION_MESSAGE'])
assert '---HANDOFF---' in msg and 'compacted' in msg and 'MEMORY.md' in msg,'COMPACTION_MESSAGE must state the whole contract (requirement 2a)'
assert ast.literal_eval(consts['ENGINE_TIMEOUT_S'])==1800,'ENGINE_TIMEOUT_S changed'
assert ast.literal_eval(consts['FAULT_ESCALATE_COUNT'])==3 and ast.literal_eval(consts['FAULT_ESCALATE_S'])==1800,'the b14 valve constants changed'
for needle,why in (('"timeout"','the attempt classification literal (requirement 3)'),('"working"','the working literal'),('"silent"','the silent literal'),
                   ('"transcript"','the verification source literal (requirement 1)'),('shape_failures','the shape-failure streak (requirement 2b)'),
                   ('manager turn timeout: ','the working-timeout line wording'),('silent timeout','the silent fault wording'),
                   ('session start: ','the status session-start wording'),('last turn timeout: ','the status timeout wording'),
                   ('carried no valid five-line handoff; end it with','the handoff miss sentence'),('left MEMORY.md unchanged; write the durable facts','the MEMORY.md miss sentence')):
    assert needle in src,f'{why} is missing: {needle!r}'
readme=Path('manager/README.md').read_text()
for needle in ('session start:','last turn timeout:','manager turn timeout:','silent timeout','shape failure'):
    assert needle in readme,f'README lacks {needle!r}'
tests=Path('tests/manager/test_b15_compaction.py')
assert tests.is_file(),'tests/manager/test_b15_compaction.py missing (requirement 5)'
names=[n.name for n in ast.parse(tests.read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')]
assert len(names)>=12,f'requirement 5 expects the obligations covered; {len(names)} tests found'
for needle in ('verify','transcript','argument','pending','schedule','handoff','unchanged','backoff','forced','working','silent','status','oct18'):
    assert any(needle in n for n in names),f'no candidate test names {needle!r} (requirement 5)'
print('[b15] seams present')
PY
"$PYTHON" - <<'PY' || fail 'notes gate'
import re
from pathlib import Path
text=Path('docs/notes/b15.md').read_text()
for h in ('What changed','Evidence','Open questions'):
    assert '## '+h in text,f'docs/notes/b15.md lacks ## {h}'
    body=text.split('## '+h,1)[1].split('\n## ',1)[0].strip()
    assert len(body)>=4,f'section {h} has no body'
ev=text.split('## Evidence',1)[1].split('\n## ',1)[0]
assert re.search(r'\b\d+ passed\b',ev),'Evidence lacks a pytest count'
for line in ('[b12.acceptance] PASS','[b14.acceptance] PASS','[b15.acceptance] PASS'):
    assert line in ev,f'Evidence lacks {line}'
assert '[b15] PASS' in ev or 'exit: first run pending' in ev,'Evidence lacks the exit last line (or the first-run marker)'
for label,pat in (('Slack id',r'\b[CUW]0[A-Z0-9]{8,}\b'),('token',r'xox[a-z]-|ghp_[A-Za-z0-9]|sk-ant-|CLAUDE_CODE_OAUTH_TOKEN='),('tailnet address',r'\b100\.\d+\.\d+\.\d+\b'),('hostname',r'\b[a-z0-9-]+\.local\b')):
    assert not re.search(pat,text),f'notes contain a {label}'
print('[b15] notes ok')
PY
RESULT=$(mktemp); trap 'rm -f "$RESULT"' EXIT
"$PYTHON" -m pytest -q -p no:cacheprovider tests/manager | tee "$RESULT" || fail pytest
"$PYTHON" - "$RESULT" <<'PY' || fail 'notes count differs from the measured pytest result'
import re,sys
from pathlib import Path
counts=re.findall(r'(\d+) passed',Path(sys.argv[1]).read_text())
assert counts and re.search(r'\b'+counts[-1]+r' passed\b',Path('docs/notes/b15.md').read_text())
PY
"$PYTHON" loops/b12.acceptance.py || fail 'the frozen b12 acceptance no longer passes'
"$PYTHON" loops/b14.acceptance.py || fail 'the frozen b14 acceptance no longer passes'
"$PYTHON" loops/b15.acceptance.py || fail 'b15 acceptance'
fence
echo '[b15] PASS'
