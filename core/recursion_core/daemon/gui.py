"""Daemon-owned loopback socket for the shared GUI. No remote bind or auth bypass.

The control transport and device door are injected by the control-API adapter.
Until that adapter is attached, authentication refuses every request; the fallback
transport reports unavailable, never an invented empty route inventory.
"""
from collections.abc import Mapping
from threading import Thread
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


class _Handler(WSGIRequestHandler):
    def handle(self):
        self.connection.settimeout(2)
        super().handle()

    def log_message(self, *_):
        # Request paths may contain identifiers. Do not log them or device headers.
        pass


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
        server = WSGIServer(("127.0.0.1", self.port), _Handler)
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
