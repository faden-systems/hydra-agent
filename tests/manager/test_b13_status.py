import importlib.util
import json
import subprocess
from pathlib import Path
import pytest
from b11_helpers import S


def setup(home,turns=(),attempts=(),mode='per-turn'):
    (home/'logs').mkdir(exist_ok=True)
    (home/'engine').write_text(json.dumps(dict(acc='claude-r2d2',model='claude-fable-5-1',mode=mode)))
    for name,data in [('turns',turns),('attempts',attempts)]:
        (home/'logs'/f'{name}.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in data))


@pytest.mark.parametrize('missing',['both','attempts','turns'])
def test_missing_logs(tmp_path,missing):
    setup(tmp_path)
    for name in ['turns','attempts']:
        if missing in ('both',name):(tmp_path/'logs'/f'{name}.jsonl').unlink()
    text=S.status_text(str(tmp_path))
    assert 'last turn ran on unknown (unknown)' in text
    assert 'last turn attempts: none recorded' in text
    assert 'off configured engine:' not in text
    assert 'claude-r2d2: unknown' in text


def test_overlap_not_double_counted(tmp_path):
    turns=[dict(at=100,duration_s=30,engine='codex',model='gpt-6-astra'),dict(at=120,duration_s=20,engine='codex',model='gpt-6-astra')]
    attempts=[dict(at='1970-01-01T00:01:41Z',engine='claude-r2d2',classification='auth',success=False),dict(at='1970-01-01T00:02:05Z',engine='claude-r2d2',classification='rate_limit',success=False)]
    setup(tmp_path,turns,attempts);text=S.status_text(str(tmp_path))
    assert 'reason: auth' in text  # doubling overlap incorrectly makes rate_limit the majority
    assert 'auth' not in next(x for x in text.splitlines() if x.startswith('last turn attempts:'))


@pytest.mark.parametrize('active',[True,False])
def test_preserve_populated_runtime(tmp_path,monkeypatch,active):
    setup(tmp_path,mode='persistent');home=str(tmp_path)
    Path(S.engine_runtime_path(home)).write_text(json.dumps(dict(episode_id='test',active=active,configured=dict(acc='claude-r2d2',model='claude-fable-5-1',mode='persistent'),effective=dict(acc='codex',model='gpt-6-astra',mode='per-turn'),since='2026-10-14T22:00:00Z',reason='credits')))
    Path(S.persistent_status_path(home)).write_text(json.dumps(dict(pid=999999999,turns_served=4,restarts=dict(planned=1,unexpected=2,last=None))))
    file=tmp_path/'baseline.py';file.write_bytes(subprocess.check_output(['git','show','b91679a6db14831b5e3effe257a4c13ee9db6a53:manager/supervisor.py']))
    spec=importlib.util.spec_from_file_location('baseline_b13',file);base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
    monkeypatch.setattr(S,'pid_alive',lambda _:False);monkeypatch.setattr(base,'pid_alive',lambda _:False)
    added=('engine:','last turn attempts:','off configured engine:','accounts:')
    assert [x for x in S.status_text(home).splitlines() if not x.startswith(added)]==[x for x in base.status_text(home).splitlines() if not x.startswith('engine:')]


def test_malformed_mixed_valid_rows(tmp_path):
    setup(tmp_path,[None,[],dict(at='bad'),dict(at=100,duration_s=5,engine='claude-r2d2',model='claude-fable-5-1')],[None,[],dict(at='bad'),dict(at='1970-01-01T00:01:45Z',engine='claude-r2d2',model='claude-fable-5-1',success=True)])
    text=S.status_text(str(tmp_path))
    assert text.splitlines()[0]=='engine: configured claude-r2d2 (claude-fable-5-1) [per-turn] | last turn ran on claude-r2d2 (claude-fable-5-1)'
    assert 'last turn attempts: claude-r2d2/claude-fable-5-1 ok' in text
    assert 'off configured engine:' not in text


def test_file_order_and_incomplete_tail(tmp_path):
    setup(tmp_path,[dict(at=200,duration_s=1,engine='codex',model='gpt-6-astra'),dict(at=100,duration_s=5,engine='claude-r2d2',model='claude-fable-5-1'),dict(at=300,engine='codex'),dict(at=400,duration_s=-1,engine='codex')],[])
    text=S.status_text(str(tmp_path))
    assert 'last turn ran on claude-r2d2 (claude-fable-5-1)' in text
    assert 'last turn attempts: none recorded' in text
    assert 'off configured engine:' not in text
