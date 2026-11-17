"""Witnesses for operator-read startup publication and deadline construction.

The assertions describe the intended behavior. They fail on the base revision.
"""

from __future__ import annotations

import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.control.operator_reads import OperatorReads
from recursion_core.control import operator_reads_socket as transport

CAN_SERVE = all((hasattr(socket, "AF_UNIX"), hasattr(socket, "SO_PEERCRED"), hasattr(os, "getuid")))


@unittest.skipUnless(CAN_SERVE, "Linux owner-only socket required")
class LifecycleRegressions(unittest.TestCase):
    def test_f03_close_during_start_does_not_leave_listener_or_path(self):
        # Five controlled schedules, not a stress test. The patch only pauses
        # startup; actual socket setup and lifecycle operations are unchanged.
        leaks = []
        for round_number in range(5):
            with tempfile.TemporaryDirectory(prefix="s2b-life-") as directory:
                path = str(Path(directory) / "reads.sock")
                server = transport.OperatorReadsServer(OperatorReads(), path)
                reached, proceed, closing, closed = (threading.Event() for _ in range(4))
                errors = queue.SimpleQueue()
                original = transport._clear_stale

                def pause_start(candidate):
                    original(candidate)
                    reached.set()
                    if not proceed.wait(3):
                        raise RuntimeError("witness scheduling timeout")

                def start():
                    try:
                        server.start()
                    except Exception as exc:
                        errors.put(type(exc).__name__)

                def close():
                    closing.set()
                    try:
                        server.close()
                    except Exception as exc:
                        errors.put(type(exc).__name__)
                    finally:
                        closed.set()

                starter = threading.Thread(target=start, daemon=True)
                closer = threading.Thread(target=close, daemon=True)
                try:
                    with mock.patch.object(transport, "_clear_stale", pause_start):
                        starter.start()
                        self.assertTrue(reached.wait(3), "startup gate not reached")
                        closer.start()
                        self.assertTrue(closing.wait(3), "close not invoked")
                        # A corrected close may wait for startup; never require it
                        # to finish while the test itself holds startup paused.
                        closed.wait(0.1)
                        proceed.set()
                        starter.join(3)
                        closer.join(3)
                    self.assertFalse(starter.is_alive() or closer.is_alive(), "lifecycle call did not finish")
                    if server._listener is not None or os.path.lexists(path):
                        leaks.append(round_number)
                finally:
                    proceed.set()
                    starter.join(3)
                    if closer.ident is not None:
                        closer.join(3)
                    server.close()
        self.assertEqual(leaks, [], "close returned but startup left resources in these rounds")


class InputRegressions(unittest.TestCase):
    def test_f08_nonfinite_server_deadlines_are_rejected_at_construction(self):
        for deadline in (float("nan"), float("inf")):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                transport.OperatorReadsServer(OperatorReads(), read_deadline_s=deadline)


if __name__ == "__main__":
    unittest.main()
