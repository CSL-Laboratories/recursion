"""A stand-in daemon on a real owner-only control socket, for the desktop's tests.

``FakeDaemon`` serves the daemon's wire with the core's own server
(``control_transport.UnixControlServer``): the same 0700 directory rule, the same peer uid check
at accept, the same frames, the same ``busy`` and ``scope_refused`` transport frames, and
``ping``/``health`` answered by the core's ``with_ping_health``. Only the control API behind it is
scripted: each ``{"op": "control", ...}`` request is answered from ``answers`` (a method name to an
answer, or to a function of the params), and every frame that reached the handler is recorded, in
order, in ``frames``.

``SilentDaemon`` reads one request and closes without answering, so a client sees a request that
was written and never answered.

Neither starts a run, a unit or a model. Both put their socket where they are told; the tests give
them a fresh 0700 directory under ``TMPDIR``.
"""

from __future__ import annotations

import os
import socket
import stat
import threading
from collections.abc import Callable, Mapping
from typing import Any

from recursion_core.control_transport import UnixControlServer
from recursion_core.control_transport.framing import read_frame
from recursion_core.control_transport.handlers import with_ping_health

Answer = Mapping[str, Any] | Callable[[dict], Mapping[str, Any]]

# What the daemon answers for a method it does not have (control/api.py), as SPINE's captured fixture c17 shows it.
def method_unknown(method: str) -> dict:
    return {"code": "method_unknown", "detail": method}


class FakeDaemon:
    """The control socket with a scripted control API behind it. Use as a context manager."""

    def __init__(self, directory: str, answers: Mapping[str, Answer] | None = None, *, name: str = "control.sock",
                 refuse: str | None = None, **server_options: Any) -> None:
        """``refuse`` makes the handler refuse every control request with that code before the control API
        sees it, as the daemon refuses a peer inside a run unit (``scope_refused``)."""
        self.frames: list[dict] = []
        self._answers = dict(answers or {})
        self._refuse = refuse
        self._lock = threading.Lock()
        self.server = UnixControlServer(directory, self._record, name=name, **server_options)
        self._delegate = with_ping_health(self._control)

    @property
    def path(self) -> str:
        return self.server.path

    def requests(self) -> list[tuple[str, dict]]:
        """The (method, params) of every control frame, in order."""
        with self._lock:
            return [(f.get("method"), f.get("params")) for f in self.frames if f.get("op") == "control"]

    def _record(self, message: dict, conn) -> dict:
        with self._lock:
            self.frames.append(message)
        return self._delegate(message, conn)

    def _control(self, message: dict, conn) -> dict:
        # The checks DaemonControl.handle makes before its control API sees a request (daemon/control_adapter.py).
        if message.get("op") != "control":
            return {"ok": False, "error": {"code": "unknown_op", "op": message.get("op")}}
        method, params = message.get("method"), message.get("params")
        if not isinstance(method, str) or not isinstance(params, (dict, type(None))):
            return {"ok": False, "error": {"code": "request_invalid"}}
        if self._refuse is not None:
            return {"ok": False, "error": {"code": self._refuse}}
        answer = self._answers.get(method)
        if answer is None:
            return {"ok": True, "result": method_unknown(method)}
        result = answer(dict(params or {})) if callable(answer) else dict(answer)
        return {"ok": True, "result": result}

    def __enter__(self) -> FakeDaemon:
        self.server.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.close()


class SilentDaemon:
    """Accepts one connection, reads one frame, and closes without a reply."""

    def __init__(self, directory: str, *, name: str = "control.sock") -> None:
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise PermissionError(f"{directory} must be an owner-only directory")
        self.path = os.path.join(directory, name)
        self.received: list[dict | None] = []
        self.done = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._thread = threading.Thread(target=self._serve, name="silent-daemon", daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            self.done.set()
            return
        try:
            self.received.append(read_frame(conn))
        finally:
            conn.close()
            self.done.set()

    def __enter__(self) -> SilentDaemon:
        self._sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self._sock.listen(1)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # wakes an accept that nobody connected to
        except OSError:
            pass
        self._sock.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._thread.join()
