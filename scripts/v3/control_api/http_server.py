"""Loopback-only HTTP transport for the Control API.

``POST /v1/rpc`` carries one JSON-RPC 2.0 request. ``GET /v1/events`` is the
observer stream as a long poll over ``events.poll``. Both paths go through
``ControlService.handle``, so authentication, scopes and tenant filtering are
decided in one place.

There is no remote path in this slice. The server binds only a loopback
address and refuses any other ``Host`` or any ``Origin`` header, so a web page
cannot reach it by DNS rebinding or cross-origin script. QR pairing and the E2E
relay (CSL-498) are not built here, and nothing here is reachable off-host.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from scripts.v3.control_api import errors as err
from scripts.v3.control_api.service import MAX_BODY_BYTES, ControlService

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})
REQUEST_TIMEOUT_S = 15.0
MAX_CONNECTIONS = 32
# Headers plus one capped body. A refusal must not read without a bound.
_REFUSE_HEADER_CAP = 8192
_REFUSE_IO_TIMEOUT_S = 1.0

_STATUS_FOR_CODE = {
    err.PARSE_ERROR: 400,
    err.INVALID_REQUEST: 400,
    err.INVALID_PARAMS: 400,
    err.UNAUTHENTICATED: 401,
    err.FORBIDDEN: 403,
    err.METHOD_NOT_FOUND: 404,
    err.TOO_LARGE: 413,
    err.INTERNAL_ERROR: 500,
}


class RemoteBindRefused(ValueError):
    """The requested bind address is not loopback."""


def status_for(response: dict[str, Any]) -> int:
    error = response.get("error")
    if not isinstance(error, dict):
        return 200
    return _STATUS_FOR_CODE.get(error.get("code"), 200)


def _close_socket(request: Any) -> None:
    try:
        request.close()
    except OSError:
        pass


def _recv_until(request: Any, size: int, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or size <= 0:
        return b""
    try:
        request.settimeout(remaining)
        chunk = request.recv(size)
    except OSError:
        return b""
    return chunk if chunk else b""


def _drain_refused_request(request: Any, deadline: float) -> None:
    """Read the refused request until it ends or the deadline passes.

    One socket timeout per ``recv`` is not a bound: a peer that sends a byte
    just before each timeout keeps the accept thread in this function. The
    deadline is absolute. The peer usually does not half-close, so this stops
    at the end of one capped body instead of waiting for EOF.
    """

    cap = _REFUSE_HEADER_CAP + MAX_BODY_BYTES
    buf = bytearray()
    while b"\r\n\r\n" not in buf and len(buf) < cap:
        chunk = _recv_until(request, min(4096, cap - len(buf)), deadline)
        if not chunk:
            return
        buf += chunk
    head, sep, rest = bytes(buf).partition(b"\r\n\r\n")
    if not sep:
        return
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.lower() == b"content-length":
            try:
                length = int(value.strip() or b"0")
            except ValueError:
                return
            break
    if length < 0:
        return
    length = min(length, MAX_BODY_BYTES)
    have = len(rest)
    while have < length:
        chunk = _recv_until(request, min(4096, length - have), deadline)
        if not chunk:
            return
        have += len(chunk)


def _refuse_over_limit(request: Any) -> None:
    """Send 503, then finish the write side before reading and closing.

    ``close`` with unread data aborts the socket. On Windows that RST can
    arrive at the client before the 503, so the caller sees a reset instead
    of ``too_many_connections``.
    """

    payload = b'{"error":"too_many_connections"}'
    message = (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n"
        b"Connection: close\r\n"
        b"Cache-Control: no-store\r\n\r\n" + payload
    )
    deadline = time.monotonic() + _REFUSE_IO_TIMEOUT_S
    try:
        request.settimeout(_REFUSE_IO_TIMEOUT_S)
    except OSError:
        pass
    try:
        request.sendall(message)
    except OSError:
        _close_socket(request)
        return
    try:
        request.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    _drain_refused_request(request, deadline)
    _close_socket(request)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # ``HTTPServer`` sets this. Leave it off unless ``server_bind`` opts in.
    allow_reuse_address = False
    service: ControlService
    allowed_hosts: frozenset[str]
    max_connections: int = MAX_CONNECTIONS

    def server_bind(self) -> None:
        # POSIX ``SO_REUSEADDR`` only permits a bind during ``TIME_WAIT``.
        # On Windows the same option lets a second socket bind a specific
        # address that is still listening (same user; Winsock, Server 2003+),
        # so ``serve-fake`` on an occupied port would run instead of failing.
        # A default specific bind returns ``WSAEADDRINUSE``.
        self.allow_reuse_address = os.name != "nt"
        super().server_bind()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._active = 0
        self._active_lock = threading.Lock()

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._active_lock:
            if self._active >= self.max_connections:
                refuse = True
            else:
                self._active += 1
                refuse = False
        if refuse:
            _refuse_over_limit(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:
                self._active -= 1
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active -= 1


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_S

    def version_string(self) -> str:
        return "control-api"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        return

    def do_POST(self) -> None:
        if not self._guard():
            return
        if urlsplit(self.path).path != "/v1/rpc":
            self._plain(404, "not_found")
            return
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._plain(415, "unsupported_media_type")
            return
        length_text = self.headers.get("Content-Length")
        if length_text is None or "Transfer-Encoding" in self.headers:
            self._plain(411, "length_required")
            return
        if not length_text.isdecimal():
            self._plain(400, "bad_length")
            return
        length = int(length_text)
        if length > MAX_BODY_BYTES:
            self._plain(413, "body_too_large", close=True)
            return
        body = self.rfile.read(length)
        self._send_rpc(body)

    def do_GET(self) -> None:
        if not self._guard():
            return
        split = urlsplit(self.path)
        if split.path != "/v1/events":
            self._plain(404, "not_found")
            return
        query = parse_qs(split.query, keep_blank_values=True)
        if set(query) - {"after", "limit", "wait"} or any(len(v) != 1 for v in query.values()):
            self._plain(400, "bad_query")
            return
        try:
            params: dict[str, Any] = {"after": int(query.get("after", ["0"])[0])}
            if "limit" in query:
                params["limit"] = int(query["limit"][0])
            if "wait" in query:
                params["wait_s"] = float(query["wait"][0])
        except ValueError:
            self._plain(400, "bad_query")
            return
        message = {"jsonrpc": "2.0", "id": 1, "method": "events.poll", "params": params}
        self._send_rpc(json.dumps(message).encode("utf-8"))

    def do_PUT(self) -> None:
        self._plain(405, "method_not_allowed")

    do_DELETE = do_PATCH = do_PUT

    def _guard(self) -> bool:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._plain(403, "host_denied", close=True)
            return False
        if "Origin" in self.headers:
            self._plain(403, "origin_denied", close=True)
            return False
        return True

    def _send_rpc(self, body: bytes) -> None:
        response = self.server.service.handle(body, self.headers.get("Authorization"))
        status = status_for(response)
        payload = json.dumps(response, sort_keys=True).encode("utf-8")
        self.send_response(status)
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self._finish(payload, "application/json")

    def _plain(self, status: int, token: str, *, close: bool = False) -> None:
        self.send_response(status)
        if close or status >= 400:
            self.close_connection = True
        self._finish(json.dumps({"error": token}).encode("utf-8"), "application/json")

    def _finish(self, payload: bytes, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_HEAD(self) -> None:
        self._plain(405, "method_not_allowed")

    do_OPTIONS = do_HEAD


class ControlHTTPServer:
    def __init__(
        self,
        service: ControlService,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise RemoteBindRefused("remote_bind_refused")
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        bind = "127.0.0.1"
        self._server = _Server((bind, port), _Handler)
        actual = self._server.server_address[1]
        self._server.service = service
        self._server.max_connections = max_connections
        self._server.allowed_hosts = frozenset(
            {f"127.0.0.1:{actual}", f"localhost:{actual}"}
        )
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> ControlHTTPServer:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        """Release the bound socket without waiting for serve_forever."""

        self._server.server_close()

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
        self.close()
        if self._thread is not None:
            self._thread.join(5)

    def __enter__(self) -> ControlHTTPServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
