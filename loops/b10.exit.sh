#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:?set BASE_REF to the full SHA of main at launch (contains the merged b10 spec)}"
SPEC="${SPEC_REF:-$BASE}"
fail() { echo "[b10] FAIL: $*"; exit 1; }
[[ "$BASE" =~ ^[0-9a-f]{40}$ ]] || fail 'BASE_REF must be a full immutable commit SHA'
[[ "$SPEC" =~ ^[0-9a-f]{40}$ ]] || fail 'SPEC_REF must be a full immutable commit SHA'
git rev-parse --verify "$BASE" >/dev/null || fail "missing base $BASE"
git rev-parse --verify "$SPEC" >/dev/null || fail "missing spec commit $SPEC"
git merge-base --is-ancestor "$BASE" HEAD || fail 'candidate does not descend from the launch base'
git merge-base --is-ancestor "$SPEC" "$BASE" || fail 'the merged spec is not an ancestor of the launch base'
git merge-base --is-ancestor 579f444 "$BASE" || fail 'base predates merged b7'
FROZEN="loops/b10.md loops/b10.exit.sh loops/b10.acceptance.py loops/b7.acceptance.py"
for f in $FROZEN; do git show "$SPEC:$f" | cmp -s - <(git show "$BASE:$f") || fail "exit-owned file differs between SPEC_REF and BASE_REF: $f"; done
EXEMPT="test_failure_then_recovery_sends_exactly_two_deduplicated_notices test_diverged_histories_reconcile_with_an_ordinary_merge"
fence() {
  local changed fixtures bad f
  changed=$( (git diff --no-renames --name-only "$BASE" HEAD; git diff --no-renames --name-only "$BASE"; git diff --no-renames --name-only; git diff --no-renames --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  fixtures=$(printf '%s\n' "$changed" | grep -E '^tests/manager/fixtures/' || true)
  test -z "$fixtures" || fail "recorded fixtures changed (committed, staged, unstaged or untracked): $fixtures"
  bad=$(printf '%s\n' "$changed" | grep -Ev '^$|^manager/(supervisor\.py|README\.md)$|^tests/manager/|^docs/notes/b10\.md$' || true)
  test -z "$bad" || fail "outside fence: $bad"
  for f in $FROZEN; do git show "$BASE:$f" | cmp -s - "$f" || fail "exit-owned file changed: $f"; done
}
fence
test -f docs/notes/b10.md || fail 'docs/notes/b10.md missing'
PYTHON="${PYTHON:-python3}"
PYTHON=$("$PYTHON" -c 'import sys; print(sys.executable)') || fail 'cannot resolve the Python interpreter'
[[ "$PYTHON" = /* ]] || fail "interpreter path is not absolute: $PYTHON"
# Notes gate (requirement 8): real headings with bodies, evidence content, no Slack ids / tokens / hostnames.
"$PYTHON" - <<'PY' || fail 'notes gate'
import re,sys
from pathlib import Path
text=Path('docs/notes/b10.md').read_text()
sections={}
current=None
for line in text.splitlines():
    if line.startswith('## '):
        current=line[3:].strip();sections.setdefault(current,[])
    elif current is not None:
        sections[current].append(line)
for name in ('What changed','Evidence','Open questions'):
    assert name in sections,f'docs/notes/b10.md lacks the heading "## {name}"'
    body=' '.join(l.strip() for l in sections[name] if l.strip() and not l.startswith('#'))
    assert len(body)>=4,f'section "{name}" has no body'
evidence=' '.join(sections['Evidence'])
assert re.search(r'\b\d+ passed\b',evidence),'Evidence lacks a pytest count'
assert '[b10.acceptance] PASS' in evidence,'Evidence lacks the acceptance PASS line'
for label,pat in (('Slack id',r'\b[CUW]0[A-Z0-9]{8,}\b'),('token',r'xox[a-z]-|ghp_[A-Za-z0-9]|sk-ant-|CLAUDE_CODE_OAUTH_TOKEN='),('tailnet address',r'\b100\.\d+\.\d+\.\d+\b'),('hostname',r'\b[a-z0-9-]+\.local\b|\bhydra-manager\b')):
    assert not re.search(pat,text),f'notes contain a {label}'
print('[b10] notes ok')
PY
"$PYTHON" -c 'import pytest, slack_bolt, slack_sdk' || fail 'install pytest, slack_bolt and slack_sdk in the selected Python environment'
grep -q 'PERSISTENCE_NOTICE_AFTER_S = 300' manager/supervisor.py || fail 'PERSISTENCE_NOTICE_AFTER_S = 300 missing (requirement 3)'
grep -Eq '^def notice_error_summary\(' manager/supervisor.py || fail 'module-level notice_error_summary missing (requirement 4)'
# Baseline regression preservation: every baseline tests/manager statement stays structurally identical, except the
# two named tests of requirement 7 that contradict this spec; those may be absent. No duplicate definitions.
"$PYTHON" - "$BASE" $EXEMPT <<'PY'
import ast,subprocess,sys
from pathlib import Path
base=sys.argv[1];exempt=set(sys.argv[2:])
files=subprocess.check_output(['git','ls-tree','-r','--name-only',base,'tests/manager']).decode().splitlines()
for file in files:
    if not file.endswith('.py'):continue
    path=Path(file);assert path.is_file() and not path.is_symlink(),f'baseline test removed: {file}'
    before=ast.parse(subprocess.check_output(['git','show',f'{base}:{file}']).decode());after=ast.parse(path.read_text())
    names=[n.name for n in after.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
    assert len(names)==len(set(names)),f'duplicate definition overrides a regression: {file}'
    retained=[ast.dump(n,include_attributes=False) for n in after.body]
    for node in before.body:
        if file=='tests/manager/test_persistence.py' and isinstance(node,ast.FunctionDef) and node.name in exempt:
            continue
        d=ast.dump(node,include_attributes=False)
        assert d in retained,f'baseline regression statement changed: {file}:{node.lineno}';retained.remove(d)
print('[b10] baseline test statements preserved (two named exemptions)')
PY
"$PYTHON" -m pytest -q tests/manager || fail pytest
# The BASELINE suite (tests/manager exactly as at BASE_REF, conftest included), minus the two exempted node ids,
# runs against the CANDIDATE manager code; every remaining baseline node id must execute and pass, none skipped.
MIX=$(mktemp -d /tmp/b10-baseline-XXXXXX)
trap 'rm -rf "$MIX"' EXIT
tar --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -xf - -C "$MIX"
rm -rf "$MIX/tests/manager"
git archive "$BASE" tests/manager | tar -x -C "$MIX" || fail 'baseline tests not extracted'
DESELECT=()
for t in $EXEMPT; do DESELECT+=(--deselect "tests/manager/test_persistence.py::$t"); done
(cd "$MIX" && "$PYTHON" -m pytest --rootdir=. -q --collect-only -p no:cacheprovider "${DESELECT[@]}" tests/manager) >"$MIX/nodes.txt" || fail 'baseline collection'
(cd "$MIX" && "$PYTHON" -m pytest --rootdir=. -q -rA -p no:cacheprovider "${DESELECT[@]}" tests/manager) >"$MIX/run.txt" 2>&1 || { tail -n 40 "$MIX/run.txt"; fail 'baseline suite fails against candidate code'; }
"$PYTHON" - "$MIX/nodes.txt" "$MIX/run.txt" <<'PY'
import sys
from pathlib import Path
nodes={l.strip() for l in Path(sys.argv[1]).read_text().splitlines() if l.startswith('tests/manager/') and '::' in l}
assert nodes,'empty baseline node set'
lines=Path(sys.argv[2]).read_text().splitlines()
passed={l.split(' ',1)[1].strip() for l in lines if l.startswith('PASSED ')}
for n in sorted(nodes):
    assert n in passed,f'baseline test did not pass unskipped against candidate code: {n}'
print(f'[b10] baseline suite: {len(nodes)} node ids executed and passed against candidate code')
PY
"$PYTHON" - "$PYTHON" <<'PY'
import subprocess,sys
for argv,seconds in ((['loops/b10.acceptance.py'],300),(['loops/b10.acceptance.py','--b7-subset'],240)):
    try: result=subprocess.run([sys.argv[1],*argv],timeout=seconds)
    except subprocess.TimeoutExpired: raise SystemExit(f'[b10] FAIL: {" ".join(argv)} timeout')
    if result.returncode: raise SystemExit(f'[b10] FAIL: {" ".join(argv)} exit {result.returncode}')
PY
fence
echo '[b10] PASS'
