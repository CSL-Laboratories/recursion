"""Witness that shutting the operator-read server waits for the source call.

Only the operator half of the sweep witness is here. The control-transport half
and the other cases in that patch belong to other drafts.
"""

import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from recursion_core.control.operator_reads import OperatorReads
from recursion_core.control import operator_reads_socket as ops


def observation(name, **values):
    print('OBSERVATION ' + json.dumps({'case': name, **values}, sort_keys=True), flush=True)


def _owner_socket_can_serve():
    """The same names ``OperatorReadsServer.start`` requires before it will bind.

    ``start`` raises ``PlatformNotSupported`` when any of them is missing. The simulated
    Windows child sets ``os.name`` to ``nt`` and drops ``AF_UNIX``, ``SO_PEERCRED`` and
    ``fcntl``, so that call refuses there. ``os.getuid`` goes with them.
    """

    return (
        ops._AF_UNIX is not None
        and ops._SO_PEERCRED is not None
        and ops.fcntl is not None
        and getattr(os, 'getuid', None) is not None
    )


class _MemoryConn:
    """One request line and the methods the server calls on a live connection.

    No ``AF_UNIX`` and no peer-credential call: those names are absent when ``os.name`` is ``nt``.
    """

    def __init__(self, payload):
        self._payload = payload
        self._sent = False

    def settimeout(self, _value):
        return None

    def recv(self, _maximum):
        if self._sent:
            return b''
        self._sent = True
        return self._payload

    def sendall(self, _data):
        return None

    def shutdown(self, _how):
        return None

    def close(self):
        return None


def _open_source_call(server, message):
    """Put one inspect request in front of the source callback.

    Where the owner socket can be bound, this is the POSIX path the witness already had.
    Where ``start`` would refuse, the callback still runs on the real ``_serve`` worker,
    which is the worker ``server`` shutdown joins.
    """

    if _owner_socket_can_serve():
        server.start()
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(2)
        conn.connect(server.path)
        conn.sendall(message)
        return conn
    conn = _MemoryConn(message)
    if not server._slots.acquire(blocking=False):
        raise AssertionError('the server had no free slot for the source call')
    worker = threading.Thread(
        target=server._serve, args=(conn,), name='operator-reads-conn', daemon=True)
    with server._lock:
        server._conns.add(conn)
        server._workers = [item for item in server._workers if item.is_alive()] + [worker]
    worker.start()
    return conn


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
            closer = None
            conn = None
            try:
                conn = _open_source_call(server, message)
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
                if conn is not None:
                    conn.close()
                if closer is not None:
                    closer.join(2)
                server.close()
                finished.wait(1)


if __name__ == '__main__':
    unittest.main()
