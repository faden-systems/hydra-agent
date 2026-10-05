#!/usr/bin/env python3
"""Exit-owned idle-pong observation (loops/b9.md requirement 8). Samples the bridge health file, keeps every
sample, and decides `observed` / `not observed` from the samples alone. Metadata only; nothing from Slack."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REQUIRED = ('at', 'monotonic', 'pid', 'process_started_at', 'connected', 'pong_count', 'envelope_count',
            'reconnect_count', 'last_activity_age_s', 'poll_s')  # the exact snapshot schema (requirement 3)
MIN_SPAN_S = 300.0
FRESH_S = 300.0
GAP_FACTOR = 3.0  # a sampling gap longer than GAP_FACTOR x the sampling interval breaks a run (round 1: 2.4)
INFINITE = (float('inf'), float('-inf'))


def _number(value, minimum=0.0, strict=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value != value or value in INFINITE:
        return False
    return value > minimum if strict else value >= minimum


def validate_sample(data):
    """Round 1 5.2: malformed metadata is an error sample. None when well-formed, else the reason."""
    if not isinstance(data, dict):
        return 'not an object'
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        return 'missing keys: ' + ','.join(missing)
    extra = sorted(set(data) - set(REQUIRED))
    if extra:
        return 'unexpected keys: ' + ','.join(extra)  # round 2 2.3: exactly the ten snapshot keys, nothing else
    if not _number(data['monotonic'], minimum=float('-inf')):
        return 'invalid monotonic'
    if isinstance(data['pid'], bool) or not isinstance(data['pid'], int) or data['pid'] <= 0:
        return 'invalid pid'
    for key in ('process_started_at', 'at'):
        if not isinstance(data[key], str) or not data[key]:
            return 'invalid ' + key
    if not isinstance(data['connected'], bool):
        return 'invalid connected'
    for key in ('envelope_count', 'reconnect_count', 'pong_count'):
        if isinstance(data[key], bool) or not isinstance(data[key], int) or data[key] < 0:
            return 'invalid ' + key
    if not _number(data['last_activity_age_s']):
        return 'invalid last_activity_age_s'
    if not _number(data['poll_s'], strict=True):
        return 'invalid poll_s'
    return None


def read_sample(path):
    # Round 1 2.4: observer monotonic time drives spans, gaps and deadlines; wall time is the human record only.
    sample = {'sampled_at': time.time(), 'sampled_mono': time.monotonic()}
    try:
        data = json.loads(Path(path).read_text())
        reason = validate_sample(data)
        if reason:
            sample['error'] = 'malformed: ' + reason
        else:
            sample.update({k: data[k] for k in REQUIRED})
    except (OSError, ValueError) as exc:
        sample['error'] = f'{type(exc).__name__}: {exc}'
    return sample


def eligible(sample):
    """Round 1 B2: a sample may start or extend a quiet run only when readable, connected and fresh."""
    if 'error' in sample:
        return sample['error']
    if sample['connected'] is not True:
        return 'disconnected'
    if not (sample['last_activity_age_s'] < FRESH_S):
        return 'activity stale'
    return None


def mono(sample):
    return sample.get('sampled_mono', sample['sampled_at'])


def breaks(first, previous, current, interval):
    """Why `current` cannot extend a quiet run that started at `first`; None when it can."""
    reason = eligible(current)
    if reason:
        return reason
    if mono(current) - mono(previous) > GAP_FACTOR * interval:
        return 'sampling gap'
    if current['pid'] != first['pid'] or current['process_started_at'] != first['process_started_at']:
        return 'process changed'
    if current['envelope_count'] != first['envelope_count']:
        return 'envelope received'
    if current['reconnect_count'] != first['reconnect_count']:
        return 'reconnect attempted'
    if current['pong_count'] < previous['pong_count']:
        return 'pong count decreased'
    return None


def qualify(samples, a, b):
    """Round 1 2.5: the earliest qualifying prefix of the run a..b, or (None, reasons) when none qualifies."""
    first = samples[a]
    poll = float(first['poll_s'])
    reasons = set()
    for j in range(a, b + 1):
        span = mono(samples[j]) - mono(first)
        if span <= MIN_SPAN_S:
            reasons.add('span too short')
            continue
        rise = samples[j]['pong_count'] - first['pong_count']
        distinct_at = len({s['at'] for s in samples[a:j + 1]})
        rewritten = distinct_at >= max(1, int(span / (2 * poll)))
        if rise <= 0:
            reasons.add('flat pongs')
        if not rewritten:
            reasons.add('insufficient rewrites')
        if rise > 0 and rewritten:
            return {'from_index': a, 'to_index': j, 'from': first['sampled_at'], 'to': samples[j]['sampled_at'],
                    'span_s': round(span, 1), 'samples': j - a + 1, 'pong_rise': rise, 'distinct_at': distinct_at}, None
    if reasons - {'span too short'}:
        reasons.discard('span too short')  # some prefix was long enough; report what failed there
    return None, sorted(reasons)


def evaluate(samples, interval=None):
    if interval is None:
        interval = max((float(s['poll_s']) for s in samples if 'error' not in s), default=10.0)
    runs, start = [], None
    for index, sample in enumerate(samples):
        if start is None:
            if eligible(sample) is None:
                start = index
            continue
        reason = breaks(samples[start], samples[index - 1], sample, interval)
        if reason:
            runs.append((start, index - 1, reason))
            start = index if eligible(sample) is None else None
    if start is not None:
        runs.append((start, len(samples) - 1, 'end of collection'))
    best, qualifying, records = None, None, []
    for a, b, reason in runs:
        first, last = samples[a], samples[b]
        span = mono(last) - mono(first)
        interval_record, why_not = qualify(samples, a, b)
        record = {'from': first['sampled_at'], 'to': last['sampled_at'], 'span_s': round(span, 1),
                  'samples': b - a + 1, 'pong_rise': last['pong_count'] - first['pong_count'], 'pid': first['pid'],
                  'broken_by': reason, 'qualifies': interval_record is not None, 'qualification_failure': why_not}
        records.append(record)
        if best is None or span > best['span_s']:
            best = record
        if interval_record is not None and qualifying is None:
            qualifying = interval_record
    return {'verdict': 'observed' if qualifying else 'not observed', 'samples': len(samples),
            'required_span_s': MIN_SPAN_S, 'sampling_interval_s': interval, 'longest_run': best,
            'qualifying_interval': qualifying, 'runs': records}


def collect(path, minutes, interval, out, deployed_sha):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    samples, deadline = [], time.monotonic() + minutes * 60  # round 1 2.4: monotonic deadline
    with (out / 'samples.jsonl').open('w') as f:
        while True:
            sample = read_sample(path)
            samples.append(sample)
            f.write(json.dumps(sample) + '\n')
            f.flush()
            if time.monotonic() + interval > deadline:
                break
            time.sleep(interval)
    # Round 2 5.1: `evaluation` is exactly evaluate()'s output and `samples_sha256` the digest of the retained
    # file; recompute() returns exactly these two fields, so closure compares them field for field. Deployment
    # and collection metadata live beside them and are validated separately.
    evaluation = evaluate(samples, interval)
    verdict = {'verdict': evaluation['verdict'], 'evaluation': evaluation,
               'samples_sha256': hashlib.sha256((out / 'samples.jsonl').read_bytes()).hexdigest(),
               'metadata': {'path': str(path), 'minutes': minutes, 'interval_s': interval, 'deployed_sha': deployed_sha,
                            'observer_host_pid': os.getpid(), 'systemd': None}}
    try:
        unit = subprocess.run(['systemctl', 'show', 'hydra-bridge', '-p', 'MainPID,ExecMainStartTimestamp,NRestarts'],
                              capture_output=True, text=True, timeout=10)
        verdict['metadata']['systemd'] = unit.stdout.strip().splitlines() if unit.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    (out / 'verdict.json').write_text(json.dumps(verdict, indent=2) + '\n')
    print(verdict['verdict'])
    return verdict


def at_of(i):
    return f'2026-10-05T01:{(i // 6) % 60:02d}:{(i % 6) * 10:02d}Z'


def synthetic(n, interval=10.0, pid=4242, start='2026-10-05T01:00:00Z', mutate=None):
    samples = []
    for i in range(n):
        s = {'sampled_at': 1000.0 + i * interval, 'sampled_mono': 5000.0 + i * interval, 'pid': pid,
             'process_started_at': start, 'connected': True, 'envelope_count': 7, 'reconnect_count': 0,
             'pong_count': 100 + i * 2, 'last_activity_age_s': 3.0, 'at': at_of(i), 'poll_s': 10,
             'monotonic': 500.0 + i * interval}
        if mutate:
            mutate(i, s)
        samples.append(s)
    return samples


def snapshot_of(s):
    """The on-disk snapshot a synthetic sample stands for: exactly the ten snapshot keys."""
    return {k: s[k] for k in REQUIRED}


def malform(s, **fields):
    """Turn synthetic sample `s` in place into what read_sample records for malformed metadata."""
    data = snapshot_of(s)
    data.update(fields)
    reason = validate_sample(data)
    assert reason, 'fixture meant to be malformed is well-formed'
    keep = {'sampled_at': s['sampled_at'], 'sampled_mono': s['sampled_mono']}
    s.clear()
    s.update(keep, error='malformed: ' + reason)


def self_test():
    import tempfile
    assert evaluate(synthetic(40))['verdict'] == 'observed'
    assert evaluate(synthetic(30))['verdict'] == 'not observed', 'exactly 290s must not qualify'
    assert evaluate(synthetic(32))['verdict'] == 'observed', '310s qualifies'

    def envelope_mid(i, s):
        if i >= 20: s['envelope_count'] = 8
    v = evaluate(synthetic(40, mutate=envelope_mid))
    assert v['verdict'] == 'not observed' and v['runs'][0]['broken_by'] == 'envelope received', v

    def reconnect_mid(i, s):
        if i >= 20: s['reconnect_count'] = 1
    assert evaluate(synthetic(40, mutate=reconnect_mid))['verdict'] == 'not observed'

    def restart_mid(i, s):
        if i >= 20: s['pid'] = 4343; s['process_started_at'] = '2026-10-05T01:03:00Z'
    assert evaluate(synthetic(40, mutate=restart_mid))['verdict'] == 'not observed'

    def disconnect_once(i, s):
        if i == 20: s['connected'] = False
    assert evaluate(synthetic(40, mutate=disconnect_once))['verdict'] == 'not observed'

    def flat_pongs(i, s):
        s['pong_count'] = 100
    assert evaluate(synthetic(40, mutate=flat_pongs))['verdict'] == 'not observed', 'a flat pong count is not idle-pong evidence'

    def stale(i, s):
        if i == 25: s['last_activity_age_s'] = 300.0
    assert evaluate(synthetic(40, mutate=stale))['verdict'] == 'not observed'

    def frozen_file(i, s):
        s['at'] = '2026-10-05T01:00:00Z'
    assert evaluate(synthetic(40, mutate=frozen_file))['verdict'] == 'not observed', 'a file no longer rewritten cannot qualify'

    def unreadable_mid(i, s):
        if i == 20:
            for k in list(s):
                if k != 'sampled_at': s.pop(k)
            s['error'] = 'OSError: gone'
    assert evaluate(synthetic(40, mutate=unreadable_mid))['verdict'] == 'not observed'
    # A quiet stretch after a busy start still qualifies: the envelope break starts a new run.
    def busy_then_quiet(i, s):
        if i < 5: s['envelope_count'] = 7 - (5 - i)
    assert evaluate(synthetic(45, mutate=busy_then_quiet))['verdict'] == 'observed'
    # Round 1 B2: an ineligible first sample never starts a run; the healthy remainder of 32 spans exactly 300 s.
    def disconnected_first(i, s):
        if i == 0: s['connected'] = False
    assert evaluate(synthetic(32, mutate=disconnected_first))['verdict'] == 'not observed', 'disconnected first sample started a run'
    def stale_first(i, s):
        if i == 0: s['last_activity_age_s'] = 300.0
    assert evaluate(synthetic(32, mutate=stale_first))['verdict'] == 'not observed', 'stale first sample started a run'
    assert evaluate(synthetic(33, mutate=disconnected_first))['verdict'] == 'observed', '310 s after an ineligible first sample qualifies'
    # Round 1 2.5: a qualifying prefix followed by a frozen-file tail still qualifies and the interval is retained.
    def frozen_tail(i, s):
        if i >= 32: s['at'] = at_of(31); s['pong_count'] = 100 + 31 * 2
    v = evaluate(synthetic(80, mutate=frozen_tail))
    assert v['verdict'] == 'observed' and v['qualifying_interval']['to_index'] == 31, v['qualifying_interval']
    assert v['longest_run']['span_s'] == 790.0 and v['longest_run']['qualifies'], v['longest_run']
    # Round 1 2.4: wall-clock jumps do not inflate spans; an observer stall breaks the run as a sampling gap.
    def wall_jump(i, s):
        if i >= 10: s['sampled_at'] += 10000
    assert evaluate(synthetic(31, mutate=wall_jump))['verdict'] == 'not observed', 'wall-clock jump inflated the span'
    def stall(i, s):
        if i >= 20: s['sampled_mono'] += 60; s['sampled_at'] += 60
    v = evaluate(synthetic(40, mutate=stall))
    assert v['verdict'] == 'not observed' and v['runs'][0]['broken_by'] == 'sampling gap', v['runs']
    assert evaluate(synthetic(40, mutate=stall), interval=30)['verdict'] == 'observed', 'the gap rule scales with the declared interval'
    # Round 1 5.2: malformed metadata is an error sample; it breaks the run and never crashes evaluation.
    def null_counter(i, s):
        if i == 20: malform(s, pong_count=None)
    v = evaluate(synthetic(40, mutate=null_counter))
    assert v['verdict'] == 'not observed' and v['runs'][0]['broken_by'] == 'malformed: invalid pong_count', v['runs']
    def string_age(i, s):
        if i == 20: malform(s, last_activity_age_s='3')
    assert evaluate(synthetic(40, mutate=string_age))['verdict'] == 'not observed'
    def bool_pid(i, s):
        if i == 20: malform(s, pid=True)
    assert evaluate(synthetic(40, mutate=bool_pid))['verdict'] == 'not observed'
    def nan_age(i, s):
        if i == 20: malform(s, last_activity_age_s=float('nan'))
    assert evaluate(synthetic(40, mutate=nan_age))['verdict'] == 'not observed'
    for bad in ({'pid': 0}, {'poll_s': 0}, {'connected': 'yes'}, {'envelope_count': -1}, {'at': ''}, {'process_started_at': 7},
                {'monotonic': 'x'}, {'monotonic': float('nan')}, {'monotonic': None}):
        assert validate_sample(dict(snapshot_of(synthetic(1)[0]), **bad)), bad
    assert validate_sample([]) == 'not an object' and validate_sample({}).startswith('missing keys')
    # Round 2 2.3: exactly the snapshot schema. An extra key (a payload, an id, anything) is malformed metadata,
    # and so is a missing or invalid monotonic; read_sample records the violation instead of discarding it.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='b9-schema-') as tmp:
        p = Path(tmp) / 'bridge-health.json'
        good = snapshot_of(synthetic(1)[0])
        p.write_text(json.dumps(good))
        assert 'error' not in read_sample(str(p)) and set(read_sample(str(p))) == set(REQUIRED) | {'sampled_at', 'sampled_mono'}
        p.write_text(json.dumps(dict(good, channel='C0123')))
        assert read_sample(str(p))['error'] == 'malformed: unexpected keys: channel'
        p.write_text(json.dumps({k: v for k, v in good.items() if k != 'monotonic'}))
        assert read_sample(str(p))['error'] == 'malformed: missing keys: monotonic'
        p.write_text(json.dumps(dict(good, monotonic='soon')))
        assert read_sample(str(p))['error'] == 'malformed: invalid monotonic'
        p.write_text('[1, 2]')
        assert read_sample(str(p))['error'] == 'malformed: not an object'
        p.write_text('{not json')
        assert read_sample(str(p))['error'].startswith('JSONDecodeError')
        p.unlink()
        assert read_sample(str(p))['error'].startswith('FileNotFoundError')
    # Round 1 5.2: explicit reasons when the longest run does not qualify.
    assert evaluate(synthetic(40, mutate=flat_pongs))['longest_run']['qualification_failure'] == ['flat pongs']
    assert evaluate(synthetic(40, mutate=frozen_file))['longest_run']['qualification_failure'] == ['insufficient rewrites']
    assert evaluate(synthetic(30))['longest_run']['qualification_failure'] == ['span too short']
    # Round 1 F1: recomputation from a retained samples file reproduces the evaluation and digests the bytes.
    with tempfile.TemporaryDirectory(prefix='b9-eval-') as tmp:
        p = Path(tmp) / 'samples.jsonl'
        p.write_bytes(''.join(json.dumps(x) + '\n' for x in synthetic(40)).encode())
        again = recompute(str(p), 10.0)
        assert set(again) == {'evaluation', 'samples_sha256'}
        assert again['evaluation'] == evaluate(synthetic(40), 10.0) and again['evaluation']['verdict'] == 'observed'
        assert again['samples_sha256'] == hashlib.sha256(p.read_bytes()).hexdigest()
    # Collection plumbing on a real file: short run, verdict written; round 2 5.1: collect -> recompute round trip
    # is an exact field-for-field match (evaluation and digest), from the files on disk, with the recorded interval.
    with tempfile.TemporaryDirectory(prefix='b9-observe-') as tmp:
        path = Path(tmp) / 'bridge-health.json'
        path.write_text(json.dumps(snapshot_of(synthetic(1)[0])))
        out = Path(tmp) / 'out'
        verdict = collect(str(path), minutes=0.0005, interval=0.01, out=out, deployed_sha='0' * 40)
        assert verdict['verdict'] == 'not observed' and verdict['evaluation']['samples'] >= 2
        assert set(verdict) == {'verdict', 'evaluation', 'samples_sha256', 'metadata'}
        assert set(verdict['metadata']) == {'path', 'minutes', 'interval_s', 'deployed_sha', 'observer_host_pid', 'systemd'}
        recorded = json.loads((out / 'verdict.json').read_text())
        again = recompute(str(out / 'samples.jsonl'), recorded['metadata']['interval_s'])
        assert again == {'evaluation': recorded['evaluation'], 'samples_sha256': recorded['samples_sha256']}, (again, recorded)
        assert closure_check(out) is None
        (out / 'samples.jsonl').write_text('')
        assert closure_check(out) == 'samples digest differs'
    print('[b9.observe] self-test PASS')


def recompute(samples_path, interval=None):
    """Round 1 F1 / round 2 5.1: recompute exactly the comparable fields from a retained samples.jsonl."""
    raw = Path(samples_path).read_bytes()
    samples = [json.loads(line) for line in raw.decode().splitlines() if line.strip()]
    return {'evaluation': evaluate(samples, interval), 'samples_sha256': hashlib.sha256(raw).hexdigest()}


def closure_check(evidence_dir):
    """Requirement 9: None when the recorded verdict.json matches a recomputation from samples.jsonl field for
    field (evaluation and digest, using the recorded interval); otherwise the first difference."""
    evidence_dir = Path(evidence_dir)
    recorded = json.loads((evidence_dir / 'verdict.json').read_text())
    again = recompute(str(evidence_dir / 'samples.jsonl'), recorded['metadata']['interval_s'])
    if again['samples_sha256'] != recorded.get('samples_sha256'):
        return 'samples digest differs'
    if again['evaluation'] != recorded.get('evaluation'):
        return 'evaluation differs'
    if recorded.get('verdict') != again['evaluation']['verdict']:
        return 'verdict field differs from evaluation'
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evaluate', metavar='SAMPLES_JSONL', help='recompute evaluation and digest from retained samples')
    parser.add_argument('--closure-check', metavar='EVIDENCE_DIR', help='compare verdict.json with a recomputation; exit 1 on difference')
    parser.add_argument('--path')
    parser.add_argument('--minutes', type=float, default=15)
    parser.add_argument('--interval', type=float, default=10)
    parser.add_argument('--out')
    parser.add_argument('--deployed-sha')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.evaluate:
        print(json.dumps(recompute(args.evaluate, args.interval), indent=2, sort_keys=True))
        return
    if args.closure_check:
        difference = closure_check(args.closure_check)
        print(difference or 'closure check: recorded verdict matches recomputation')
        raise SystemExit(1 if difference else 0)
    if not (args.path and args.out and args.deployed_sha):
        raise SystemExit('--path, --out and --deployed-sha are required')
    collect(args.path, args.minutes, args.interval, args.out, args.deployed_sha)


if __name__ == '__main__':
    main()
