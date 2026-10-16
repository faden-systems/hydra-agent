#!/usr/bin/env bash
# b14 exit (loops/b14.md): ancestry, frozen triple, fence, baseline tests byte-identical, seams, pytest, the frozen
# b12 acceptance (regression guard for founder rules 1-5), the b14 acceptance, the notes gate.
set -euo pipefail
BASE="${BASE_REF:?set BASE_REF to the full SHA of the spec squash merge}"
SPEC="${SPEC_REF:?set SPEC_REF to the full SHA of the spec squash merge}"
fail() { echo "[b14] FAIL: $*"; exit 1; }
[[ "$BASE" =~ ^[0-9a-f]{40}$ && "$SPEC" =~ ^[0-9a-f]{40}$ ]] || fail 'BASE_REF and SPEC_REF must be full SHAs'
[[ "$BASE" == "$SPEC" ]] || fail 'BASE_REF must equal SPEC_REF (loops/b14.md Exit)'
git rev-parse --verify "$BASE" >/dev/null || fail "missing base $BASE"
git merge-base --is-ancestor "$BASE" HEAD || fail 'candidate does not descend from the spec merge'
git merge-base --is-ancestor 6a6206ec20500a6a9ad451cae11b75f706695873 "$BASE" || fail 'base predates the b12 build merge'
FROZEN="loops/b14.md loops/b14.exit.sh loops/b14.acceptance.py loops/b12.acceptance.py loops/b12.md loops/b12.exit.sh"
for f in $FROZEN; do git show "$SPEC:$f" | cmp -s - "$f" || fail "exit-owned file changed: $f"; done
fence() {
  local changed bad f
  changed=$( (git diff --no-renames --name-only "$BASE" HEAD; git diff --no-renames --name-only "$BASE"; git diff --no-renames --name-only; git diff --no-renames --cached --name-only; git ls-files --others --exclude-standard) | sort -u)
  bad=$(printf '%s\n' "$changed" | grep -Ev '^$|^manager/(supervisor\.py|README\.md)$|^tests/manager/test_b14_session\.py$|^docs/notes/b14\.md$' || true)
  test -z "$bad" || fail "outside fence: $bad"
  for f in $(git ls-tree -r --name-only "$BASE" tests/manager); do git show "$BASE:$f" | cmp -s - "$f" || fail "baseline test changed: $f"; done
}
fence
test -f docs/notes/b14.md || fail 'docs/notes/b14.md missing'
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
assert 'SESSION_IN_USE_RE' in consts,'module-level SESSION_IN_USE_RE missing (requirement 2)'
assert ast.literal_eval(consts['FAULT_ESCALATE_COUNT'])==3,'FAULT_ESCALATE_COUNT must be the literal 3 (requirement 3)'
assert ast.literal_eval(consts['FAULT_ESCALATE_S'])==1800,'FAULT_ESCALATE_S must be the literal 1800 (requirement 3)'
assert ast.literal_eval(consts['TIER_PROBE_S'])==3600,'TIER_PROBE_S changed'
assert re.search(r'engine fault escalated: ',src),'the escalated line wording is missing (requirement 3)'
assert re.search(r'fault episode: tier ',src),'the status line wording is missing (requirement 5)'
assert src.count('find_claude_transcript(')>=4,'the invocation paths do not consult the transcript (requirement 1)'
readme=Path('manager/README.md').read_text()
for needle in ('--resume','escalated','fault episode','digest'):
    assert needle in readme,f'README lacks {needle!r}'
print('[b14] seams present')
PY
"$PYTHON" - <<'PY' || fail 'notes gate'
import re
from pathlib import Path
text=Path('docs/notes/b14.md').read_text()
for h in ('What changed','Evidence','Open questions'):
    assert '## '+h in text,f'docs/notes/b14.md lacks ## {h}'
    body=text.split('## '+h,1)[1].split('\n## ',1)[0].strip()
    assert len(body)>=4,f'section {h} has no body'
ev=text.split('## Evidence',1)[1].split('\n## ',1)[0]
assert re.search(r'\b\d+ passed\b',ev),'Evidence lacks a pytest count'
assert '[b12.acceptance] PASS' in ev and '[b14.acceptance] PASS' in ev,'Evidence lacks the acceptance PASS lines'
for label,pat in (('Slack id',r'\b[CUW]0[A-Z0-9]{8,}\b'),('token',r'xox[a-z]-|ghp_[A-Za-z0-9]|sk-ant-|CLAUDE_CODE_OAUTH_TOKEN='),('tailnet address',r'\b100\.\d+\.\d+\.\d+\b'),('hostname',r'\b[a-z0-9-]+\.local\b')):
    assert not re.search(pat,text),f'notes contain a {label}'
print('[b14] notes ok')
PY
RESULT=$(mktemp); trap 'rm -f "$RESULT"' EXIT
"$PYTHON" -m pytest -q -p no:cacheprovider tests/manager | tee "$RESULT" || fail pytest
"$PYTHON" - "$RESULT" <<'PY' || fail 'notes count differs from the measured pytest result'
import re,sys
from pathlib import Path
counts=re.findall(r'(\d+) passed',Path(sys.argv[1]).read_text())
assert counts and re.search(r'\b'+counts[-1]+r' passed\b',Path('docs/notes/b14.md').read_text())
PY
"$PYTHON" loops/b12.acceptance.py || fail 'the frozen b12 acceptance no longer passes'
"$PYTHON" loops/b14.acceptance.py || fail 'b14 acceptance'
fence
echo '[b14] PASS'
