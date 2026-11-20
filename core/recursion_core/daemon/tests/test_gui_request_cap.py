"""Evicting a reader must not leave more live request threads than the cap."""
import contextlib
import socket
import threading
import unittest
from socketserver import threading as server_threading
from unittest import mock

from recursion_core.daemon import gui


class RequestThreadCapTests(unittest.TestCase):
    def test_eviction_must_not_spawn_more_live_request_threads_than_the_cap(self):
        # Four connections suffice: cap=2 plus reserve=1, with the old readers paused
        # at a legal scheduling point before they can observe socket shutdown.
        entered = [threading.Event() for _ in range(4)]
        release = threading.Event()
        index_lock = threading.Lock()
        count = 0

        class PausedHandler(gui._Handler):
            def setup(self):
                nonlocal count
                super().setup()
                with index_lock:
                    index = count
                    count += 1
                entered[index].set()
                release.wait(3)

        sockets, workers = [], []
        server = gui._Server(("127.0.0.1", 0), PausedHandler)
        server.set_app(lambda env, start: [])
        constructor = server_threading.Thread

        def track(*args, **kwargs):
            thread = constructor(*args, **kwargs)
            workers.append(thread)
            return thread

        try:
            with mock.patch.object(gui, "MAX_REQUEST_THREADS", 2), \
                    mock.patch.object(gui, "STOP_RESERVED_THREADS", 1), \
                    mock.patch.object(server_threading, "Thread", side_effect=track):
                for index in range(4):
                    left, right = socket.socketpair()
                    sockets.extend((left, right))
                    server.process_request(left, ("127.0.0.1", 1))
                    if index == 0:
                        self.assertTrue(entered[index].wait(2))
                    else:
                        # A corrected server may refuse a replacement until its evicted
                        # worker exits. Do not require an excess worker to be created.
                        entered[index].wait(0.1)
                live = sum(worker.is_alive() for worker in workers)
                self.assertLessEqual(live, 3, "evicted readers remain alive outside the tracked cap")
        finally:
            release.set()
            for sock in sockets:
                with contextlib.suppress(OSError):
                    sock.shutdown(2)
                sock.close()
            for worker in workers:
                worker.join(3)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
