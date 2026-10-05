#!/usr/bin/env python3
"""Exit-owned offline b9 contracts (loops/b9.md). No real Slack, credentials or network."""
import datetime as dt
import json
import os
import runpy
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'manager'))
import bridge as B  # noqa: E402

KEYS = {'at', 'monotonic', 'pid', 'process_started_at', 'connected', 'pong_count', 'envelope_count',
        'reconnect_count', 'last_activity_age_s', 'poll_s'}


def make_client(connected=True, pong=None):
    client = SimpleNamespace(current_session=SimpleNamespace(last_ping_pong_time=pong),
                             socket_mode_request_listeners=[], auto_reconnect_enabled=True,
                             default_auto_reconnect_enabled=True, connected=connected, reconnects=0, sent=[])
    client.is_connected = lambda: client.connected
    # Outbound traffic the bridge may generate: must be invisible to the monitor.
    client.ping = lambda: client.sent.append('ping')
    client.send_message = lambda message: client.sent.append(message)

    def reconnect(force=False):
        client.reconnects += 1
        raise RuntimeError('offline-fixture')
    client.connect_to_new_endpoint = reconnect
    return client


class Handler:
    def __init__(self, client):
        self.client = client

    def connect(self):
        pass

    def start(self):
        raise AssertionError('blocking start is forbidden')


def counters():
    for name in ('counters',):
        assert hasattr(B.SocketHealth, name), f'SocketHealth.{name} missing (requirement 1)'
    now = [0.0]
    client = make_client(pong=1000)
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    assert health.counters() == {'pong_count': 0, 'envelope_count': 0}
    assert health.pong_count == 0 and health.envelope_count == 0
    assert health.check() is True and health.counters() == {'pong_count': 0, 'envelope_count': 0}, \
        'the initial pong timestamp is not an observed change'
    client.current_session.last_ping_pong_time = 1001
    now[0] = 5; assert health.check() is True
    assert health.pong_count == 1, 'a changed pong counts once'
    now[0] = 10; health.check(); now[0] = 15; health.check()
    assert health.pong_count == 1, 'an unchanged pong must not count again'
    client.current_session = SimpleNamespace(last_ping_pong_time=1001)
    now[0] = 20; health.check()
    assert health.pong_count == 1, 'a replacement session with the same old timestamp is not a pong'
    client.ping(); client.send_message('{"type":"ping"}')
    now[0] = 25; health.check()
    assert health.counters() == {'pong_count': 1, 'envelope_count': 0}, 'outbound traffic never counts'
    client.current_session.last_ping_pong_time = 1002
    now[0] = 30; health.check(); assert health.pong_count == 2
    health.note_envelope(); health.note_envelope(client, object())
    assert health.envelope_count == 2 and health.pong_count == 2, 'one envelope per receipt, pongs untouched'
    client.connected = False
    now[0] = 35; assert health.check() is False
    assert health.counters() == {'pong_count': 2, 'envelope_count': 2}, 'disconnection changes no counter'
    print('counters: ok')


def snapshot_contract():
    for name in ('health_snapshot', 'write_health_observation'):
        assert hasattr(B, name), f'bridge.{name} missing (requirement 3)'
    now = [100.0]
    client = make_client(pong=1)
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    client.current_session.last_ping_pong_time = 2
    now[0] = 140.0; health.check(); health.note_envelope()
    snap = B.health_snapshot(health, 3, 10, lambda: now[0])
    assert set(snap) == KEYS, f'snapshot keys must be exactly the metadata set: {sorted(set(snap) ^ KEYS)}'
    assert snap['pid'] == os.getpid() and snap['connected'] is True
    assert snap['pong_count'] == 1 and snap['envelope_count'] == 1 and snap['reconnect_count'] == 3
    assert snap['poll_s'] == 10 and snap['monotonic'] == 140.0
    assert isinstance(snap['last_activity_age_s'], (int, float)) and 0 <= snap['last_activity_age_s'] <= 0.001
    for key in ('at', 'process_started_at'):
        assert isinstance(snap[key], str) and snap[key].endswith('Z')
        dt.datetime.strptime(snap[key][:19], '%Y-%m-%dT%H:%M:%S')
    again = B.health_snapshot(health, 3, 10, lambda: now[0])
    assert again['process_started_at'] == snap['process_started_at'], 'process start is fixed per process'
    text = json.dumps(snap)
    for forbidden in ('token', 'xoxb', 'xapp', 'http', 'C0', 'U0'):
        assert forbidden not in text, f'metadata leak: {forbidden}'
    with tempfile.TemporaryDirectory(prefix='b9-') as tmp:
        path = Path(tmp) / 'logs' / 'bridge-health.json'
        path.parent.mkdir()
        assert B.write_health_observation(str(path), snap) is True
        assert json.loads(path.read_text()) == snap
        assert [p.name for p in path.parent.iterdir()] == ['bridge-health.json'], 'temporary file left behind'
        snap2 = dict(snap, pong_count=2)
        assert B.write_health_observation(str(path), snap2) is True
        assert json.loads(path.read_text())['pong_count'] == 2
        assert [p.name for p in path.parent.iterdir()] == ['bridge-health.json']
        blocked = Path(tmp) / 'blocked'
        blocked.write_text('not a directory')
        assert B.write_health_observation(str(blocked / 'bridge-health.json'), snap) is False, \
            'a write failure returns False and never raises'
        assert B.write_health_observation(str(path), object()) is False, 'an unserialisable snapshot never raises'
    print('snapshot contract: ok')


def run_monitor(client, observation_path, stop_after, pong_at=None, envelope_at=None, poll_s=0.002):
    handler = Handler(client)
    stop = threading.Event()
    if pong_at is not None:
        threading.Timer(pong_at, lambda: setattr(client.current_session, 'last_ping_pong_time', time.time())).start()
    if envelope_at is not None:
        threading.Timer(envelope_at, lambda: client.socket_mode_request_listeners[0](client, object())).start()
    threading.Timer(stop_after, stop.set).start()
    B.run_socket_mode(handler, stop, clock=time.monotonic, poll_s=poll_s, reconnect_timeout_s=0.05,
                      observation_path=observation_path)
    return handler


def observation_file():
    with tempfile.TemporaryDirectory(prefix='b9-') as tmp:
        path = Path(tmp) / 'logs' / 'bridge-health.json'
        path.parent.mkdir()
        client = make_client(pong=1)
        run_monitor(client, str(path), stop_after=0.08, pong_at=0.02, envelope_at=0.04)
        assert path.is_file(), 'the monitor writes the observation file at every poll'
        snap = json.loads(path.read_text())
        assert set(snap) == KEYS
        assert snap['pid'] == os.getpid() and snap['connected'] is True
        assert snap['pong_count'] == 1 and snap['envelope_count'] == 1 and snap['reconnect_count'] == 0
        assert snap['poll_s'] == 0.002 and snap['last_activity_age_s'] < 300
        assert [p.name for p in path.parent.iterdir()] == ['bridge-health.json']
        assert client.socket_mode_request_listeners, 'the raw receipt listener is still installed'
        assert not client.auto_reconnect_enabled and not client.default_auto_reconnect_enabled
        # Without a path nothing is written and the loop runs as before.
        run_monitor(make_client(pong=1), None, stop_after=0.02)
        # A recovery attempt is counted before its outcome is known.
        failing = Path(tmp) / 'failing.json'
        client = make_client(connected=False)
        try:
            run_monitor(client, str(failing), stop_after=1.0)
        except (RuntimeError, SystemExit, TimeoutError):
            pass
        else:
            raise AssertionError('failed reconnect must escape service main')
        assert client.reconnects == 1
        snap = json.loads(failing.read_text())
        assert snap['reconnect_count'] == 1 and snap['connected'] is False, 'the fatal attempt is in the last snapshot'
        # A successful recovery returns to monitoring and keeps counting.
        client = make_client(connected=False)
        stop = threading.Event()

        def recover(force=False):
            client.reconnects += 1
            client.connected = True
            client.current_session = SimpleNamespace(last_ping_pong_time=None)
            threading.Timer(0.005, lambda: setattr(client.current_session, 'last_ping_pong_time', time.time())).start()
            threading.Timer(0.03, stop.set).start()
        client.connect_to_new_endpoint = recover
        recovered = Path(tmp) / 'recovered.json'
        B.run_socket_mode(Handler(client), stop, clock=time.monotonic, poll_s=0.002, reconnect_timeout_s=0.05,
                          observation_path=str(recovered))
        snap = json.loads(recovered.read_text())
        assert snap['reconnect_count'] == 1 and snap['connected'] is True and snap['pong_count'] >= 1
    print('observation file: ok')


def write_failure_isolated():
    with tempfile.TemporaryDirectory(prefix='b9-') as tmp:
        blocked = Path(tmp) / 'blocked'
        blocked.write_text('a regular file where a directory is expected')
        path = str(blocked / 'bridge-health.json')
        # Healthy loop keeps running to the stop signal despite every write failing.
        client = make_client(pong=1)
        run_monitor(client, path, stop_after=0.05, pong_at=0.01)
        assert client.reconnects == 0 and client.connected
        # Unhealthy loop still recovers (and still fails fatally) exactly as b7 requires.
        client = make_client(connected=False)
        try:
            run_monitor(client, path, stop_after=1.0)
        except (RuntimeError, SystemExit, TimeoutError):
            pass
        else:
            raise AssertionError('write failure must not swallow the recovery failure')
        assert client.reconnects == 1, 'write failure must not change the recovery decision'
    print('write failure isolated: ok')


def recovery_unchanged():
    module = runpy.run_path(str(ROOT / 'loops' / 'b7.acceptance.py'), run_name='b9_inherited')
    module['liveness']()
    print('b7 liveness contract: ok')


if __name__ == '__main__':
    counters()
    snapshot_contract()
    observation_file()
    write_failure_isolated()
    recovery_unchanged()
    print('[b9.acceptance] PASS')
