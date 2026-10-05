#!/usr/bin/env python3
"""Exit-owned acceptance bounds and immutable build fence."""
import ast,os,re,signal,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)
BASE=sys.argv[1]
FROZEN=('b8.md','b8.exit.sh','b8.verify.py','b8.acceptance.py','b8.fake.py','b8.identity.py','b8.mode.py','b8.live.py','b8.rollover.py','b8.capture-wire.py','b8.capture.py','b8.regressions.py')
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
# B3 exact replacement mapping (loops/b8.md, "Baseline regression preservation"). A baseline test
# listed here may be changed or removed by the candidate ONLY because the frozen file named beside
# it re-runs the same obligations with a trigger-only replacement (requirement 6: no automatic
# pressure rollover; identity contract: fallback fixtures need verified dummy sidecars). The
# frozen runners execute the ORIGINAL BASE_REF functions, so deleting a listed test loses nothing.
# Every other baseline statement (tests, fixtures, helpers, decorators) must stay identical.
SUPERSEDED={
    'tests/manager/test_compaction.py':{
        # automatic pressure-triggered rollover: superseded by lifecycle pressure non-trigger checks
        # and the real-error emergency matrix
        'test_over_threshold_schedules_defers_then_runs_in_quiet_hours':'b8.rollover.py',
        'test_far_over_threshold_runs_immediately':'b8.rollover.py',
        'test_exactly_25_percent_is_immediate':'b8.rollover.py',
        'test_session_file_over_max_bytes_schedules':'b8.rollover.py',
        # failure/verification obligations reached through a pressure trigger: re-run with an
        # explicit manual request (regressions) and a qualified emergency backoff (rollover)
        'test_failed_compact_posts_once_keeps_the_session_and_backs_off':'b8.regressions.py',
        'test_no_reduction_after_compact_is_a_failure':'b8.regressions.py',
        'test_failed_compaction_turn_skips_compact_and_posts':'b8.regressions.py',
    },
    'tests/manager/test_engine_fallback.py':{
        # routing/accounting assertions unchanged; fake engine maps need verified dummy sidecars
        'test_same_account_non_fable_retry_before_next_account':'b8.regressions.py',
        'test_cross_family_fallback_to_codex_when_claude_exhausted':'b8.regressions.py',
        'test_auth_error_skips_model_retry':'b8.regressions.py',
        'test_attempts_jsonl_records_every_attempt':'b8.regressions.py',
        'test_fallback_notice_persists_across_restart_without_duplication':'b8.regressions.py',
        'test_deliberately_selecting_codex_is_not_a_fallback_episode':'b8.regressions.py',
    },
}
def preserve_regressions():
    # Collection counts alone cannot detect replaced assertions or weakened fixtures.
    # Preserve every existing top-level AST statement (including decorators/helpers)
    # while allowing genuinely new tests/imports. Only SUPERSEDED names are exempt.
    spec=Path('loops/b8.md').read_text()
    files=git('ls-tree','-r','--name-only',BASE,'tests/manager').decode().splitlines()
    assert set(SUPERSEDED)<=set(files),'mapping names a file missing from the baseline'
    for file in files:
        if not file.endswith('.py'):continue
        path=ROOT/file
        assert path.is_file() and not path.is_symlink(),f'baseline test removed/replaced: {file}'
        before=ast.parse(git('show',BASE+':'+file).decode())
        after=ast.parse(path.read_text())
        exempt=SUPERSEDED.get(file,{})
        baseline_tests={node.name for node in before.body if isinstance(node,ast.FunctionDef)}
        for name,replacement in exempt.items():
            # The mapping must be honest: a real baseline test, a frozen replacement, documented.
            assert name in baseline_tests,f'mapping names a test absent from the baseline: {name}'
            assert replacement in FROZEN,f'replacement is not a frozen gate: {replacement}'
            assert name in spec,f'replacement not documented in loops/b8.md: {name}'
            if replacement=='b8.regressions.py':
                assert name in Path('loops',replacement).read_text(),f'runner does not name its replaced test: {name}'
        names=[node.name for node in after.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
        assert len(names)==len(set(names)),f'duplicate definition overrides regression: {file}'
        # 2.3: no new module-level skip/collection override may disable retained tests without touching them.
        def overrides(tree):
            found=set()
            for node in tree.body:
                if isinstance(node,(ast.Assign,ast.AnnAssign)):
                    targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target,ast.Name) and target.id in ('pytestmark','collect_ignore','collect_ignore_glob','pytest_plugins'):found.add(target.id)
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('pytest_'):found.add(node.name)
                for d in getattr(node,'decorator_list',[]):
                    text=ast.dump(d)
                    if "attr='skip'" in text or "attr='skipif'" in text or "attr='xfail'" in text:
                        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and node.name in baseline_tests:found.add('decorator:'+node.name)
            return found
        new=overrides(after)-overrides(before)
        assert not new,f'new skip/collection override in {file}: {sorted(new)}'
        retained=[ast.dump(node,include_attributes=False) for node in after.body]
        for node in before.body:
            if isinstance(node,ast.FunctionDef) and node.name in exempt:
                continue  # obligations re-run from BASE_REF by the frozen replacement gate
            original=ast.dump(node,include_attributes=False)
            assert original in retained,f'baseline regression statement changed: {file}:{node.lineno}'
            retained.remove(original)

def inherited_contracts():
    # These contracts cover unchanged bridge/accounting/delivery/memory behavior.
    # Legacy compaction/fallback tests need explicit replacement mapping before launch.
    names=('liveness','context','tracks','migration_integration','empty','blocks','outbox',
           'ledger_failures','attribution','continuation_waiting','persistence_reconciliation',
           'continuation_crashes','waiting_delivery','persistence_dirty_safety',
           'work_validation_reactions','continuation_priority','persistence_tick_retry',
           'settlement_success_only')
    source='loops/b7.acceptance.py'
    assert git('show',BASE+':'+source)==Path(source).read_bytes(),'inherited harness changed'
    for name in names:
        script='import runpy; m=runpy.run_path("loops/b7.acceptance.py"); m['+repr(name)+']()'
        run([sys.executable,'-c',script],120)

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
preserve_regressions()
inherited_contracts()
run([sys.executable,'loops/b8.regressions.py',BASE],120)
JUNIT='/tmp/b8-junit-'+str(os.getpid())+'.xml'
run([sys.executable,'-m','pytest','-q','tests/manager','--junitxml',JUNIT],300)
def baseline_tests_executed():
    # 2.3: every retained baseline test actually ran; skips/xfails of baseline node ids are rejected.
    import xml.etree.ElementTree as ET
    retained=set()
    for file in git('ls-tree','-r','--name-only',BASE,'tests/manager').decode().splitlines():
        if not file.endswith('.py') or file.endswith('conftest.py'):continue
        exempt=SUPERSEDED.get(file,{})
        for node in ast.parse(git('show',BASE+':'+file).decode()).body:
            if isinstance(node,ast.FunctionDef) and node.name.startswith('test_') and node.name not in exempt:retained.add(node.name)
    seen={}
    for case in ET.parse(JUNIT).getroot().iter('testcase'):
        name=case.get('name','').split('[')[0]
        seen.setdefault(name,[]).append([child.tag for child in case])
    for name in sorted(retained):
        assert name in seen,f'retained baseline test did not run: {name}'
        assert not any(tag in ('skipped',) for tags in seen[name] for tag in tags),f'retained baseline test was skipped: {name}'
baseline_tests_executed()
for file in ('b8.identity.py','b8.mode.py','b8.acceptance.py','b8.rollover.py'):
    run([sys.executable,'loops/'+file],120)
# The real gate runs only with explicit scratch evidence input, never production defaults.
run([sys.executable,'loops/b8.live.py'],30)
fence()
preserve_regressions()
print('[b8] PASS')
