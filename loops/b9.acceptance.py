#!/usr/bin/env python3
"""Exit-owned offline b9 contracts (loops/b9.md). No real Slack, credentials or network."""
import contextlib
import datetime as dt
import json
import os
import runpy
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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


def fresh_snapshot(**over):
    snap = {'at': '2026-10-05T01:00:00Z', 'monotonic': 1.0, 'pid': os.getpid(), 'process_started_at': '2026-10-05T00:00:00Z',
            'connected': True, 'pong_count': 1, 'envelope_count': 0, 'reconnect_count': 0, 'last_activity_age_s': 0.0, 'poll_s': 10}
    snap.update(over)
    return snap


def atomic_write_contract():
    """Round 1 2.2: the writer completes a sibling temporary file, then replaces atomically; the destination
    survives serialization and replacement failures; no temporary file is ever left behind."""
    with tempfile.TemporaryDirectory(prefix='b9-') as tmp:
        directory = Path(tmp) / 'logs'
        directory.mkdir()
        path = directory / 'bridge-health.json'
        first, second = fresh_snapshot(), fresh_snapshot(pong_count=2)
        assert B.write_health_observation(str(path), first) is True
        seen = []
        real_replace = os.replace

        def checking_replace(src, dst):
            assert Path(src).parent == Path(dst).parent, 'the temporary file must be a sibling of the destination'
            assert json.loads(Path(src).read_text()) == second, 'the temporary file is incomplete before replacement'
            assert json.loads(Path(dst).read_text()) == first, 'the destination changed before the atomic replacement'
            seen.append((src, dst))
            return real_replace(src, dst)
        with patch.object(B.os, 'replace', side_effect=checking_replace):
            assert B.write_health_observation(str(path), second) is True
        assert len(seen) == 1 and json.loads(path.read_text()) == second
        assert [p.name for p in directory.iterdir()] == ['bridge-health.json']
        with patch.object(B.os, 'replace', side_effect=OSError('fixture replace failure')):
            assert B.write_health_observation(str(path), fresh_snapshot(pong_count=3)) is False
        assert json.loads(path.read_text()) == second, 'a failed replacement damaged the destination'
        assert [p.name for p in directory.iterdir()] == ['bridge-health.json'], 'temporary file left after a failed replacement'
        assert B.write_health_observation(str(path), {'unserializable': object()}) is False
        assert json.loads(path.read_text()) == second and [p.name for p in directory.iterdir()] == ['bridge-health.json']
    print('atomic write contract: ok')


def per_poll_contract():
    """Round 1 2.3 / round 2 2.2: polls are counted independently through the health check; the stop comes from
    the check, not the writer; exactly one snapshot follows each check in order, including the poll with a fatal
    recovery, and none follows a normal stop. last_activity_age_s grows by the injected clock's advance."""
    now = [1000.0]
    events = []
    stop = threading.Event()
    real_check, real_writer = B.SocketHealth.check, B.write_health_observation

    def counting_check(self):
        events.append('check')
        if len([e for e in events if e == 'check']) >= 5:
            stop.set()
        result = real_check(self)
        now[0] += 7.0  # the clock advances after every check; nothing arrives
        return result

    def recording_writer(path, snapshot):
        events.append(dict(snapshot))
        return real_writer(path, snapshot)
    with tempfile.TemporaryDirectory(prefix='b9-') as tmp:
        path = Path(tmp) / 'bridge-health.json'
        client = make_client(pong=1)
        with patch.object(B.SocketHealth, 'check', counting_check), patch.object(B, 'write_health_observation', side_effect=recording_writer):
            B.run_socket_mode(Handler(client), stop, clock=lambda: now[0], poll_s=0.001, reconnect_timeout_s=0.05,
                              observation_path=str(path))
        kinds = ['check' if e == 'check' else 'write' for e in events]
        assert kinds == ['check', 'write'] * 5, ('one ordered snapshot per poll, none after the stop', kinds)
        calls = [e for e in events if e != 'check']
        ages = [c['last_activity_age_s'] for c in calls]
        assert ages == [7.0 * (i + 1) for i in range(5)], ('age must grow by exactly the injected advance', ages)
        assert [c['monotonic'] for c in calls] == [1000.0 + 7.0 * (i + 1) for i in range(5)]
        assert all(c['pong_count'] == 0 and c['poll_s'] == 0.001 and c['reconnect_count'] == 0 for c in calls), \
            'the initial pong timestamp is not a counted change'
        assert json.loads(path.read_text())['last_activity_age_s'] == ages[-1]
        # The poll whose recovery is fatal: one check, one recovery, one snapshot, nothing more.
        events.clear()
        stop = threading.Event()
        client = make_client(connected=False)
        with patch.object(B.SocketHealth, 'check', counting_check), patch.object(B, 'write_health_observation', side_effect=recording_writer):
            try:
                B.run_socket_mode(Handler(client), stop, clock=lambda: now[0], poll_s=0.001, reconnect_timeout_s=0.05,
                                  observation_path=str(path))
            except RuntimeError:
                pass
            else:
                raise AssertionError('fatal recovery must escape')
        kinds = ['check' if e == 'check' else 'write' for e in events]
        assert kinds == ['check', 'write'] and client.reconnects == 1, kinds
        assert events[1]['reconnect_count'] == 1 and events[1]['connected'] is False
    print('per-poll contract: ok')


def scheduled_polls(schedule, on_poll, observation_path, logs):
    """Run ONE run_socket_mode invocation for len(schedule) polls; `on_poll(index, flag)` arms the failure
    layer for that poll before its snapshot is built. Returns the `health observation` diagnostics logged."""
    stop = threading.Event()
    polls = []
    real_check = B.SocketHealth.check

    def counting_check(self):
        polls.append(len(polls))
        if len(polls) >= len(schedule):
            stop.set()
        on_poll(len(polls) - 1, schedule[len(polls) - 1])
        return real_check(self)
    before = len(logs)
    with patch.object(B.SocketHealth, 'check', counting_check):
        B.run_socket_mode(Handler(make_client(pong=1)), stop, clock=time.monotonic, poll_s=0.001, reconnect_timeout_s=0.05,
                          observation_path=observation_path)
    assert len(polls) == len(schedule)
    return [m for m in logs[before:] if 'health observation' in m]


def failure_layers(tmp):
    """Round 2 2.1 / round 3 1.1: every way a poll's telemetry can fail, each armed by `layer.arm(flag)` and
    active only inside `layer.patched()`. All go through the production monitor; nothing else is patched."""
    real_snapshot, real_writer, real_replace = B.health_snapshot, B.write_health_observation, os.replace
    layers = []

    class Layer:
        def __init__(self, name, patched, on_arm=None):
            self.name, self.patched, self.on_arm, self.armed = name, patched, on_arm, False
            layers.append(self)

        def arm(self, flag):
            self.armed = flag
            if self.on_arm:
                self.on_arm(flag)

    snapshot_raises = Layer('snapshot raises', lambda: patch.object(B, 'health_snapshot', side_effect=lambda *a, **k: (
        (_ for _ in ()).throw(RuntimeError('fixture snapshot failure')) if snapshot_raises.armed else real_snapshot(*a, **k))))
    writer_false = Layer('writer returns False', lambda: patch.object(B, 'write_health_observation', side_effect=lambda p, snap: (
        False if writer_false.armed else real_writer(p, snap))))
    writer_raises = Layer('writer raises', lambda: patch.object(B, 'write_health_observation', side_effect=lambda p, snap: (
        (_ for _ in ()).throw(OSError('fixture writer failure')) if writer_raises.armed else real_writer(p, snap))))
    serialization = Layer('serialization fails', lambda: patch.object(B, 'health_snapshot', side_effect=lambda *a, **k: (
        dict(real_snapshot(*a, **k), poison=object()) if serialization.armed else real_snapshot(*a, **k))))
    replace_fails = Layer('os.replace fails', lambda: patch.object(B.os, 'replace', side_effect=lambda src, dst: (
        (_ for _ in ()).throw(OSError('fixture replace failure')) if replace_fails.armed else real_replace(src, dst))))
    directory = Path(tmp) / 'fs-layer'
    directory.mkdir()
    filesystem = Layer('directory unwritable', contextlib.nullcontext, lambda flag: os.chmod(directory, 0o500 if flag else 0o700))
    filesystem.directory = directory
    return layers


def telemetry_boundary():
    """Round 1 1.2 / 5.1, round 2 2.1, round 3 1.1: within ONE monitor invocation, the deterministic poll
    sequence fail, fail, ok, fail, fail produces exactly two `health observation` diagnostics across every logging
    layer, for each failure layer separately (snapshot raising, writer returning False, writer raising,
    serialization failure, os.replace failure, unwritable directory) and for consecutive failures that switch
    layers without a successful write. Telemetry failures never alter health or recovery outcomes in any layer."""
    logs = []
    schedule = [True, True, False, True, True]
    with tempfile.TemporaryDirectory(prefix='b9-') as tmp, \
            patch.object(B.S, 'log', side_effect=lambda message: logs.append(str(message))):
        layers = failure_layers(tmp)
        try:
            for layer in layers:
                logs.clear()
                directory = getattr(layer, 'directory', Path(tmp) / layer.name.replace(' ', '-'))
                directory.mkdir(exist_ok=True)
                path = directory / 'bridge-health.json'
                with layer.patched():
                    diagnostics = scheduled_polls(schedule, lambda i, flag: layer.arm(flag), str(path), logs)
                layer.arm(False)
                assert len(diagnostics) == 2, (layer.name, 'exactly two diagnostics for two streaks', diagnostics, logs)
                assert path.is_file(), (layer.name, 'the ok poll wrote the file')
                assert [p.name for p in directory.iterdir()] == ['bridge-health.json'], (layer.name, 'temporary file left behind')
                # Recovery unchanged with this layer armed on the fatal poll: the recovery exception escapes.
                layer.arm(True)
                client = make_client(connected=False)
                with layer.patched():
                    try:
                        run_monitor(client, str(path), stop_after=1.0)
                    except (RuntimeError, SystemExit, TimeoutError) as exc:
                        assert str(exc) == 'offline-fixture', (layer.name, 'the telemetry failure replaced the recovery failure', exc)
                    else:
                        raise AssertionError((layer.name, 'telemetry failure must not swallow the recovery failure'))
                layer.arm(False)
                assert client.reconnects == 1, layer.name
            # Consecutive failures switching layers without a successful write: one streak, one diagnostic; then
            # a success and two more layer-switching failures: a second diagnostic. Exactly two in all.
            by_name = {layer.name: layer for layer in layers}
            sequence = ['snapshot raises', 'writer returns False', 'os.replace fails', None, 'writer raises', 'serialization fails']

            def switch(i, flag):
                for layer in layers:
                    layer.arm(layer.name == sequence[i])
            logs.clear()
            path = Path(tmp) / 'switching' / 'bridge-health.json'
            path.parent.mkdir()
            with contextlib.ExitStack() as stack:
                for name in ('snapshot raises', 'writer returns False', 'os.replace fails'):
                    stack.enter_context(by_name[name].patched())
                # 'writer raises' and 'serialization fails' patch the same seams; emulate them through the armed ones.
                diagnostics = scheduled_polls([x is not None for x in sequence[:4]] + [True, True],
                                              lambda i, flag: switch(i, flag) if i < 4 else by_name['writer returns False'].arm(True) if i == 4 else by_name['snapshot raises'].arm(True),
                                              str(path), logs)
            for layer in layers:
                layer.arm(False)
            assert len(diagnostics) == 2, ('layer-switching streaks: exactly two diagnostics', diagnostics, logs)
            assert path.is_file()
            # A long failing run logs once; no flooding.
            blocked = Path(tmp) / 'blocked'
            blocked.write_text('a regular file where a directory is expected')
            logs.clear()
            diagnostics = scheduled_polls([True] * 25, lambda i, flag: None, str(blocked / 'bridge-health.json'), logs)
            assert len(diagnostics) == 1, ('25 failing polls, one diagnostic', len(diagnostics))
        finally:
            os.chmod(Path(tmp) / 'fs-layer', 0o700)
    print('telemetry boundary: ok')


def repeated_recovery():
    """Round 4 2.1: within ONE monitor invocation, two successful recoveries and a fatal third attempt report
    reconnect_count 1, 2 and 3 in successive snapshots, while envelope and pong counts accumulated earlier are
    preserved across recoveries."""
    client = make_client(connected=False)
    attempts = [0]
    stop = threading.Event()

    def recover(force=False):
        attempts[0] += 1
        client.reconnects += 1
        if attempts[0] == 3:
            raise RuntimeError('offline-fixture')
        client.connected = True
        client.current_session = SimpleNamespace(last_ping_pong_time=1000 + attempts[0])  # a fresh, changed pong
    client.connect_to_new_endpoint = recover
    checks = [0]
    real_check = B.SocketHealth.check

    def driving_check(self):
        checks[0] += 1
        if checks[0] == 2:
            client.socket_mode_request_listeners[0](client, object())  # one envelope while healthy
        if checks[0] in (3, 5):
            client.connected = False  # the connection drops again
        return real_check(self)
    snapshots = []
    real_writer = B.write_health_observation

    def recording_writer(path, snapshot):
        snapshots.append(dict(snapshot))
        return real_writer(path, snapshot)
    threading.Timer(2.0, stop.set).start()
    with tempfile.TemporaryDirectory(prefix='b9-') as tmp:
        with patch.object(B.SocketHealth, 'check', driving_check), patch.object(B, 'write_health_observation', side_effect=recording_writer):
            try:
                B.run_socket_mode(Handler(client), stop, clock=time.monotonic, poll_s=0.001, reconnect_timeout_s=0.05,
                                  observation_path=str(Path(tmp) / 'bridge-health.json'))
            except RuntimeError as exc:
                assert str(exc) == 'offline-fixture', exc
            else:
                raise AssertionError('the third attempt must be fatal')
    assert client.reconnects == 3 and checks[0] == 5, (client.reconnects, checks)
    counts = [s['reconnect_count'] for s in snapshots]
    assert counts == [1, 1, 2, 2, 3], ('successive snapshots count every attempt', counts)
    assert [s['envelope_count'] for s in snapshots] == [0, 1, 1, 1, 1], 'the envelope survives later recoveries'
    assert [s['pong_count'] for s in snapshots] == [1, 1, 2, 2, 2], 'pongs accumulate across recoveries'
    assert snapshots[-1]['connected'] is False and snapshots[2]['connected'] is True
    print('repeated recovery: ok')


def b4_protections():
    """Round 2 8.1: frozen regressions for the two B4 defects of loops/b7.md. (a) A connected but stale client
    (no envelope, no changed pong for 300 s) is recovered with connect_to_new_endpoint(force=True). (b) A reconnect
    that returns connected but shows only the old pong and no new envelope is fatal within the bound."""
    now, step = [0.0], [0.0]

    def clock():
        now[0] += step[0]
        return now[0]
    client = make_client(pong=1)
    forces = []

    def reconnect(force=False):
        client.reconnects += 1
        forces.append(force)
        step[0] = 1.0  # the monitor clock now advances on every read: the grace bound must expire by the clock
    client.connect_to_new_endpoint = reconnect
    polls = [0]
    real_connected = client.is_connected

    def connected():
        polls[0] += 1
        if polls[0] == 2:
            now[0] = 400.0  # second poll: 400 s since the only activity, still connected
        return real_connected()
    client.is_connected = connected
    stop = threading.Event()
    threading.Timer(2.0, stop.set).start()
    with tempfile.TemporaryDirectory(prefix='b9-') as tmp:
        path = Path(tmp) / 'bridge-health.json'
        try:
            B.run_socket_mode(Handler(client), stop, clock=clock, poll_s=0.001, reconnect_timeout_s=0.05, observation_path=str(path))
        except RuntimeError as exc:
            assert 'no fresh inbound activity' in str(exc), exc
        else:
            raise AssertionError('a reconnect with no fresh inbound activity must be fatal (B4)')
        assert forces == [True], ('a stale-but-connected client is recovered with force=True (B4)', forces)
        assert client.reconnects == 1
        snap = json.loads(path.read_text())
        assert snap['reconnect_count'] == 1 and snap['connected'] is True and snap['envelope_count'] == 0 and snap['pong_count'] == 0
    print('b4 protections: ok')


def serve_telemetry():
    """Round 1 B1: the production bridge main supplies <home>/logs/bridge-health.json to the actual monitor.
    Network boundaries are faked exactly as in the inherited b7 serve-child check; bridge.py is not patched
    except to shorten the poll of the same production monitor."""
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--serve-child'],
                           capture_output=True, text=True, timeout=90)
    assert 'TELEMETRY_OK' in child.stdout, (child.stdout[-2000:], child.stderr[-2000:])
    assert child.returncode != 0, 'the fixture recovery failure must still end the production main nonzero'
    print('serve telemetry: ok')


def serve_child():
    import logging
    import slack_bolt
    import slack_bolt.adapter.socket_mode.builtin as adapter
    from slack_bolt.response import BoltResponse
    h = tempfile.mkdtemp(prefix='b9-serve-')
    for name in ('logs', 'inbox', 'mirror', 'credentials'):
        (Path(h) / name).mkdir()
    (Path(h) / 'allowlist.json').write_text(json.dumps({'U': {'instructs': True}}))
    (Path(h) / 'credentials' / 'slack.env').write_text('SLACK_BOT_TOKEN=fake\nSLACK_APP_TOKEN=fake\n')

    class App:
        def __init__(self, **kw):
            self.logger = logging.getLogger('b9')
            self.events = {}
            self.client = SimpleNamespace(proxy=None, auth_test=lambda: {'user_id': 'M'})

        def event(self, name):
            def register(fn):
                self.events[name] = fn
                return fn
            return register

        def dispatch(self, req):
            return BoltResponse(status=200, body='')

    class Client:
        def __init__(self, **kw):
            self.logger = logging.getLogger('b9-client')
            self.socket_mode_request_listeners = []
            self.current_session = SimpleNamespace(last_ping_pong_time=1)
            self.auto_reconnect_enabled = self.default_auto_reconnect_enabled = True
            self.polls = 0

        def connect(self):
            pass

        def is_connected(self):
            # Healthy on the first poll (one snapshot by the real monitor), disconnected afterwards so the
            # inherited fatal-recovery path ends the production main.
            self.polls += 1
            return self.polls <= 1

        def connect_to_new_endpoint(self, force=False):
            raise RuntimeError('fixture recovery failure')

        def send_socket_mode_response(self, response):
            pass

        def close(self):
            pass

        def disconnect(self):
            self.close()

    def pump(self, stop):
        stop.wait(5)
    real_run = B.run_socket_mode

    def fast_run(handler, stop, clock=time.monotonic, poll_s=10, reconnect_timeout_s=30, observation_path=None):
        return real_run(handler, stop, clock=clock, poll_s=0.01, reconnect_timeout_s=0.05, observation_path=observation_path)
    with patch.object(slack_bolt, 'App', App), patch.object(adapter, 'SocketModeClient', Client), \
            patch.object(B.Bridge, 'pump_outbox', pump), patch.object(B, 'run_socket_mode', fast_run):
        try:
            rc = B.main(['--home', h])
        except SystemExit as exc:
            rc = exc.code or 0
    path = Path(h) / 'logs' / 'bridge-health.json'
    assert path.is_file(), 'production serve() did not supply <home>/logs/bridge-health.json to the monitor'
    snap = json.loads(path.read_text())
    assert set(snap) == KEYS and snap['pid'] == os.getpid(), snap
    assert snap['reconnect_count'] == 1 and snap['connected'] is False, snap
    assert [p.name for p in (Path(h) / 'logs').iterdir() if p.name.startswith('bridge-health')] == ['bridge-health.json']
    print('TELEMETRY_OK', flush=True)
    raise SystemExit(rc or 1)


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
    if '--serve-child' in sys.argv:
        serve_child()
    counters()
    snapshot_contract()
    atomic_write_contract()
    observation_file()
    per_poll_contract()
    write_failure_isolated()
    telemetry_boundary()
    repeated_recovery()
    b4_protections()
    serve_telemetry()
    recovery_unchanged()
    print('[b9.acceptance] PASS')
