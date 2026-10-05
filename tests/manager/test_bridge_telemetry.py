"""requirement 1/2/3/4/6 (idle-pong telemetry), loops/b9.md: SocketHealth's pong/envelope counters,
run_socket_mode's reconnect_count and per-poll observation file, and write-failure isolation."""
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from conftest import B

SNAPSHOT_KEYS = {"at", "monotonic", "pid", "process_started_at", "connected", "pong_count", "envelope_count",
                 "reconnect_count", "last_activity_age_s", "poll_s"}


def make_client(connected=True, pong=None):
    client = SimpleNamespace(
        current_session=SimpleNamespace(last_ping_pong_time=pong),
        socket_mode_request_listeners=[],
        auto_reconnect_enabled=True,
        default_auto_reconnect_enabled=True,
        connected=connected,
        sent=[],
    )
    client.is_connected = lambda: client.connected
    client.ping = lambda: client.sent.append("ping")  # outbound traffic: must stay invisible to the monitor
    return client


class Handler:
    def __init__(self, client):
        self.client = client

    def connect(self):
        pass

    def start(self):
        raise AssertionError("run_socket_mode must never call handler.start()")


# ----------------------------------------------------------------------------------------------- counters


def test_envelope_count_increments_once_per_receipt():
    health = B.SocketHealth(make_client(), clock=lambda: 0.0, timeout_s=300)
    assert health.envelope_count == 0
    health.note_envelope()
    health.note_envelope(object(), object())
    assert health.envelope_count == 2
    assert health.counters()["envelope_count"] == 2


def test_pong_count_increments_only_on_a_changed_value():
    now = [0.0]
    client = make_client(pong=1)
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    assert health.pong_count == 0
    health.check()  # the initial pong is not an observed change
    assert health.pong_count == 0
    client.current_session.last_ping_pong_time = 2
    now[0] = 10
    health.check()
    assert health.pong_count == 1
    now[0] = 20
    health.check()  # unchanged value: no second count
    assert health.pong_count == 1


def test_repeated_pong_value_on_a_replacement_session_is_not_counted_twice():
    now = [0.0]
    client = make_client(pong=5)
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    now[0] = 10
    health.check()  # first observation of 5
    client.current_session = SimpleNamespace(last_ping_pong_time=5)  # a replacement, same old timestamp
    now[0] = 20
    health.check()
    assert health.pong_count == 0, "a replacement session carrying the same old pong must not count"


def test_a_ping_does_not_count_as_a_pong():
    client = make_client(pong=1)
    health = B.SocketHealth(client, clock=lambda: 0.0, timeout_s=300)
    client.ping()
    health.check()
    assert health.counters() == {"pong_count": 0, "envelope_count": 0}


def test_counters_start_at_zero_and_exposed_both_ways():
    health = B.SocketHealth(make_client(), clock=lambda: 0.0, timeout_s=300)
    assert health.pong_count == 0 and health.envelope_count == 0
    assert health.counters() == {"pong_count": 0, "envelope_count": 0}


# ----------------------------------------------------------------------------------------------- run_socket_mode: reconnect_count


def test_reconnect_count_accumulates_across_attempts_in_one_invocation():
    client = make_client(connected=False)
    attempts = [0]

    def recover(force=False):
        attempts[0] += 1
        if attempts[0] < 3:
            client.connected = True
            client.current_session = SimpleNamespace(last_ping_pong_time=1000 + attempts[0])
        else:
            raise RuntimeError("fixture-fatal")
    client.connect_to_new_endpoint = recover
    checks = [0]
    real_check = B.SocketHealth.check

    def driving_check(self):
        checks[0] += 1
        if checks[0] in (2, 4):
            client.connected = False  # drop again after each successful recovery
        return real_check(self)
    stop = threading.Event()
    threading.Timer(2.0, stop.set).start()
    with patch.object(B.SocketHealth, "check", driving_check):
        with pytest.raises(RuntimeError, match="fixture-fatal"):
            B.run_socket_mode(Handler(client), stop, clock=time.monotonic, poll_s=0.001, reconnect_timeout_s=0.05)
    assert attempts[0] == 3


# ----------------------------------------------------------------------------------------------- snapshot keys


def test_health_snapshot_has_exactly_the_required_keys(tmp_path):
    client = make_client(pong=1)
    health = B.SocketHealth(client, clock=lambda: 42.0, timeout_s=300)
    snap = B.health_snapshot(health, 2, 10, lambda: 42.0)
    assert set(snap) == SNAPSHOT_KEYS
    assert snap["pid"] == os.getpid() and snap["reconnect_count"] == 2 and snap["poll_s"] == 10


# ----------------------------------------------------------------------------------------------- atomic replacement


def test_write_health_observation_leaves_no_leftover_temporary_file(tmp_path):
    path = tmp_path / "bridge-health.json"
    snap = {"at": "2026-10-05T00:00:00Z", "monotonic": 1.0, "pid": os.getpid(),
            "process_started_at": "2026-10-05T00:00:00Z", "connected": True, "pong_count": 0,
            "envelope_count": 0, "reconnect_count": 0, "last_activity_age_s": 0.0, "poll_s": 10}
    assert B.write_health_observation(str(path), snap) is True
    assert json.loads(path.read_text()) == snap
    assert [p.name for p in tmp_path.iterdir()] == ["bridge-health.json"]
    snap2 = dict(snap, pong_count=1)
    assert B.write_health_observation(str(path), snap2) is True
    assert json.loads(path.read_text()) == snap2
    assert [p.name for p in tmp_path.iterdir()] == ["bridge-health.json"], "a stale temporary file was left behind"


def test_write_health_observation_replace_failure_leaves_no_temp_and_no_damage(tmp_path):
    path = tmp_path / "bridge-health.json"
    first = {"at": "2026-10-05T00:00:00Z", "monotonic": 1.0, "pid": os.getpid(),
             "process_started_at": "2026-10-05T00:00:00Z", "connected": True, "pong_count": 0,
             "envelope_count": 0, "reconnect_count": 0, "last_activity_age_s": 0.0, "poll_s": 10}
    assert B.write_health_observation(str(path), first) is True
    with patch.object(B.os, "replace", side_effect=OSError("fixture")):
        assert B.write_health_observation(str(path), dict(first, pong_count=9)) is False
    assert json.loads(path.read_text()) == first, "a failed replacement must not damage the destination"
    assert [p.name for p in tmp_path.iterdir()] == ["bridge-health.json"]


# ----------------------------------------------------------------------------------------------- write failure isolation


def test_write_failure_never_changes_the_health_result_or_recovery_decision(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("a regular file where a directory is expected")
    path = str(blocked / "bridge-health.json")
    client = make_client(connected=False)
    handler = Handler(client)
    with pytest.raises(RuntimeError, match="offline-fixture"):
        def reconnect(force=False):
            raise RuntimeError("offline-fixture")
        client.connect_to_new_endpoint = reconnect
        B.run_socket_mode(handler, threading.Event(), clock=time.monotonic, poll_s=0.001, reconnect_timeout_s=0.05,
                          observation_path=path)
    assert not Path(path).exists(), "the write failure itself must never raise past write_health_observation"
