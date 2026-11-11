"""Daemon-owned loopback socket for the shared GUI. No remote bind or auth bypass.

The control transport and device door are injected by the control-API adapter
(``control_adapter.compose_control`` in the running daemon). Constructed without
them, authentication refuses every request and the fallback transport reports
unavailable, never an invented empty route inventory.

Any local uid can connect to 127.0.0.1, and the Host and token checks run only after the
headers arrive. So each connection gets its own thread, the request line and headers must
arrive within ``HEADER_DEADLINE_S`` of the accept however the bytes are spaced, and the
number of threads is bounded: when every one is taken, the connection that has been sending
headers longest is closed to make room (ADV1-3). One slow client cannot hold the page or
its Stop.
"""
from collections.abc import Mapping
import io
import socket
from socketserver import ThreadingMixIn
from threading import Lock, Thread
import time
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

from recursion_core.gui import compose
from recursion_core.gui.client import ControlClient, ControlError


def _unavailable(*_):
    raise ControlError("control_api_not_attached")


def _unpaired(_):
    return False


def _snapshot(plugin):
    """Materialise one plugin's panels and required subjects once.

    Validation at construction and the served application at bind must see the same
    declarations, so one-shot iterables (generators) are read exactly once here.
    """
    if not isinstance(plugin, Mapping):
        raise ValueError("plugin must be a mapping")
    snapshot = dict(plugin)
    if "panels" in snapshot:
        snapshot["panels"] = tuple(dict(p) if isinstance(p, Mapping) else p for p in snapshot["panels"])
    if "required" in snapshot:
        snapshot["required"] = {cls: tuple(subjects) for cls, subjects in snapshot["required"].items()}
    return snapshot


MAX_REQUEST_THREADS = 16
RECV_TIMEOUT_S = 2.0      # one read or write
HEADER_DEADLINE_S = 5.0   # the request line and every header, from the accept
BODY_DEADLINE_S = 10.0    # the rest of the request, once the headers are in


class _DeadlineReader(io.RawIOBase):
    """Socket reads that stop at a whole-request deadline, not only at a per-read timeout."""

    def __init__(self, sock, deadline):
        self._sock, self.deadline = sock, deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("request deadline")
        self._sock.settimeout(min(left, RECV_TIMEOUT_S))
        return self._sock.recv_into(buffer)


class _Handler(WSGIRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(RECV_TIMEOUT_S)
        self.rfile.close()
        self._reader = _DeadlineReader(self.connection, time.monotonic() + HEADER_DEADLINE_S)
        self.rfile = io.BufferedReader(self._reader)

    def handle(self):
        try:
            super().handle()
        except OSError:
            pass  # a slow or vanished client: its deadline, its timeout or the server closed the read

    def parse_request(self):
        parsed = super().parse_request()
        self._reader.deadline = time.monotonic() + BODY_DEADLINE_S
        self.server.headers_read(self.connection)
        return parsed

    def log_message(self, *_):
        # Request paths may contain identifiers. Do not log them or device headers.
        pass


class _Server(ThreadingMixIn, WSGIServer):
    """One daemon thread per connection, at most ``MAX_REQUEST_THREADS`` of them."""
    daemon_threads = True

    def __init__(self, *args):
        self._gate = Lock()
        self._reading = {}    # connections still sending headers, oldest first
        self._working = set()  # connections whose request is being answered
        super().__init__(*args)

    def process_request(self, request, client_address):
        evicted = None
        with self._gate:
            full = len(self._reading) + len(self._working) >= MAX_REQUEST_THREADS
            if full and self._reading:
                evicted = next(iter(self._reading))
                del self._reading[evicted]
                full = False
            if not full:
                self._reading[request] = None
        if evicted is not None:
            try:
                evicted.shutdown(socket.SHUT_RDWR)  # its thread sees the end of the read and leaves
            except OSError:
                pass
        if full:  # every thread is answering a request: refuse this one, never queue behind them
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._forget(request)
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._forget(request)

    def headers_read(self, request):
        with self._gate:
            if request in self._reading:
                del self._reading[request]
                self._working.add(request)

    def _forget(self, request):
        with self._gate:
            self._reading.pop(request, None)
            self._working.discard(request)


class LoopbackGUI:
    """Lifecycle belongs to the daemon; port=0 is available for local socket tests."""
    def __init__(self, client=None, door=None, *, plugins=(), port=8765):
        self.client = client if client is not None else ControlClient(_unavailable)
        self.door = door if door is not None else _unpaired
        self.plugins, self.port = tuple(_snapshot(p) for p in plugins), port
        self.server = self.thread = None
        # Validate plugin panels before any bind, so a bad registry fails at startup
        # even when the port is occupied and the bind is retried later. Both compose()
        # calls read the same snapshot, so generator-backed panels are not lost.
        compose(self.client, self.door, frozenset(), frozenset(), plugins=self.plugins)

    def __enter__(self):
        server = _Server(("127.0.0.1", self.port), _Handler)
        try:
            host = "127.0.0.1:%d" % server.server_port
            server.set_app(compose(self.client, self.door, {host}, {"http://" + host}, plugins=self.plugins))
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                            name="recursion-loopback-gui", daemon=True)
            thread.start()
        except BaseException:
            server.server_close()
            raise
        self.server, self.thread = server, thread
        return self

    @property
    def address(self):
        return self.server.server_address

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
