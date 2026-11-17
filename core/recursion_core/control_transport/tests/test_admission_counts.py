"""Connection counts are exact ints, and the refusal history is bounded.

The red tests of the s7 triage of the transport sweep (sol lists), entries D08 and D09, from
``sweeps/20261115/s7-triage-sol-lists-claude-opus-5-5-evidence/patches/transport/D0{8,9}-red.patch``;
their bodies are kept as written there, with one test of this draft's own after them.
All filesystem writes stay beneath TMPDIR.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

from recursion_core import control_transport as ct
from recursion_core.control_transport import unix


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
        self.addCleanup(srv.close)
        srv.start()
        return srv


class CountTests(ScratchCase):
    def test_f02_connection_caps_reject_fractional_or_nonfinite_counts(self):
        """s7 D08: 1.5, NaN and infinity were taken as connection counts."""
        for option in ("max_connections", "reserved_connections"):
            for value in (1.5, float("nan"), float("inf")):
                with self.subTest(option=option, value=value):
                    kwargs = {option: value, "priority": lambda request: True}
                    # ValueError is already the constructor's convention for bad counts.
                    with self.assertRaises((ValueError, ct.TransportRefused)):
                        ct.UnixControlServer(self.directory, ct.with_ping_health(), **kwargs)

    def test_a_bool_or_text_is_not_a_count_and_an_int_still_is(self):
        for option in ("max_connections", "reserved_connections"):
            for value in (True, "2", None, 2.0):
                with self.subTest(option=option, value=repr(value)):
                    with self.assertRaises(ValueError):
                        ct.UnixControlServer(self.directory, ct.with_ping_health(),
                                             **{option: value, "priority": lambda request: True})
        ct.UnixControlServer(self.directory, ct.with_ping_health(), max_connections=2,
                             reserved_connections=1, priority=lambda request: True)


class RefusalHistoryTests(ScratchCase):
    def test_f05_refusal_history_has_bounded_retention(self):
        """s7 D09: every admission-refused peer was appended to a list that was never pruned."""
        # Proposed retention policy: keep at most the most recent 128 reasons.
        srv = self.server(admit=lambda peer: False)
        for _ in range(160):
            with ct.UnixControlClient(srv.path, timeout_s=1) as client:
                with self.assertRaises(ct.PeerNotAdmitted):
                    client.request({"op": "ping"})
        self.assertLessEqual(len(srv.refused), 128)
        self.assertEqual(srv.refused[-1], "not_admitted")
        self.assertIsInstance(srv.refused, list)
        self.assertEqual(len(srv.refused), unix.REFUSED_KEPT)


if __name__ == "__main__":
    unittest.main()
