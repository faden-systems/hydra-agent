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

REQUIRED = ('pid', 'process_started_at', 'connected', 'envelope_count', 'reconnect_count', 'pong_count',
            'last_activity_age_s', 'at', 'poll_s')
MIN_SPAN_S = 300.0
FRESH_S = 300.0


def read_sample(path):
    sample = {'sampled_at': time.time()}
    try:
        data = json.loads(Path(path).read_text())
        missing = [k for k in REQUIRED if k not in data]
        if missing:
            sample['error'] = 'missing keys: ' + ','.join(missing)
        else:
            sample.update({k: data[k] for k in REQUIRED})
            sample['monotonic'] = data.get('monotonic')
    except (OSError, ValueError) as exc:
        sample['error'] = f'{type(exc).__name__}: {exc}'
    return sample


def breaks(first, previous, current):
    """Why `current` cannot extend a quiet run that started at `first`; None when it can."""
    if 'error' in current:
        return 'unreadable sample: ' + current['error']
    if current['pid'] != first['pid'] or current['process_started_at'] != first['process_started_at']:
        return 'process changed'
    if current['connected'] is not True:
        return 'disconnected'
    if current['envelope_count'] != first['envelope_count']:
        return 'envelope received'
    if current['reconnect_count'] != first['reconnect_count']:
        return 'reconnect attempted'
    if current['pong_count'] < previous['pong_count']:
        return 'pong count decreased'
    if not (current['last_activity_age_s'] < FRESH_S):
        return 'activity stale'
    return None


def evaluate(samples):
    runs, start = [], None
    for index, sample in enumerate(samples):
        if start is None:
            if 'error' not in sample:
                start = index
            continue
        reason = breaks(samples[start], samples[index - 1], sample)
        if reason:
            runs.append((start, index - 1, reason))
            start = None if 'error' in sample else index
    if start is not None:
        runs.append((start, len(samples) - 1, 'end of collection'))
    best, observed = None, False
    for a, b, reason in runs:
        first, last = samples[a], samples[b]
        span = last['sampled_at'] - first['sampled_at']
        rise = last['pong_count'] - first['pong_count']
        distinct_at = len({s['at'] for s in samples[a:b + 1]})
        poll = float(first['poll_s'] or 10)
        rewritten = distinct_at >= max(1, int(span / (2 * poll)))
        qualifies = span > MIN_SPAN_S and rise > 0 and rewritten
        record = {'from': first['sampled_at'], 'to': last['sampled_at'], 'span_s': round(span, 1),
                  'samples': b - a + 1, 'pong_rise': rise, 'distinct_at': distinct_at, 'rewritten': rewritten,
                  'pid': first['pid'], 'broken_by': reason, 'qualifies': qualifies}
        if best is None or span > best['span_s']:
            best = record
        observed = observed or qualifies
    return {'verdict': 'observed' if observed else 'not observed', 'samples': len(samples),
            'required_span_s': MIN_SPAN_S, 'longest_run': best,
            'runs': [{'span_s': round(samples[b]['sampled_at'] - samples[a]['sampled_at'], 1), 'broken_by': r}
                     for a, b, r in runs]}


def collect(path, minutes, interval, out, deployed_sha):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    samples, deadline = [], time.time() + minutes * 60
    with (out / 'samples.jsonl').open('w') as f:
        while True:
            sample = read_sample(path)
            samples.append(sample)
            f.write(json.dumps(sample) + '\n')
            f.flush()
            if time.time() + interval > deadline:
                break
            time.sleep(interval)
    verdict = evaluate(samples)
    verdict.update(path=str(path), minutes=minutes, interval_s=interval, deployed_sha=deployed_sha,
                   samples_sha256=hashlib.sha256((out / 'samples.jsonl').read_bytes()).hexdigest(),
                   observer_host_pid=os.getpid())
    try:
        unit = subprocess.run(['systemctl', 'show', 'hydra-bridge', '-p', 'MainPID,ExecMainStartTimestamp,NRestarts'],
                              capture_output=True, text=True, timeout=10)
        verdict['systemd'] = unit.stdout.strip().splitlines() if unit.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        verdict['systemd'] = None
    (out / 'verdict.json').write_text(json.dumps(verdict, indent=2) + '\n')
    print(verdict['verdict'])
    return verdict


def synthetic(n, interval=10.0, pid=4242, start='2026-10-05T01:00:00Z', mutate=None):
    samples = []
    for i in range(n):
        s = {'sampled_at': 1000.0 + i * interval, 'pid': pid, 'process_started_at': start, 'connected': True,
             'envelope_count': 7, 'reconnect_count': 0, 'pong_count': 100 + i * 2, 'last_activity_age_s': 3.0,
             'at': f'2026-10-05T01:{(i // 6) % 60:02d}:{(i % 6) * 10:02d}Z', 'poll_s': 10, 'monotonic': 500.0 + i * interval}
        if mutate:
            mutate(i, s)
        samples.append(s)
    return samples


def self_test():
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
    # Collection plumbing on a real file: short run, two samples, verdict written.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='b9-observe-') as tmp:
        path = Path(tmp) / 'bridge-health.json'
        path.write_text(json.dumps({k: v for k, v in synthetic(1)[0].items() if k != 'sampled_at'}))
        verdict = collect(str(path), minutes=0.0005, interval=0.01, out=Path(tmp) / 'out', deployed_sha='0' * 40)
        assert verdict['verdict'] == 'not observed' and verdict['samples'] >= 1
        assert (Path(tmp) / 'out' / 'verdict.json').is_file() and (Path(tmp) / 'out' / 'samples.jsonl').is_file()
    print('[b9.observe] self-test PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    if not (args.path and args.out and args.deployed_sha):
        raise SystemExit('--path, --out and --deployed-sha are required')
    collect(args.path, args.minutes, args.interval, args.out, args.deployed_sha)


if __name__ == '__main__':
    main()
