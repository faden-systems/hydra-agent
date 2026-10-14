"""Conservative range verification at API boundaries beyond the frozen timer scenarios."""
import copy
import pytest
from b11_helpers import S

OLD, NEW = 'a' * 40, 'b' * 40
REPO = 'faden-systems/faden'


def responses():
    parent = {'sha': OLD, 'url': 'https://example.test/parent'}
    entry = {'sha': NEW, 'parents': [parent]}
    compare = {'status': 'ahead', 'merge_base_commit': {'sha': OLD}, 'total_commits': 1,
               'ahead_by': 1, 'behind_by': 0, 'commits': [entry]}
    identity = {'name': 'hydra-manager', 'email': 'manager@hydra.local'}
    detail = {**copy.deepcopy(entry), 'commit': {'author': identity, 'committer': identity,
              'message': 'manager: turn 42\n'}, 'files': [{'status': 'renamed',
              'filename': 'factory/log/new.jsonl', 'previous_filename': 'factory/log/old.jsonl'}]}
    return compare, detail


def verify(compare, detail, repo=REPO):
    sup = object.__new__(S.Supervisor)
    sup._timer_gh = lambda cfg, path: compare if '/compare/' in path else detail
    return sup._timer_persistence_only({}, repo, OLD, NEW)


def test_complete_range_accepts_api_parent_urls_and_internal_rename():
    assert verify(*responses())


@pytest.mark.parametrize('case', ['other-repo', 'boolean-count', 'boolean-ahead', 'boolean-behind',
                                  'overflow', 'empty-range', 'empty-files', 'unknown-author',
                                  'missing-rename-origin', 'absolute', 'empty-component', 'dot-component',
                                  'prefix-lookalike', 'unexpected-origin', 'wrong-detail-parent', 'message-body'])
def test_uncertain_range_always_retains_wake(case):
    compare, detail = responses()
    if case == 'other-repo':
        assert not verify(compare, detail, 'other/faden')
        return
    if case == 'boolean-count': compare['total_commits'] = True
    if case == 'boolean-ahead': compare['ahead_by'] = True
    if case == 'boolean-behind': compare['behind_by'] = False
    if case == 'overflow': compare['total_commits'] = compare['ahead_by'] = 101
    if case == 'empty-range': compare['commits'] = []
    if case == 'empty-files': detail['files'] = []
    if case == 'unknown-author': detail['commit']['author'] = {'name': 'someone', 'email': 'someone@example.test'}
    if case == 'missing-rename-origin': detail['files'][0].pop('previous_filename')
    if case == 'absolute': detail['files'][0]['filename'] = '/factory/log/file'
    if case == 'empty-component': detail['files'][0]['filename'] = 'factory/log//file'
    if case == 'dot-component': detail['files'][0]['filename'] = 'factory/log/./file'
    if case == 'prefix-lookalike': detail['files'][0]['filename'] = 'factory/logger/file'
    if case == 'unexpected-origin': detail['files'][0].update(status='modified', previous_filename='src/app.py')
    if case == 'wrong-detail-parent': detail['parents'] = [{'sha': 'c' * 40}]
    if case == 'message-body': detail['commit']['message'] += 'extra text'
    assert not verify(compare, detail)
