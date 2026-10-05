#!/usr/bin/env python3
"""Run original fallback assertions with exit-owned dummy identity setup."""
import ast
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTING = {
    'test_same_account_non_fable_retry_before_next_account',
    'test_cross_family_fallback_to_codex_when_claude_exhausted',
    'test_auth_error_skips_model_retry',
    'test_attempts_jsonl_records_every_attempt',
    'test_fallback_notice_persists_across_restart_without_duplication',
    'test_deliberately_selecting_codex_is_not_a_fallback_episode',
}

def verified_fixture(home, engines):
    """Only generated scratch values; never patch the production identity resolver."""
    directory = Path(home) / 'credentials'
    directory.mkdir(exist_ok=True)
    for name, spec in engines.items():
        if name not in ('claude-r2d2', 'claude-l'):
            continue
        token = 'b8-regression-dummy-' + name
        credential = directory / (name + '.env')
        credential.write_text('CLAUDE_CODE_OAUTH_TOKEN=' + token + '\n')
        credential.chmod(0o600)
        spec['cred'] = credential.name
        Path(str(credential) + '.identity.json').write_text(json.dumps({
            'schema_version': 1, 'label': name, 'account_id': 'fixture-' + name,
            'token_sha256': hashlib.sha256(token.encode()).hexdigest(),
            'verified_at': '2026-10-04T20:00:00Z',
            'method': 'private-window-and-usage-bar',
            'evidence': 'https://app.slack.com/archives/Cfixture/p123',
        }))

def adapted_source(source):
    tree = ast.parse(source)
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name not in ROUTING:
            continue
        found.add(node.name)
        original = [ast.dump(n, include_attributes=False) for n in node.body]
        indices = [i for i, n in enumerate(node.body)
                   if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'eng_map' for t in n.targets)]
        assert len(indices) == 1, 'baseline fixture layout changed: ' + node.name
        node.body.insert(indices[0] + 1, ast.parse('_b8_verified_fixture(home, eng_map)').body[0])
        retained = [ast.dump(n, include_attributes=False) for i, n in enumerate(node.body) if i != indices[0] + 1]
        assert retained == original, 'original regression statements changed'
    assert found == ROUTING, 'missing baseline routing cases'
    return ast.unparse(ast.fix_missing_locations(tree))

# Replaces the pressure-triggered setup of these BASE_REF tests with an explicit manual request;
# their ledger, backoff, session-retention and truthful-reporting obligations are asserted below:
#   test_failed_compact_posts_once_keeps_the_session_and_backs_off
#   test_no_reduction_after_compact_is_a_failure
#   test_failed_compaction_turn_skips_compact_and_posts
def manual_compaction_contracts(base, conftest):
    source = subprocess.check_output(['git', 'show', base + ':tests/manager/test_compaction.py'], cwd=ROOT).decode()
    fixture = {}
    exec(compile(source, '<frozen-b8-compaction-fixtures>', 'exec'), fixture)
    S = conftest.S
    for failure in ('usage limit reached', 'unclassified memory-write failure', None):
        with tempfile.TemporaryDirectory(prefix='b8-manual-regression-') as tmp:
            root = Path(tmp)
            home = conftest.make_home(str(root / 'home'))
            fixture['config'](home)
            poster = fixture['Poster']()
            sup, directory = fixture['make'](home, root, 740000, blog=poster,
                fail_compact=failure is not None, fail_message=failure,
                after_tokens=600000)
            verified_fixture(home, sup.engines)
            conftest.queue_event(home, 'warm')
            assert sup.run_once() is True
            # The trigger is explicit; token pressure must not be needed to reach coverage.
            Path(home, 'COMPACT').write_text('b8 explicit manual request\n')
            for index in range(4):
                conftest.queue_event(home, 'after-' + str(index))
                assert sup.run_once() is True
            record = sup.compaction_state()['last']
            assert record['ok'] is False and record['before_tokens'] == 740000
            assert len(poster.posted) == 1, 'failure notice duplicated'
            assert len(fixture['compact_calls'](directory)) == 1, 'memory request retried on routine ticks'
            if failure is not None:
                assert Path(home, 'session-id').read_text().strip() == 'sess-test'
                assert record.get('after_tokens') is None and record['error']
                rows = fixture['turns'](home)
                assert len(rows) == 5 and not any(row.get('kind') == 'compaction' for row in rows)
                failed = [row for row in S.read_jsonl(str(Path(home, 'logs', 'attempts.jsonl'))) if row.get('success') is False]
                assert len(failed) == 1
                assert failed[0]['classification'] == ('usage_limit' if failure == 'usage limit reached' else 'other')
                assert '(740000 -> failed)' in S.status_text(home)
            else:
                assert record['after_tokens'] == 600000
                assert record['old_id'] == 'sess-test' and record['new_id'] != 'sess-test'
                assert record['new_id'] and '600000' in poster.posted[0][2]
    print('manual compaction failure/reporting contracts PASS')

def main(base):
    source = subprocess.check_output(['git', 'show', base + ':tests/manager/test_engine_fallback.py'], cwd=ROOT).decode()
    sys.path.insert(0, str(ROOT / 'tests' / 'manager'))
    import conftest
    # Original functions/assertions are loaded from the frozen spec parent, not candidate tests.
    namespace = {'_b8_verified_fixture': verified_fixture}
    exec(compile(adapted_source(source), '<frozen-b8-fallback-regressions>', 'exec'), namespace)
    for name in sorted(ROUTING):
        with tempfile.TemporaryDirectory(prefix='b8-regression-') as tmp:
            root = Path(tmp)
            home = conftest.make_home(str(root / 'home'))
            args = {'home': home}
            if 'tmp_path' in namespace[name].__code__.co_varnames[:namespace[name].__code__.co_argcount]:
                args['tmp_path'] = root
            namespace[name](**args)
    print('six frozen fallback regressions PASS')
    manual_compaction_contracts(base, conftest)

if __name__ == '__main__':
    main(sys.argv[1])
