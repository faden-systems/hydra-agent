"""loops/b11.md requirement 7 (l): `hydra compact` with and without a live loop prints exactly one outcome line
and never a success-looking line for a held or unverified outcome."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from b11_helpers import S, config, event, fakes, home, make, session_id
from conftest import MANAGER

HYDRA = os.path.join(MANAGER, "hydra")


class LiveLoop:
    def __init__(self, sup, h):
        self.sup, self.h, self.stop = sup, h, threading.Event()
        Path(h, "logs", "supervisor.pid").write_text(str(os.getpid()))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                self.sup.run_once()
            except Exception as e:  # noqa: BLE001
                print("live loop tick error:", e)
            time.sleep(0.2)

    def close(self):
        self.stop.set(); self.thread.join(5)
        Path(self.h, "logs", "supervisor.pid").unlink(missing_ok=True)


def compact(h, timeout=60):
    r = subprocess.run([sys.executable, HYDRA, "compact"], env={**os.environ, "HYDRA_HOME": h, "HYDRA_COMPACT_TIMEOUT": str(timeout)},
                       capture_output=True, text=True, timeout=timeout + 30)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def test_l_without_a_loop():
    h = home()
    rc, out = compact(h, 5)
    assert rc == 0 and Path(h, "COMPACT").exists(), out
    h2 = home(); config(h2, rollover_enabled=False)
    rc, out = compact(h2, 5)
    assert rc == 1 and "compaction held:" in out and not Path(h2, "COMPACT").exists()


def test_l_live_loop_verified_rollover_exit_0():
    h, d = home(), fakes(context=940000, context_after=90000)
    sup = make(h, d)
    event(h, "A"); sup.run_once()
    loop = LiveLoop(sup, h)
    try:
        rc, out = compact(h)
    finally:
        loop.close()
    assert rc == 0 and "compacted at " in out and session_id(h) != "sess-test", out


def test_l_live_loop_held_failed_and_unverified_exit_1():
    h, d = home(), fakes(context=940000, context_after=90000)
    config(h, rollover_enabled=False)
    sup = make(h, d)
    event(h, "A"); sup.run_once()
    loop = LiveLoop(sup, h)
    try:
        rc, out = compact(h)
    finally:
        loop.close()
    assert rc == 1 and "compaction held:" in out and "compacted at" not in out, out
    h2, d2 = home(), fakes(context=940000, context_after=90000, fail_compact=True)
    sup2 = make(h2, d2)
    event(h2, "A"); sup2.run_once()
    loop = LiveLoop(sup2, h2)
    try:
        rc, out = compact(h2)
    finally:
        loop.close()
    assert rc == 1 and "compaction failed at" in out and "compacted at" not in out, out
    h3, d3 = home(), fakes()
    sup3 = make(h3, d3)
    S.write_text(S.compaction_state_path(h3), json.dumps({"last": {"before_tokens": 940000, "after_tokens": None, "at": "2026-10-08T09:00:00Z", "ok": True, "reason": "tokens"}}))

    def seeded():
        if Path(h3, "COMPACT").exists():
            Path(h3, "COMPACT").unlink()
            S.write_text(S.compaction_state_path(h3), json.dumps({"last": {"before_tokens": 940000, "after_tokens": None, "at": "2026-10-08T12:00:00Z", "ok": None, "reason": "forced", "error": "rollover left no session id"}}))
            return True
        return False
    sup3.run_once = seeded
    loop = LiveLoop(sup3, h3)
    try:
        rc, out = compact(h3)
    finally:
        loop.close()
    assert rc == 1 and "compaction unverified at" in out and "compacted at" not in out, out
