"""requirement 1-2 (bridge Socket Mode liveness and recovery), loops/b7.md: SocketHealth's monotonic activity
window, and run_socket_mode's bounded recovery, SDK listener wiring, and auto-reconnect override."""
import threading
import time
from types import SimpleNamespace

import pytest

from conftest import B


def make_client(connected=True, pong=None):
    return SimpleNamespace(
        current_session=SimpleNamespace(last_ping_pong_time=pong),
        is_connected=lambda: connected,
        socket_mode_request_listeners=[],
        auto_reconnect_enabled=True,
        default_auto_reconnect_enabled=True,
    )


def test_healthy_within_grace_then_stale_without_activity():
    now = [0.0]
    client = make_client()
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    assert health.check() is True
    now[0] = 299
    assert health.check() is True
    now[0] = 300
    assert health.check() is False


def test_note_envelope_refreshes_activity():
    now = [0.0]
    client = make_client()
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    now[0] = 300
    assert health.check() is False
    health.note_envelope()
    assert health.check() is True
    now[0] = 599
    assert health.check() is True


def test_note_envelope_accepts_any_args_as_a_raw_listener():
    client = make_client()
    health = B.SocketHealth(client, clock=lambda: 0.0, timeout_s=300)
    health.note_envelope(client, {"type": "events_api"})  # (client, request) shape, never raises


def test_disconnected_is_immediately_unhealthy_even_with_fresh_pong():
    client = make_client(connected=False, pong=123.0)
    health = B.SocketHealth(client, clock=lambda: 0.0, timeout_s=300)
    assert health.check() is False


def test_pong_change_refreshes_but_same_value_does_not():
    now = [0.0]
    client = make_client(pong=1.0)
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    now[0] = 100
    client.current_session.last_ping_pong_time = 2.0
    assert health.check() is True  # a changed pong is activity
    now[0] = 399
    assert health.check() is True  # still within the window since the change
    now[0] = 400
    assert health.check() is False  # the same unchanged value cannot buy more time


def test_replacement_session_with_same_old_pong_is_not_fresh_activity():
    now = [0.0]
    client = make_client(pong=5.0)
    health = B.SocketHealth(client, clock=lambda: now[0], timeout_s=300)
    now[0] = 50
    assert health.check() is True  # first observation of 5.0 counts once
    now[0] = 400
    client.current_session = SimpleNamespace(last_ping_pong_time=5.0)  # a new session, same stale timestamp
    assert health.check() is False, "an old timestamp on a replaced session must not look fresh"


def test_check_never_reconnects_itself():
    client = make_client(connected=False)
    health = B.SocketHealth(client, clock=lambda: 0.0, timeout_s=300)
    calls = []
    client.connect_to_new_endpoint = lambda: calls.append(1)
    health.check()
    assert calls == []


# ----------------------------------------------------------------------------------------------- run_socket_mode

class FakeHandler:
    def __init__(self, client):
        self.client = client
        self.connected = False

    def connect(self):
        self.connected = True

    def start(self):
        raise AssertionError("run_socket_mode must never call handler.start()")


def test_run_socket_mode_connects_once_and_wires_the_raw_listener():
    client = make_client(connected=True)
    handler = FakeHandler(client)
    stop = threading.Event()
    threading.Timer(0.02, stop.set).start()
    B.run_socket_mode(handler, stop, clock=time.monotonic, poll_s=0.001, reconnect_timeout_s=0.05)
    assert handler.connected is True
    assert client.socket_mode_request_listeners, "the receipt listener must be installed"
    assert client.auto_reconnect_enabled is False and client.default_auto_reconnect_enabled is False


def test_run_socket_mode_raises_on_disconnected_after_reconnect():
    client = make_client(connected=False)
    client.connect_to_new_endpoint = lambda: None  # returns without reconnecting
    handler = FakeHandler(client)
    with pytest.raises(Exception):
        B.run_socket_mode(handler, threading.Event(), clock=time.monotonic, poll_s=0.001, reconnect_timeout_s=0.05)


def test_run_socket_mode_raises_on_reconnect_timeout():
    client = make_client(connected=False)
    release = threading.Event()

    def slow_reconnect():
        release.wait(2)
    client.connect_to_new_endpoint = slow_reconnect
    handler = FakeHandler(client)
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            B.run_socket_mode(handler, threading.Event(), clock=time.monotonic, poll_s=0.001, reconnect_timeout_s=0.05)
    finally:
        release.set()
    assert time.monotonic() - started < 0.5


def test_run_socket_mode_raises_on_reconnect_exception():
    client = make_client(connected=False)
    client.connect_to_new_endpoint = lambda: (_ for _ in ()).throw(RuntimeError("fixture"))
    handler = FakeHandler(client)
    with pytest.raises(RuntimeError, match="fixture"):
        B.run_socket_mode(handler, threading.Event(), clock=time.monotonic, poll_s=0.001, reconnect_timeout_s=0.05)


def test_run_socket_mode_watchdog_timeout_recovers_and_keeps_monitoring():
    now = [0.0]
    client = make_client(connected=True)
    handler = FakeHandler(client)
    stop = threading.Event()
    went_stale = []

    def clock():
        return now[0]
    # the poll loop checks health every poll_s "seconds" of our fake clock; drive it externally
    attempts = []

    def flaky_reconnect():
        attempts.append(1)
        client.is_connected = lambda: True
    client.connect_to_new_endpoint = flaky_reconnect

    def driver():
        time.sleep(0.01)
        now[0] = 301  # beyond the 300s timeout, forcing a reconnect attempt
        client.is_connected = lambda: False
        time.sleep(0.05)
        stop.set()
    threading.Thread(target=driver, daemon=True).start()
    B.run_socket_mode(handler, stop, clock=clock, poll_s=0.01, reconnect_timeout_s=0.5)
    assert attempts, "a stale (but connected) channel must trigger a recovery attempt"
