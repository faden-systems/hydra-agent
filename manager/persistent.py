#!/usr/bin/env python3
"""Child-process plumbing for the persistent Claude engine (loops/b8.md): spawn one long-lived `claude -p
--input-format stream-json --output-format stream-json` process, detect a startup failure before any user
request is sent, serialize successive turns over the same stdin/stdout pipes, and tear a live process down
either orderly (close stdin, wait) or by force (interrupt, bounded grace, SIGKILL the whole process group).

This module knows nothing about accounts, models, fallback policy or the engine file; `supervisor.Supervisor`
is the only caller, and `Supervisor.invoke` is the vendor boundary (loops/b8.md, F1). Nothing here talks to a
network or reads a credential; the caller's `env` carries whatever token is needed.
"""
import contextlib
import json
import os
import queue
import signal
import subprocess
import threading
import time


class StartupFailed(Exception):
    """A spawned process failed before producing the native startup record (requirement 8, loops/b8.md):
    distinguishable from a submitted-request failure, so the caller never replays a request into it."""

    def __init__(self, reason=""):
        super().__init__(reason)
        self.reason = reason


def _pid_alive(pid):
    return pid is not None and os.path.exists(f"/proc/{pid}")


class _LineReader:
    """One background thread per stream so the caller can wait on a deadline without blocking on readline;
    ('line', text) for each line, then exactly one ('eof', None) when the stream closes."""

    def __init__(self, stream):
        self._q = queue.Queue()
        self._t = threading.Thread(target=self._run, args=(stream,), daemon=True)
        self._t.start()

    def _run(self, stream):
        with contextlib.suppress(ValueError, OSError):
            for line in stream:
                self._q.put(("line", line))
        self._q.put(("eof", None))

    def get(self, deadline):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            return self._q.get(timeout=remaining)
        except queue.Empty:
            return ("timeout", None)

    def drain_text(self):
        """Whatever has already arrived, without waiting (a bounded diagnostic, never blocking cleanup)."""
        parts = []
        while True:
            try:
                kind, val = self._q.get_nowait()
            except queue.Empty:
                break
            if kind == "line":
                parts.append(val)
        return "".join(parts)


class Child:
    """One live persistent CLI process and its stream-json wire."""

    def __init__(self, popen):
        self.popen = popen
        self.pid = popen.pid
        self.stdout = _LineReader(popen.stdout)
        self.stderr = _LineReader(popen.stderr)

    def alive(self):
        try:
            rc = self.popen.poll()
        except OSError:  # already reaped by someone else (e.g. a supervisor restart raced a dead child)
            return False
        return rc is None and _pid_alive(self.pid)

    def send(self, text, timeout):
        """Write one stream-json user message and read lines until a terminal `result` record, EOF, or the
        deadline. Returns (lines, timed_out, crashed); at most one of the last two is true. A request that
        never gets a terminal result is never silently replayed by this call (requirement 1, loops/b8.md):
        the caller alone decides what a timeout or a crash means for the turn and the process."""
        envelope = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
        try:
            self.popen.stdin.write(envelope)
            self.popen.stdin.flush()
        except (BrokenPipeError, OSError):
            return [], False, True
        deadline = time.monotonic() + timeout
        collected = []
        while True:
            kind, val = self.stdout.get(deadline)
            if kind == "timeout":
                return collected, True, False
            if kind == "eof":
                return collected, False, True
            line = val.strip()
            if not line:
                continue
            collected.append(line)
            with contextlib.suppress(json.JSONDecodeError):
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("type") == "result":
                    return collected, False, False

    def close_orderly(self, timeout=2.0):
        """Close stdin and wait (bounded) for exit; `{"orderly": bool, "returncode": int|None}`. Forced
        TERM/KILL is the caller's job (`kill_tree`) only when this returns orderly: False."""
        with contextlib.suppress(Exception):
            self.popen.stdin.close()
        try:
            rc = self.popen.wait(timeout=timeout)
            return {"orderly": True, "returncode": rc}
        except subprocess.TimeoutExpired:
            return {"orderly": False, "returncode": None}

    def kill_tree(self, grace=0.6):
        """Interrupt, a bounded grace, then SIGKILL the whole process group (the child's own process group,
        requirement 3/PR45 2.5, loops/b8.md: a hung tool-call descendant is in the same group); waits for
        `/proc` to clear so the caller never reports a killed process as still running."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGINT)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and _pid_alive(self.pid):
            time.sleep(0.02)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGKILL)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and _pid_alive(self.pid):
            time.sleep(0.02)
        with contextlib.suppress(Exception):
            self.popen.wait(timeout=0.1)

    def diagnostic(self):
        return self.stderr.drain_text().strip()


def spawn(argv, env, cwd, startup_timeout):
    """Start `argv` in its own session/process group with piped stdio; read exactly one line from stdout
    within `startup_timeout` and require the native `system`-typed startup record before any request is sent
    (requirement 8, loops/b8.md: a startup failure must never carry a submitted request). Raises
    StartupFailed with a bounded diagnostic on EOF, a startup timeout, or an unexpected first line; the
    process is killed first in every failure case."""
    popen = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, bufsize=1, env=env, cwd=cwd, start_new_session=True)
    child = Child(popen)
    deadline = time.monotonic() + startup_timeout
    kind, val = child.stdout.get(deadline)
    if kind != "line":
        reason = "startup timed out" if kind == "timeout" else "process exited before producing output"
        child.kill_tree()
        raise StartupFailed((reason + ": " + child.diagnostic())[:1000])
    try:
        obj = json.loads(val.strip())
    except json.JSONDecodeError:
        obj = None
    if not (isinstance(obj, dict) and obj.get("type") == "system"):
        child.kill_tree()
        raise StartupFailed(("unexpected startup line: " + val.strip())[:1000])
    return child
