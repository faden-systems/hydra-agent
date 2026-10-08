#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:?set BASE_REF to the full SHA of main at launch (contains the merged b11 spec)}"
SPEC="${SPEC_REF:-$BASE}"
fail() { echo "[b11] FAIL: $*"; exit 1; }
[[ "$BASE" =~ ^[0-9a-f]{40}$ ]] || fail 'BASE_REF must be a full immutable commit SHA'
[[ "$SPEC" =~ ^[0-9a-f]{40}$ ]] || fail 'SPEC_REF must be a full immutable commit SHA'
git rev-parse --verify "$BASE" >/dev/null || fail "missing base $BASE"
git rev-parse --verify "$SPEC" >/dev/null || fail "missing spec commit $SPEC"
git merge-base --is-ancestor "$BASE" HEAD || fail 'candidate does not descend from the launch base'
git merge-base --is-ancestor "$SPEC" "$BASE" || fail 'the merged spec is not an ancestor of the launch base'
git merge-base --is-ancestor aad5f1e7f0f8de81f339bdaf24e311e92267b4c1 "$BASE" || fail 'base predates merged b10'
FROZEN="loops/b11.md loops/b11.exit.sh loops/b11.acceptance.py"
for f in $FROZEN; do git show "$SPEC:$f" | cmp -s - <(git show "$BASE:$f") || fail "exit-owned file differs between SPEC_REF and BASE_REF: $f"; done
# Requirement 7: the two baseline tests that contradict this spec (file::name), replaced by the candidate's own.
EXEMPT="tests/manager/test_rollover.py::test_held_by_default_records_diagnostic_without_engine_call tests/manager/test_compaction.py::test_hydra_compact_cli_schedules"
fence() {
  local changed fixtures bad f
  changed=$( (git diff --no-renames --name-only "$BASE" HEAD; git diff --no-renames --name-only "$BASE"; git diff --no-renames --name-only; git diff --no-renames --cached --name-only; git ls-files --others --exclude-standard) | sort -u)
  fixtures=$(printf '%s\n' "$changed" | grep -E '^tests/manager/fixtures/' || true)
  test -z "$fixtures" || fail "recorded fixtures changed (committed, staged, unstaged or untracked): $fixtures"
  bad=$(printf '%s\n' "$changed" | grep -Ev '^$|^manager/(supervisor\.py|hydra|README\.md)$|^tests/manager/|^docs/notes/b11\.md$' || true)
  test -z "$bad" || fail "outside fence: $bad"
  for f in $FROZEN; do git show "$BASE:$f" | cmp -s - "$f" || fail "exit-owned file changed: $f"; done
}
fence
test -f docs/notes/b11.md || fail 'docs/notes/b11.md missing'
PYTHON="${PYTHON:-python3}"
PYTHON=$("$PYTHON" -c 'import sys; print(sys.executable)') || fail 'cannot resolve the Python interpreter'
[[ "$PYTHON" = /* ]] || fail "interpreter path is not absolute: $PYTHON"
# Notes gate (requirement 8): real headings with bodies, evidence content, no Slack ids / tokens / hostnames.
"$PYTHON" - <<'PY' || fail 'notes gate'
import re,sys
from pathlib import Path
text=Path('docs/notes/b11.md').read_text()
sections={}
current=None
for line in text.splitlines():
    if line.startswith('## '):
        current=line[3:].strip();sections.setdefault(current,[])
    elif current is not None:
        sections[current].append(line)
for name in ('What changed','Evidence','Open questions'):
    assert name in sections,f'docs/notes/b11.md lacks the heading "## {name}"'
    body=' '.join(l.strip() for l in sections[name] if l.strip() and not l.startswith('#'))
    assert len(body)>=4,f'section "{name}" has no body'
evidence=' '.join(sections['Evidence'])
assert re.search(r'\b\d+ passed\b',evidence),'Evidence lacks a pytest count'
assert '[b11.acceptance] PASS' in evidence,'Evidence lacks the acceptance PASS line'
m=re.search(r'message bytes before:\s*(\d+)',evidence);n=re.search(r'message bytes after:\s*(\d+)',evidence)
assert m and n,'Evidence lacks the labelled message bytes before/after'
assert int(n.group(1))<int(m.group(1)),'message bytes after must be below before'
for label,pat in (('Slack id',r'\b[CUW]0[A-Z0-9]{8,}\b'),('token',r'xox[a-z]-|ghp_[A-Za-z0-9]|sk-ant-|CLAUDE_CODE_OAUTH_TOKEN='),('tailnet address',r'\b100\.\d+\.\d+\.\d+\b'),('hostname',r'\b[a-z0-9-]+\.local\b')):
    assert not re.search(pat,text),f'notes contain a {label}'
print('[b11] notes ok')
PY
"$PYTHON" -c 'import pytest, slack_bolt, slack_sdk' || fail 'install pytest, slack_bolt and slack_sdk in the selected Python environment'
# Seams of requirements 2, 3 and 5 (the acceptance exercises their behaviour; these are the names it relies on).
"$PYTHON" - <<'PY' || fail 'required seams missing'
import ast,re
from pathlib import Path
src=Path('manager/supervisor.py').read_text()
tree=ast.parse(src)
names={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
assert 'cli_compactions_since' in names,'module-level cli_compactions_since missing (requirement 3)'
sup=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Supervisor')
methods={n.name for n in sup.body if isinstance(n,ast.FunctionDef)}
assert 'state_digest' in methods,'Supervisor.state_digest missing (requirement 5)'
assert 'state_block' not in methods,'Supervisor.state_block must be replaced by state_digest (requirement 5)'
defaults=re.search(r'COMPACTION_DEFAULTS\s*=\s*\{(.*?)\}',src,re.S)
assert defaults and re.search(r'"rollover_enabled":\s*True',defaults.group(1)),'COMPACTION_DEFAULTS lacks "rollover_enabled": True (requirement 2)'
assert 'compact_boundary' in src,'compact_boundary detection missing (requirement 3)'
assert 'message_bytes' in src,'turn records must carry message_bytes (requirement 5)'
assert 'compaction: pending (' not in src,'the old status wording must go (requirement 4)'
cli=Path('manager/hydra').read_text()
assert 'compaction held' in cli,'hydra compact must report a held outcome (requirement 2)'
print('[b11] seams present')
PY
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
        if isinstance(node,ast.FunctionDef) and f'{file}::{node.name}' in exempt:
            continue
        d=ast.dump(node,include_attributes=False)
        assert d in retained,f'baseline regression statement changed: {file}:{node.lineno}';retained.remove(d)
print('[b11] baseline test statements preserved (two named exemptions)')
PY
"$PYTHON" -m pytest -q tests/manager || fail pytest
# The BASELINE suite (tests/manager exactly as at BASE_REF, conftest included), minus the two exempted node ids,
# runs against the CANDIDATE manager code; every remaining baseline node id must execute and pass, none skipped.
MIX=$(mktemp -d /tmp/b11-baseline-XXXXXX)
trap 'rm -rf "$MIX"' EXIT
tar --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -xf - -C "$MIX"
rm -rf "$MIX/tests/manager"
git archive "$BASE" tests/manager | tar -x -C "$MIX" || fail 'baseline tests not extracted'
DESELECT=()
for t in $EXEMPT; do DESELECT+=(--deselect "$t"); done
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
print(f'[b11] baseline suite: {len(nodes)} node ids executed and passed against candidate code')
PY
"$PYTHON" - "$PYTHON" <<'PY'
import subprocess,sys
try: result=subprocess.run([sys.argv[1],'loops/b11.acceptance.py'],timeout=600)
except subprocess.TimeoutExpired: raise SystemExit('[b11] FAIL: loops/b11.acceptance.py timeout')
if result.returncode: raise SystemExit(f'[b11] FAIL: loops/b11.acceptance.py exit {result.returncode}')
PY
fence
echo '[b11] PASS'
