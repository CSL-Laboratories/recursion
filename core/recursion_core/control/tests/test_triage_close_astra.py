"""Witness that shutting the operator-read server waits for the source call.

Only the operator half of the sweep witness is here. The control-transport half
and the other cases in that patch belong to other drafts.
"""

import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from recursion_core.control.operator_reads import OperatorReads
from recursion_core.control import operator_reads_socket as ops


def observation(name, **values):
    print('OBSERVATION ' + json.dumps({'case': name, **values}, sort_keys=True), flush=True)


class SweepRegressions(unittest.TestCase):
    def test_F1_operator_close_waits_for_source(self):
        entered, release, finished, close_done = [threading.Event() for _ in range(4)]
        close_errors = []
        with tempfile.TemporaryDirectory(prefix='s2-close-') as directory:
            marker = Path(directory) / 'callback-finished.txt'

            def callback(*args):
                entered.set()
                if not release.wait(12):
                    raise RuntimeError('bounded callback wait expired')
                marker.write_text('ordinary callback completed', encoding='utf-8')
                finished.set()
                return {'venue': 'paper', 'rows': []}

            server = ops.OperatorReadsServer(
                OperatorReads({'positions': callback}),
                str(Path(directory) / 'read.sock'))
            message = b'{"method":"operator.positions.inspect","params":{}}\n'
            close_observation_s = 6.0  # shipped per-worker join is five seconds
            server.start()
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(2)
            closer = None
            try:
                conn.connect(server.path)
                conn.sendall(message)
                self.assertTrue(entered.wait(2), 'callback did not enter')

                def close():
                    try:
                        server.close()
                    except BaseException as exc:
                        close_errors.append(type(exc).__name__)
                    finally:
                        close_done.set()

                closer = threading.Thread(target=close, daemon=True)
                closer.start()
                returned_before_release = close_done.wait(close_observation_s)
                marker_at_return = marker.exists()
                release.set()
                self.assertTrue(finished.wait(2), 'callback did not finish after release')
                closer.join(2)
                self.assertFalse(closer.is_alive(), 'bounded closer did not finish')
                observation('F1_operator',
                            close_returned_before_callback_release=returned_before_release,
                            marker_at_close_return=marker_at_return,
                            marker_after_release=marker.exists(), close_errors=close_errors)
                self.assertEqual(close_errors, [])
                self.assertFalse(returned_before_release,
                                 'close returned while its callback could still write')
            finally:
                release.set()
                conn.close()
                if closer is not None:
                    closer.join(2)
                server.close()
                finished.wait(1)


if __name__ == '__main__':
    unittest.main()
