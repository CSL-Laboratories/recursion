"""A failed start gives back what it took, and close() drains before it gives up the path and the lock.

The red tests of the s7 triage of the transport sweep (sol lists), entries D04, D05 and D06, from
``sweeps/20261115/s7-triage-sol-lists-claude-opus-5-5-evidence/patches/transport/D0{4,5,6}-red.patch``;
their bodies are kept as written there, in one module. All filesystem writes stay beneath TMPDIR.
"""

from __future__ import annotations

import errno
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from recursion_core import control_transport as ct
from recursion_core.control_transport import framing, unix


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux is the tested transport path")
class ScratchCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="s7-", dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.directory = os.path.join(self.tmp.name, "run")
        self.worker_errors = []
        old_hook = threading.excepthook
        threading.excepthook = lambda args: self.worker_errors.append(args.exc_type.__name__)
        self.addCleanup(setattr, threading, "excepthook", old_hook)

    def server(self, handler=None, **kwargs):
        srv = ct.UnixControlServer(self.directory, handler or ct.with_ping_health(), **kwargs)
        self.addCleanup(self.force_close, srv)
        srv.start()
        return srv

    @staticmethod
    def force_close(srv):
        # Cleanup only: before this change a failed start() could keep an unstarted thread.
        if srv._thread is not None and srv._thread.ident is None:
            srv._thread = None
        srv.close()


class StartRollbackTests(ScratchCase):
    def test_f03_socket_allocation_failure_releases_the_flock_fd(self):
        """s7 D04: socket.socket() failing after the lock is taken leaked the fd that holds the flock."""
        srv = ct.UnixControlServer(self.directory, ct.with_ping_health())
        acquired = []
        real_acquire = unix._acquire_lock

        def acquire(path):
            fd = real_acquire(path)
            acquired.append(fd)
            return fd

        def close_if_open(fd):
            try:
                os.close(fd)
            except OSError:
                pass

        with mock.patch.object(unix, "_acquire_lock", side_effect=acquire), \
             mock.patch.object(unix.socket, "socket", side_effect=OSError(errno.EMFILE, "injected fd exhaustion")):
            with self.assertRaises(OSError):
                srv.start()
        self.addCleanup(close_if_open, acquired[0])
        with self.assertRaises(OSError, msg="start leaked the fd that still holds flock"):
            os.fstat(acquired[0])

    def test_f03_accept_thread_start_failure_rolls_back_the_listener(self):
        """s7 D05: an accept thread that cannot start left the path, the listener and the lock in place."""
        srv = ct.UnixControlServer(self.directory, ct.with_ping_health())
        self.addCleanup(self.force_close, srv)
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("injected thread exhaustion")):
            with self.assertRaises(RuntimeError):
                srv.start()
        self.assertFalse(os.path.exists(srv.path), "failed start left a live listener and held flock")
        # This must also be safe if the caller follows a failed start with close.
        srv.close()

    def test_a_failed_accept_thread_start_leaves_the_lock_free_for_the_next_server(self):
        """s7 D05, the lock half: after the rolled-back start, a fresh server on the same path starts and serves."""
        srv = ct.UnixControlServer(self.directory, ct.with_ping_health())
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("injected thread exhaustion")):
            with self.assertRaises(RuntimeError):
                srv.start()
        nxt = self.server()
        with ct.UnixControlClient(nxt.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        self.assertTrue(os.path.exists(nxt.path))

    def test_accept_loop_returns_when_the_listener_is_already_cleared(self):
        """The accept thread's first read can see a listener the rollback already cleared.

        An assertion there kills the thread. Returning leaves the thread with no exception,
        including when the assertion would have been skipped and the next use of None would raise.
        """
        srv = ct.UnixControlServer(self.directory, ct.with_ping_health())
        self.addCleanup(self.force_close, srv)
        try:
            srv._accept_loop()
        except Exception as exc:
            self.fail(f"accept loop raised {type(exc).__name__} when the listener was already cleared")

    def test_an_interrupted_start_stops_the_loop_before_clearing_the_listener(self):
        """Rollback must mark the loop stopped before it clears the listener the thread reads."""
        seen = {}

        class Watched(ct.UnixControlServer):
            def __setattr__(self, name, value):
                # Assignment looks up __setattr__ on the class, so the watch has to live here.
                if name == "_sock" and value is None and getattr(self, "_armed", False) and "stop_first" not in seen:
                    seen["stop_first"] = self._stop.is_set()
                object.__setattr__(self, name, value)

        srv = Watched(self.directory, ct.with_ping_health())
        self.addCleanup(self.force_close, srv)
        srv._armed = True

        def pretend(thread):
            thread._ident = 1
            raise KeyboardInterrupt("injected once the accept thread has begun")

        with mock.patch.object(threading.Thread, "start", pretend):
            with self.assertRaises(KeyboardInterrupt):
                srv.start()
        self.assertIs(seen.get("stop_first"), True)


class CloseDrainsTests(ScratchCase):
    def test_close_retains_ownership_while_handler_can_still_write(self):
        """s7 D06: close() returned after a timed join and gave up the lock while a handler could still write."""
        entered, release, finished, closed = (threading.Event() for _ in range(4))
        marker = Path(self.tmp.name) / "late-handler-marker"
        errors = []

        def handler(message, connection):
            entered.set()
            if not release.wait(12):
                raise RuntimeError("test release watchdog")
            marker.write_text("finished", encoding="utf-8")
            finished.set()
            return {"ok": True}

        srv = self.server(handler, idle_timeout_s=0.01)

        def close():
            try:
                srv.close()
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                closed.set()

        closer = threading.Thread(target=close, daemon=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(2)
            raw.connect(srv.path)
            raw.sendall(framing.encode_frame({"op": "ordinary"}))
            self.assertTrue(entered.wait(2))
            closer.start()
            try:
                returned_early = closed.wait(6)
                marker_at_return = marker.exists()
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
                closer.join(3)
            self.assertFalse(closer.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(marker.exists())
            self.assertFalse(returned_early and not marker_at_return,
                             "close returned, released its lock, then the handler wrote")

    def test_close_keeps_the_path_until_the_handler_has_returned(self):
        """s7 D06, the ownership half: while close() waits on a handler, the socket path and the lock stay held."""
        entered, release = threading.Event(), threading.Event()

        def handler(message, connection):
            entered.set()
            release.wait(10)
            return {"ok": True}

        srv = self.server(handler, idle_timeout_s=0.01)
        closer = threading.Thread(target=srv.close, daemon=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(2)
            raw.connect(srv.path)
            raw.sendall(framing.encode_frame({"op": "ordinary"}))
            self.assertTrue(entered.wait(2))
            closer.start()
            closer.join(1.5)
            try:
                self.assertTrue(closer.is_alive(), "close() returned while a handler was still running")
                self.assertTrue(os.path.exists(srv.path))
                rival = ct.UnixControlServer(self.directory, ct.with_ping_health())
                with self.assertRaises(Exception):
                    rival.start()
            finally:
                release.set()
                closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertFalse(os.path.exists(srv.path))

    def test_shutdown_bounds_each_join(self):
        """The accept thread and a handler share one shutdown deadline, not a fresh wait each."""
        seen = []
        real_join = threading.Thread.join
        entered = threading.Event()
        release = threading.Event()

        def record(thread, timeout=None):
            seen.append(timeout)
            if len(seen) >= 2:
                release.set()
            return real_join(thread, timeout)

        def handler(message, connection):
            entered.set()
            release.wait(5)
            return {"ok": True}

        srv = self.server(handler, idle_timeout_s=0.01)
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(raw.close)
        raw.settimeout(2)
        raw.connect(srv.path)
        raw.sendall(framing.encode_frame({"op": "ordinary"}))
        self.assertTrue(entered.wait(2))
        closer = threading.Thread(target=srv.close, daemon=True)
        with mock.patch.object(threading.Thread, "join", record):
            closer.start()
            deadline = time.monotonic() + 5
            while closer.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertFalse(closer.is_alive())
        self.assertGreaterEqual(len(seen), 2, seen)
        # One deadline: the first join is handed the whole budget, and a later join only what is left.
        budget = max(unix.SHUTDOWN_DRAIN_BOUND_S, srv._idle_timeout_s + 1.0)
        self.assertAlmostEqual(seen[0], budget, delta=0.5)
        self.assertLess(seen[-1], seen[0], seen)
        self.assertTrue(
            all(item is not None and 0.0 <= item <= budget + 1e-6 for item in seen),
            seen,
        )

    def test_two_handlers_may_shut_the_server_down_together(self):
        """Two handlers that both shut the server down both return, and a fresh server can take the path.

        Each would join the other. An unbounded join never returns, so this test stops waiting and fails.
        """
        arrived = threading.Barrier(2)
        done: list = []
        errors: list = []
        holder: dict = {}

        def handler(message, connection):
            try:
                arrived.wait(5)
                holder["srv"].close()
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                done.append(1)

        srv = ct.UnixControlServer(self.directory, handler, idle_timeout_s=0.01)
        holder["srv"] = srv

        def abandon():
            srv._stop.set()
            with srv._lock:
                srv._workers.clear()
                srv._thread = None
            try:
                srv.close()
            except Exception:
                pass

        self.addCleanup(abandon)
        srv.start()
        raws = []
        try:
            for _ in range(2):
                raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                raw.settimeout(2)
                raw.connect(srv.path)
                raw.sendall(framing.encode_frame({"op": "ordinary"}))
                raws.append(raw)
            deadline = time.monotonic() + unix.SHUTDOWN_DRAIN_BOUND_S + 5.0
            while len(done) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            for raw in raws:
                raw.close()
        self.assertEqual(errors, [])
        self.assertEqual(len(done), 2, "both shutdowns must return within the drain bound")
        self.assertFalse(os.path.exists(srv.path))
        self.assertIsNone(srv._sock)
        self.assertIsNone(srv._lock_fd)
        nxt = self.server()
        with ct.UnixControlClient(nxt.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")

    def test_default_idle_drain_budget_matches_the_idle_drain(self):
        """At the default idle timeout the shutdown budget is idle_timeout_s + 1, not the short bound.

        The daemon builds the server with no idle timeout. Master waits that long for each handler.
        """
        seen = []
        entered = threading.Event()

        def record(thread, timeout=None):
            seen.append(timeout)
            return None

        def handler(message, connection):
            entered.set()
            return {"ok": True}

        srv = self.server(handler)
        self.assertEqual(srv._idle_timeout_s, unix.IDLE_TIMEOUT_S)
        budget = max(unix.SHUTDOWN_DRAIN_BOUND_S, srv._idle_timeout_s + 1.0)
        self.assertGreater(budget, unix.SHUTDOWN_DRAIN_BOUND_S)
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(raw.close)
        raw.settimeout(2)
        raw.connect(srv.path)
        raw.sendall(framing.encode_frame({"op": "ordinary"}))
        self.assertTrue(entered.wait(2))
        with mock.patch.object(threading.Thread, "join", record):
            srv.close()
        self.assertGreaterEqual(len(seen), 1, seen)
        self.assertAlmostEqual(seen[0], budget, delta=0.5)
        self.assertTrue(all(item is not None and item > unix.SHUTDOWN_DRAIN_BOUND_S for item in seen), seen)

    def test_default_idle_drain_keeps_the_path_past_the_short_bound(self):
        """A handler still running a little past the short bound keeps the path at the default idle timeout."""
        entered = threading.Event()
        release = threading.Event()
        begun = threading.Event()

        def handler(message, connection):
            entered.set()
            release.wait(60)
            return {"ok": True}

        def shutdown():
            begun.set()
            srv.close()

        srv = self.server(handler)
        closer = threading.Thread(target=shutdown, daemon=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(2)
            raw.connect(srv.path)
            raw.sendall(framing.encode_frame({"op": "ordinary"}))
            self.assertTrue(entered.wait(2))
            closer.start()
            self.assertTrue(begun.wait(5), "shutdown did not start")
            time.sleep(unix.SHUTDOWN_DRAIN_BOUND_S + 2.0)
            try:
                self.assertTrue(closer.is_alive(), "shutdown gave the path back inside the default idle drain")
                self.assertTrue(os.path.exists(srv.path))
                rival = ct.UnixControlServer(self.directory, ct.with_ping_health())
                with self.assertRaises(Exception):
                    rival.start()
            finally:
                release.set()
                closer.join(srv._idle_timeout_s + 5)
        self.assertFalse(closer.is_alive())

    def test_one_deadline_covers_every_wedged_handler(self):
        """Wedged handlers share one shutdown budget. A fresh wait per handler would stack it."""
        hold = threading.Event()
        ready = threading.Event()
        count = {"n": 0}
        lock = threading.Lock()

        def handler(message, connection):
            with lock:
                count["n"] += 1
                if count["n"] >= 2:
                    ready.set()
            hold.wait(90)
            return {"ok": True}

        srv = self.server(handler, idle_timeout_s=1.0)
        budget = max(unix.SHUTDOWN_DRAIN_BOUND_S, srv._idle_timeout_s + 1.0)
        self.assertEqual(budget, unix.SHUTDOWN_DRAIN_BOUND_S)
        raws = []
        closer = None
        try:
            for _ in range(2):
                raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                raw.settimeout(2)
                raw.connect(srv.path)
                raw.sendall(framing.encode_frame({"op": "ordinary"}))
                raws.append(raw)
            self.assertTrue(ready.wait(5), "both handlers did not start")
            started = time.monotonic()
            closer = threading.Thread(target=srv.close, daemon=True)
            closer.start()
            closer.join(budget * 2 + 5)
            elapsed = time.monotonic() - started
        finally:
            hold.set()
            for raw in raws:
                raw.close()
        self.assertIsNotNone(closer)
        self.assertFalse(closer.is_alive(), "shutdown did not return")
        self.assertLess(elapsed, budget + 4.0, f"shutdown took {elapsed:.2f}s; one budget is {budget:.0f}s")
        self.assertGreater(elapsed, budget - 2.0, f"shutdown returned in {elapsed:.2f}s, short of the one budget")

    def test_accept_loop_returns_when_the_listener_is_no_longer_its_socket(self):
        """An accept loop whose listener was cleared without the stop flag returns, instead of spinning."""
        srv = self.server()
        thread = srv._thread
        sock = srv._sock
        self.assertIsNotNone(thread)
        self.assertIsNotNone(sock)
        srv._sock = None
        sock.close()
        thread.join(1.5)
        self.assertFalse(
            thread.is_alive(),
            "accept loop kept spinning after its listener was replaced",
        )


if __name__ == "__main__":
    unittest.main()
