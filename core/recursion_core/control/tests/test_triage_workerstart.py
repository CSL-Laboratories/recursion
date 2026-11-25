"""S3b F02: a connection worker whose start fails must not stay tracked."""

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from recursion_core.control import operator_reads_socket as T
from recursion_core.control.operator_reads import OperatorReads

POSITIONS = 'operator.positions.inspect'
CAN_SERVE = T._AF_UNIX is not None and T._SO_PEERCRED is not None and hasattr(os, 'getuid')


@unittest.skipUnless(CAN_SERVE, 'owner-only sockets need Linux peer credentials')
class SocketGapTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='s3b-socket-')
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / 'reads.sock')

    def test_worker_start_failure_restores_capacity_and_does_not_break_close(self):
        server = T.OperatorReadsServer(OperatorReads(), self.path, max_connections=1).start()
        real_start = threading.Thread.start

        def start(thread):
            if thread.name == 'operator-reads-conn':
                raise RuntimeError('synthetic-thread-start-failure')
            return real_start(thread)

        try:
            with mock.patch.object(threading.Thread, 'start', start):
                reply = T.OperatorReadsClient(self.path, timeout_s=2).call(POSITIONS)
            self.assertEqual(reply['code'], 'busy')
            restored = server._slots.acquire(blocking=False)
            if restored:
                server._slots.release()
            self.assertTrue(restored, 'failed worker start leaked the only slot')
            server.close()  # Must not join the Thread whose start failed.
            self.assertFalse(os.path.lexists(self.path))
        finally:
            try:
                server.close()
            except RuntimeError:
                pass  # The assertion above is the witness; this only bounds cleanup.


if __name__ == '__main__':
    unittest.main()
