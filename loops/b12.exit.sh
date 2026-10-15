#!/usr/bin/env bash
set -euo pipefail
BASE="${BASE_REF:?set BASE_REF to the full SHA of the acceptance base (launch main or recorded rebase main tip)}"
SPEC="${SPEC_REF:?set SPEC_REF to the recorded full spec-merge SHA}"
fail() { echo "[b12] FAIL: $*"; exit 1; }
[[ "$BASE" =~ ^[0-9a-f]{40}$ ]] || fail 'BASE_REF must be a full immutable commit SHA'
[[ "$SPEC" =~ ^[0-9a-f]{40}$ ]] || fail 'SPEC_REF must be a full immutable commit SHA'
git rev-parse --verify "$BASE" >/dev/null || fail "missing base $BASE"
git rev-parse --verify "$SPEC" >/dev/null || fail "missing spec commit $SPEC"
git merge-base --is-ancestor "$BASE" HEAD || fail 'candidate does not descend from the acceptance base'
git merge-base --is-ancestor "$SPEC" "$BASE" || fail 'the merged spec is not an ancestor of the acceptance base'
git merge-base --is-ancestor 421b7bd9c2f4387bf306b1e573e22087b1f69f44 "$BASE" || fail 'base predates the merged b11 spec'
FROZEN="loops/b12.md loops/b12.exit.sh loops/b12.acceptance.py"
for f in $FROZEN; do git show "$SPEC:$f" | cmp -s - <(git show "$BASE:$f") || fail "exit-owned file differs between SPEC_REF and BASE_REF: $f"; done
# Requirement 7: the eleven baseline tests that contradict this spec (file::name), replaced by the candidate's own.
EXEMPT="tests/manager/test_cli.py::test_engine_acc_model_round_trip_and_rejection tests/manager/test_cli.py::test_attach_uses_the_configured_model tests/manager/test_bridge.py::test_engine_command_acc_and_model tests/manager/test_memory.py::test_parse_engine_command_aliases_legacy_and_full_ids tests/manager/test_memory.py::test_parse_engine_command_rejects_with_a_reason tests/manager/test_memory.py::test_models_json_is_extendable_from_home tests/manager/test_memory.py::test_turn_exports_memory_dir_stamps_header_snapshots_and_ledgers tests/manager/test_memory.py::test_auto_switch_carries_the_model_alias_or_uses_the_family_default tests/manager/test_engine_fallback.py::test_same_account_non_fable_retry_before_next_account tests/manager/test_engine_fallback.py::test_fallback_episode_closes_when_mode_was_ever_explicitly_set tests/manager/test_engines.py::test_all_engines_fail_keeps_events_and_notes_once tests/manager/test_engines.py::test_non_quota_failure_tries_next_without_persisting tests/manager/test_b13_status.py::test_preserve_populated_runtime"
fence() {
  local changed fixtures bad f
  changed=$( (git diff --no-renames --name-only "$BASE" HEAD; git diff --no-renames --name-only "$BASE"; git diff --no-renames --name-only; git diff --no-renames --cached --name-only; git ls-files --others --exclude-standard) | sort -u)
  fixtures=$(printf '%s\n' "$changed" | grep -E '^tests/manager/fixtures/' || true)
  test -z "$fixtures" || fail "recorded fixtures changed (committed, staged, unstaged or untracked): $fixtures"
  bad=$(printf '%s\n' "$changed" | grep -Ev '^$|^manager/(supervisor\.py|bridge\.py|hydra|models\.json|README\.md)$|^tests/manager/|^docs/notes/b12\.md$' || true)
  test -z "$bad" || fail "outside fence: $bad"
  for f in $FROZEN; do git show "$BASE:$f" | cmp -s - "$f" || fail "exit-owned file changed: $f"; done
}
fence
test -f docs/notes/b12.md || fail 'docs/notes/b12.md missing'
unset PYTHONOPTIMIZE
PYTHON="${PYTHON:-python3}"
PYTHON=$("$PYTHON" -c 'import sys; sys.exit("optimized Python disables exit gates") if sys.flags.optimize else print(sys.executable)') || fail 'cannot resolve the Python interpreter'
[[ "$PYTHON" = /* ]] || fail "interpreter path is not absolute: $PYTHON"
# Notes gate (requirement 8): real headings with bodies, evidence content, no Slack ids / tokens / hostnames.
"$PYTHON" - <<'PY' || fail 'notes gate'
import re,sys
from pathlib import Path
text=Path('docs/notes/b12.md').read_text()
sections={}
current=None
for line in text.splitlines():
    if line.startswith('## '):
        current=line[3:].strip();sections.setdefault(current,[])
    elif current is not None:
        sections[current].append(line)
for name in ('What changed','Evidence','Open questions'):
    assert name in sections,f'docs/notes/b12.md lacks the heading "## {name}"'
    body=' '.join(l.strip() for l in sections[name] if l.strip() and not l.startswith('#'))
    assert len(body)>=4,f'section "{name}" has no body'
evidence=' '.join(sections['Evidence'])
assert re.search(r'\b\d+ passed\b',evidence),'Evidence lacks a pytest count'
assert '[b12.acceptance] PASS' in evidence,'Evidence lacks the acceptance PASS line'
for label,pat in (('Slack id',r'\b[CUW]0[A-Z0-9]{8,}\b'),('token',r'xox[a-z]-|ghp_[A-Za-z0-9]|sk-ant-|CLAUDE_CODE_OAUTH_TOKEN='),('tailnet address',r'\b100\.\d+\.\d+\.\d+\b'),('hostname',r'\b[a-z0-9-]+\.local\b')):
    assert not re.search(pat,text),f'notes contain a {label}'
print('[b12] notes ok')
PY
"$PYTHON" -c 'import pytest, slack_bolt, slack_sdk' || fail 'install pytest, slack_bolt and slack_sdk in the selected Python environment'
# Seams of requirements 1 to 5 (the acceptance exercises their behaviour; these are the names it relies on).
"$PYTHON" - <<'PY' || fail 'required seams missing'
import ast,json,re
from pathlib import Path
src=Path('manager/supervisor.py').read_text()
tree=ast.parse(src)
names={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
consts={t.id for n in tree.body if isinstance(n,ast.Assign) for t in n.targets if isinstance(t,ast.Name)}
assert 'MANAGER_LADDER' in consts,'module-level MANAGER_LADDER missing (requirement 1)'
for fn in ('manager_ladder','ladder_steps','floor_model','ladder_position','enforce_engine_policy'):
    assert fn in names,f'module-level {fn} missing (requirements 1-2)'
sup=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Supervisor')
methods={n.name for n in sup.body if isinstance(n,ast.FunctionDef)}
assert 'engine_ladder' in methods,'Supervisor.engine_ladder missing (requirement 1)'
assert '["claude-sonnet-5"]' not in src,'the old default same-account retry list must go (requirement 1)'
assert 'all allowed engines failed' in src,'the unavailable line wording is missing (requirement 3)'
assert 'engine policy:' in src,'the status line is missing (requirement 4)'
assert "not on the manager's allowlist" in src,'the refusal wording is missing (requirement 2)'
assert 'EngineFault' in names and 'TIER_PROBE_S' in consts,'EngineFault / TIER_PROBE_S missing (requirements 11-12)'
assert 'engine fault: ' in src and 'fallback moved (episode ' in src,'fault / moved notice wording missing (requirements 12-13)'
assert '| effective ' in src,'the configured/effective status line is missing (requirement 14)'
for m in ('_tier_probe_due','_next_tier_probe','_note_fault','_close_fault_episode','_reconcile_fault_notice'):
    assert m in methods,f'Supervisor.{m} missing (requirements 11-12)'
assert 'manager unavailable (every engine failed)' not in src,'the old per-thread unavailable note must go (requirement 3)'
for f in ('manager/hydra','manager/bridge.py'):
    assert 'engine_policy' in Path(f).read_text(),f'{f} does not enforce the engine policy (requirement 2)'
models=json.loads(Path('manager/models.json').read_text())
cl=models['claude']['aliases'];cx=models['codex']['aliases']
assert 'sonnet5' not in cl and 'haiku4.5' not in cl,'sonnet5/haiku4.5 must be removed from models.json (requirement 5)'
assert cx.get('gpt6.1')=='gpt-6.1-sol' and cx.get('sol6.1')=='gpt-6.1-sol','gpt6.1/sol6.1 -> gpt-6.1-sol missing (requirement 5)'
assert cl.get('fable5.1')=='claude-fable-5-1' and cl.get('opus5.5')=='claude-opus-5-5' and cx.get('gpt6')=='gpt-6-astra','ladder aliases must stay'
readme=Path('manager/README.md').read_text()
assert 'floor' in readme and 'gpt-6.1-sol' in readme,'README must describe the floor and the coder alias (requirement 5)'
assert 'sonnet5' not in Path('manager/hydra').read_text().split('import')[0],'hydra usage text still lists sonnet5 (requirement 5)'
print('[b12] seams present')
PY
# Baseline regression preservation: every baseline tests/manager statement stays structurally identical, except the
# eleven named tests of requirement 7 that contradict this spec; those may be absent. No duplicate definitions.
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
        if file == 'tests/manager/test_memory.py' and isinstance(node,ast.FunctionDef) and node.name == 'test_switch_to_codex_and_back_lists_the_other_familys_files':
            literals=[n for n in ast.walk(node) if isinstance(n,ast.Constant) and n.value == 'gpt-5.6-sol']
            assert len(literals)==3,'baseline memory migration no longer matches exactly three literals'
            for literal in literals: literal.value='gpt-6-astra'
        d=ast.dump(node,include_attributes=False)
        assert d in retained,f'baseline regression statement changed: {file}:{node.lineno}';retained.remove(d)
print('[b12] baseline test statements preserved (eleven named exemptions)')
PY
"$PYTHON" -m pytest -q tests/manager || fail pytest
# The BASELINE suite (tests/manager exactly as at BASE_REF, conftest included), minus the exempted node ids, runs
# against the CANDIDATE manager code; every remaining baseline node id must execute and pass, none skipped.
MIX=$(mktemp -d /tmp/b12-baseline-XXXXXX)
trap 'rm -rf "$MIX"' EXIT
tar --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache -cf - . | tar -xf - -C "$MIX"
rm -rf "$MIX/tests/manager"
git archive "$BASE" tests/manager | tar -x -C "$MIX" || fail 'baseline tests not extracted'
# Literal-only model migration; all memory assertions remain in the baseline run.
"$PYTHON" - "$MIX/tests/manager/test_memory.py" <<'PYMIGRATE'
import ast,sys
from pathlib import Path
p=Path(sys.argv[1]);source=p.read_text();tree=ast.parse(source)
fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='test_switch_to_codex_and_back_lists_the_other_familys_files')
literals=[n for n in ast.walk(fn) if isinstance(n,ast.Constant) and n.value=='gpt-5.6-sol']
assert len(literals)==3,'baseline memory migration no longer matches exactly three literals'
# AST columns are UTF-8 byte offsets; preserve the remainder of the source verbatim.
lines=source.encode().splitlines(keepends=True)
for n in sorted(literals,key=lambda n:(n.lineno,n.col_offset),reverse=True):
    assert n.lineno==n.end_lineno,'unexpected multiline model literal'
    line=lines[n.lineno-1]
    lines[n.lineno-1]=line[:n.col_offset]+b'"gpt-6-astra"'+line[n.end_col_offset:]
p.write_bytes(b''.join(lines))
PYMIGRATE
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
print(f'[b12] baseline suite: {len(nodes)} node ids executed and passed against candidate code')
import re
notes=Path('docs/notes/b12.md').read_text()
m=re.search(r'(\d+) passed',notes)
assert m and int(m.group(1))>=len(nodes),f'notes pytest count {m.group(1) if m else None} is below the {len(nodes)} baseline node ids the exit executed'
PY
"$PYTHON" - "$PYTHON" <<'PY'
import subprocess,sys
try: result=subprocess.run([sys.argv[1],'loops/b12.acceptance.py'],timeout=900,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
except subprocess.TimeoutExpired: raise SystemExit('[b12] FAIL: loops/b12.acceptance.py timeout')
sys.stdout.write(result.stdout)
if result.returncode: raise SystemExit(f'[b12] FAIL: loops/b12.acceptance.py exit {result.returncode}')
if '[b12.acceptance] PASS' not in result.stdout: raise SystemExit('[b12] FAIL: the acceptance did not print its PASS line')
PY
fence
echo '[b12] PASS'
