#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:?set BASE_REF to the merged b9 spec commit SHA}"
fail() { echo "[b9] FAIL: $*"; exit 1; }
[[ "$BASE" =~ ^[0-9a-f]{40}$ ]] || fail 'BASE_REF must be a full immutable commit SHA'
git rev-parse --verify "$BASE" >/dev/null || fail "missing base $BASE"
git merge-base --is-ancestor "$BASE" HEAD || fail 'candidate does not descend from the spec base'
git merge-base --is-ancestor 579f444 "$BASE" || fail 'base predates merged b7'
FROZEN="loops/b9.md loops/b9.exit.sh loops/b9.acceptance.py loops/b9.observe.py loops/b7.acceptance.py"
fence() {
  local changed fixtures bad f
  # Round 4 B1: the changed-path set is the union of committed (BASE..HEAD), staged, unstaged and untracked paths;
  # recorded fixtures are rejected from that whole set BEFORE the general tests/manager allowance, so a fixture
  # change hidden in the commit or the index behind a restored working tree cannot pass.
  changed=$( (git diff --name-only "$BASE" HEAD; git diff --name-only "$BASE"; git diff --name-only; git diff --cached --name-only; git ls-files --others --exclude-standard) | sort -u )
  fixtures=$(printf '%s\n' "$changed" | grep -E '^tests/manager/fixtures/' || true)
  test -z "$fixtures" || fail "recorded fixtures changed (committed, staged, unstaged or untracked): $fixtures"
  bad=$(printf '%s\n' "$changed" | grep -Ev '^$|^manager/(bridge\.py|README\.md)$|^tests/manager/|^docs/notes/b9\.md$' || true)
  test -z "$bad" || fail "outside fence: $bad"
  for f in $FROZEN; do git show "$BASE:$f" | cmp -s - "$f" || fail "exit-owned file changed: $f"; done
}
fence
test -f docs/notes/b9.md || fail 'docs/notes/b9.md missing'
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import pytest, slack_bolt, slack_sdk' || fail 'install pytest, slack_bolt and slack_sdk in the selected Python environment'
# Baseline regression preservation: every baseline tests/manager statement stays structurally identical; no exemptions.
"$PYTHON" - "$BASE" <<'PY'
import ast,subprocess,sys
from pathlib import Path
base=sys.argv[1]
files=subprocess.check_output(['git','ls-tree','-r','--name-only',base,'tests/manager']).decode().splitlines()
for file in files:
    if not file.endswith('.py'):continue
    path=Path(file);assert path.is_file() and not path.is_symlink(),f'baseline test removed: {file}'
    before=ast.parse(subprocess.check_output(['git','show',f'{base}:{file}']).decode());after=ast.parse(path.read_text())
    names=[n.name for n in after.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
    assert len(names)==len(set(names)),f'duplicate definition overrides a regression: {file}'
    retained=[ast.dump(n,include_attributes=False) for n in after.body]
    for node in before.body:
        d=ast.dump(node,include_attributes=False)
        assert d in retained,f'baseline regression statement changed: {file}:{node.lineno}';retained.remove(d)
print('[b9] baseline test statements preserved')
PY
"$PYTHON" -m pytest -q tests/manager || fail pytest
# Round 1 2.6: the BASELINE suite (tests/manager exactly as at BASE_REF, conftest included) runs against the
# CANDIDATE manager code; every baseline node id must execute and pass, none skipped.
MIX=$(mktemp -d /tmp/b9-baseline-XXXXXX)
trap 'rm -rf "$MIX"' EXIT
tar --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -xf - -C "$MIX"
rm -rf "$MIX/tests/manager"
git archive "$BASE" tests/manager | tar -x -C "$MIX" || fail 'baseline tests not extracted'
(cd "$MIX" && "$PYTHON" -m pytest --rootdir=. -q --collect-only -p no:cacheprovider tests/manager) >"$MIX/nodes.txt" || fail 'baseline collection'
(cd "$MIX" && "$PYTHON" -m pytest --rootdir=. -q -rA -p no:cacheprovider tests/manager) >"$MIX/run.txt" 2>&1 || { tail -n 40 "$MIX/run.txt"; fail 'baseline suite fails against candidate code'; }
"$PYTHON" - "$MIX/nodes.txt" "$MIX/run.txt" <<'PY'
import sys
from pathlib import Path
nodes={l.strip() for l in Path(sys.argv[1]).read_text().splitlines() if l.startswith('tests/manager/') and '::' in l}
assert nodes,'empty baseline node set'
lines=Path(sys.argv[2]).read_text().splitlines()
passed={l.split(' ',1)[1].strip() for l in lines if l.startswith('PASSED ')}
for n in sorted(nodes):
    assert n in passed,f'baseline test did not pass unskipped against candidate code: {n}'
print(f'[b9] baseline suite: {len(nodes)} node ids executed and passed against candidate code')
PY
"$PYTHON" - "$PYTHON" <<'PY'
import subprocess,sys
for argv,seconds in ((['loops/b7.acceptance.py'],180),(['loops/b9.acceptance.py'],120),(['loops/b9.observe.py','--self-test'],60)):
    try: result=subprocess.run([sys.argv[1],*argv],timeout=seconds)
    except subprocess.TimeoutExpired: raise SystemExit(f'[b9] FAIL: {argv[0]} timeout')
    if result.returncode: raise SystemExit(f'[b9] FAIL: {argv[0]} exit {result.returncode}')
PY
fence
echo '[b9] PASS'
