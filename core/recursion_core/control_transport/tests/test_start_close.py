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


if __name__ == "__main__":
    unittest.main()
