import json
from pathlib import Path
import pytest
from b11_helpers import S
from test_b13_status import setup

@pytest.mark.parametrize('at,expected',[(1792108800,'2026-10-15 17:00:00 PDT'),(1793620800,'2026-11-02 04:00:00 PST')])
def test_full_date_today(tmp_path,monkeypatch,at,expected):
    monkeypatch.setattr(S.time,'time',lambda:at)
    setup(tmp_path,[dict(n=1,at=at,duration_s=1,engine='codex',model='gpt-6-astra')])
    assert 'last turn: '+expected in S.status_text(str(tmp_path))

@pytest.mark.parametrize('reset,at,expected',[
 ('2026-11-02T14:00:00+02:00',1793620800,'2026-11-02 04:00:00 PST'),
 ('8pm',1793620800,'8pm PST'),('8pm',1792108800,'8pm PDT'),('8pm XYZ',1792108800,'8pm XYZ')])
def test_reset_formats(tmp_path,reset,at,expected):
    row=dict(at=at,engine='claude-l',success=False,classification='credits',reason='usage credits; resets '+reset)
    setup(tmp_path,attempts=[row]);assert 'reset '+expected in S.status_text(str(tmp_path))
    row=dict(row,at=at+1,reason='usage credits')
    setup(tmp_path,attempts=[dict(row,at=at,reason='resets '+reset),row]);assert 'reset ' not in S.status_text(str(tmp_path))

def test_continuation_and_unknown_context(tmp_path,monkeypatch):
    at=1793620800;monkeypatch.setattr(S.time,'time',lambda:at)
    setup(tmp_path,[dict(n=1,at=at,duration_s=1,engine='codex')])
    (tmp_path/'config.json').write_text(json.dumps(dict(continuation=dict(max_per_hour=1))))
    (tmp_path/'logs/continuation-reservations.jsonl').write_text(json.dumps(dict(at=at-10))+'\n')
    out=S.status_text(str(tmp_path))
    assert 'continuation: capped until 2026-11-02 04:59:50 PST' in out
    assert 'last Claude turn unknown; 1 turns since on codex' in out


def test_mixed_unknown_context_and_compaction(tmp_path):
    at=1793620800
    rows=[dict(at=at+i,duration_s=1,engine=e) for i,e in enumerate(['claude-l','codex',None,'codex','other-engine'])]
    rows.insert(3,dict(at=at+2,kind='compaction',duration_s=1,engine='claude-l'))
    setup(tmp_path,rows)
    assert '(stale: last Claude turn 2026-11-02 04:00:00 PST; 4 turns since on mixed (codex, unknown, other-engine))' in S.status_text(str(tmp_path))
    setup(tmp_path,rows[1:])
    assert '(stale: last Claude turn unknown; 4 turns since on mixed (codex, unknown, other-engine))' in S.status_text(str(tmp_path))


def test_display_conflict_precedence():
    collision='Error: Session ID x is already in use.'
    assert S._status_classification(dict(classification='credits',reason=collision))=='usage_limit'
    assert S._status_classification(dict(classification='timeout',reason=collision))=='session_in_use'
    assert S._status_classification(dict(classification='startup',reason='supervisor died mid-turn'))=='supervisor_died'
