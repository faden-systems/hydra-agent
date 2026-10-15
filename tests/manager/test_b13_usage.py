"""b13 usage rule (loops/b13.md, founder amendment 2026-10-15): usage ok only when the latest attempt succeeded."""
import re
import pytest
from b11_helpers import S
from test_b13_status import setup

COLLISION = 'Error: Session ID x is already in use.'


def att(at, engine, success=False, classification='other', reason=COLLISION):
    return dict(at=at, engine=engine, model='claude-fable-5-1', rc=0 if success else 1, success=success,
                classification=None if success else classification, reason='' if success else reason)


def account(text, name):
    line = next(x for x in text.splitlines() if x.startswith('accounts: '))
    parts = re.split(r'; (?=(?:claude-r2d2|claude-l|codex):)', line.removeprefix('accounts: '))
    return parts[['claude-r2d2', 'claude-l', 'codex'].index(name)]


def status(home, attempts):
    setup(home, [], attempts)
    return S.status_text(str(home))


# Real claude-r2d2 rows of 2026-10-14/15 (session UUID sanitized): ok, supervisor death, timeout, collisions.
R2D2 = [att('2026-10-14T17:01:51Z', 'claude-r2d2', True),
        att('2026-10-14T17:04:30Z', 'claude-r2d2', reason='supervisor died mid-turn; disposition recorded without a success claim'),
        att('2026-10-14T17:49:23Z', 'claude-r2d2', reason='timeout after 1800s'),
        att('2026-10-14T17:54:25Z', 'claude-r2d2'), att('2026-10-14T18:07:38Z', 'claude-r2d2')]
LIMIT = att('2026-10-15T19:49:49Z', 'claude-r2d2', classification='credits',
            reason="You're out of usage credits. Switch to another model to continue.")
UNKNOWN_SINCE = 'usage: unknown (no successful call since 2026-10-14 10:01:51 PDT)'


def test_real_r2d2_history_is_unknown(tmp_path):
    assert account(status(tmp_path, R2D2), 'claude-r2d2') == (
        'claude-r2d2: session_in_use since 2026-10-14 10:54:25 PDT; last ok 2026-10-14 10:01:51 PDT; '
        'before: timeout at 2026-10-14 10:49:23 PDT; ' + UNKNOWN_SINCE)


def test_limit_after_last_ok_is_retained_through_later_collisions(tmp_path):
    out = account(status(tmp_path, R2D2 + [LIMIT, att('2026-10-15T20:05:00Z', 'claude-r2d2')]), 'claude-r2d2')
    assert out.endswith('; before: usage_limit at 2026-10-15 12:49:49 PDT; usage: limit at 2026-10-15 12:49:49 PDT')


def test_success_resets_limit(tmp_path):
    out = account(status(tmp_path, R2D2 + [LIMIT, att('2026-10-15T21:00:00Z', 'claude-r2d2', True)]), 'claude-r2d2')
    assert out == 'claude-r2d2: ok; last ok 2026-10-15 14:00:00 PDT; usage: ok'


@pytest.mark.parametrize('reason', ['timeout after 1800s', 'supervisor died mid-turn', 'auth failed', 'startup failed', COLLISION])
def test_non_usage_failure_after_ok_is_unknown(tmp_path, reason):
    rows = [att('2026-10-14T17:01:51Z', 'claude-r2d2', True), att('2026-10-14T17:49:23Z', 'claude-r2d2', reason=reason)]
    out = account(status(tmp_path, rows), 'claude-r2d2')
    assert out.endswith(UNKNOWN_SINCE)
    assert 'API not reached' not in out


def test_failures_without_any_success(tmp_path):
    out = account(status(tmp_path, [att('2026-10-14T18:07:38Z', 'claude-r2d2')]), 'claude-r2d2')
    assert out == 'claude-r2d2: session_in_use since 2026-10-14 11:07:38 PDT; last ok unknown; usage: unknown (no successful call recorded)'
    out = account(status(tmp_path, [att('2026-10-14T18:07:38Z', 'claude-r2d2', classification='credits', reason='out of usage credits')]), 'claude-r2d2')
    assert out.endswith('; last ok unknown; usage: limit at 2026-10-14 11:07:38 PDT')


def test_no_attempts_stays_unknown(tmp_path):
    assert account(status(tmp_path, []), 'codex') == 'codex: unknown; last ok unknown; usage: unknown'


def test_other_accounts_do_not_affect_usage(tmp_path):
    out = status(tmp_path, R2D2 + [att('2026-10-15T19:52:43Z', 'claude-l', True)])
    assert account(out, 'claude-l') == 'claude-l: ok; last ok 2026-10-15 12:52:43 PDT; usage: ok'
    assert account(out, 'claude-r2d2').endswith(UNKNOWN_SINCE)
