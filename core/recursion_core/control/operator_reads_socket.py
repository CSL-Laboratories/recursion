"""Owner-only Unix socket for the operator inspect reads (CSL-798, candidate). See README.md.

The server hands every request to ``OperatorReads.call`` and to nothing else, so this socket cannot
carry a run start or stop. It has its own socket file, apart from any control socket.

Security model: no token. Access is the socket owner's user only.

- Directory: created ``0700`` if missing. The server refuses to serve (``SocketUnsafe``) if the
  directory is a symlink, is not owned by this uid, or is open to group or other.
- Socket file: bound under umask ``0o077``, set to ``0600`` and checked before ``listen``.
- Peer: each connection's uid is read with ``SO_PEERCRED``; another uid is answered ``peer_refused`` and
  closed unread. Where ``AF_UNIX``, ``SO_PEERCRED`` or ``os.getuid`` is missing (macOS, Windows) the
  server refuses to serve with ``PlatformNotSupported``.

Framing: one request per connection. The client sends one JSON object line ``{"method": str,
"params": {}}`` of at most ``MAX_REQUEST_BYTES`` bytes; the server answers one JSON line and closes.
The request must arrive within the read deadline (``request_timeout``), and at most ``max_connections``
are served at once; one more is answered ``busy`` and closed, not queued.

Default path: ``$XDG_RUNTIME_DIR/recursion/operator-reads.sock``, else
``/run/user/<uid>/recursion/operator-reads.sock``.

The client's ``call`` never raises. Unreachable answers ``control_api_unavailable`` with ``delivered``
``False``; a request that was written but got no complete reply answers ``delivered`` ``"unknown"``.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping

from recursion_core.control.operator_reads import (
    BUSY,
    CONTROL_API_UNAVAILABLE,
    INSPECT_METHOD,
    METHOD_NOT_ALLOWED,
    PEER_REFUSED,
    REPLY_TOO_LARGE,
    REQUEST_INVALID,
    REQUEST_TIMEOUT,
    REQUEST_TOO_LARGE,
    OperatorReads,
    refusal,
)

MAX_REQUEST_BYTES = 4096
MAX_REPLY_BYTES = 1 << 20
SOCKET_DIR, SOCKET_NAME = "recursion", "operator-reads.sock"
PLATFORM_UNSUPPORTED = "platform_unsupported"

_AF_UNIX = getattr(socket, "AF_UNIX", None)
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", None)
_UMASK_LOCK = threading.Lock()  # the umask is process-wide
_BACKLOG = 32  # waiting to be accepted, not served: the accept loop answers "busy" over the cap


class PlatformNotSupported(RuntimeError):
    """This platform lacks AF_UNIX, SO_PEERCRED or uids, so the owner-only rule cannot be enforced."""


class SocketUnsafe(RuntimeError):
    """The socket directory or file is not owner-only, or the path is taken; nothing is served."""


def _getuid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise PlatformNotSupported("no uids on this platform")
    return getuid()


def default_socket_path(environ: Mapping[str, str] | None = None) -> str:
    """``$XDG_RUNTIME_DIR/recursion/operator-reads.sock``, else under ``/run/user/<uid>``."""

    uid = _getuid()
    base = (os.environ if environ is None else environ).get("XDG_RUNTIME_DIR") or ""
    if not os.path.isabs(base):
        base = f"/run/user/{uid}"
    return os.path.join(base, SOCKET_DIR, SOCKET_NAME)


def _peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("iII"))
    return struct.unpack("iII", creds)[1]


def _secure_dir(directory: str, uid: int) -> None:
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode):
        raise SocketUnsafe("the socket directory is not a directory")
    if st.st_uid != uid:
        raise SocketUnsafe("the socket directory is not owned by this user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SocketUnsafe("the socket directory is open to group or other")


def _clear_stale(path: str) -> None:
    """Remove a socket file nobody listens on. A live listener or a non-socket is left alone."""

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise SocketUnsafe("the socket path exists and is not a socket")
    probe = socket.socket(_AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(path)
    except ConnectionRefusedError:
        os.unlink(path)
        return
    except OSError:
        pass
    finally:
        probe.close()
    raise SocketUnsafe("the socket path is in use")


class OperatorReadsServer:
    """Serve ``reads`` on an owner-only Unix socket from a daemon thread."""

    def __init__(self, reads: OperatorReads, path: str | None = None, *, read_deadline_s: float = 2.0,
                 max_connections: int = 8, peer_uid: Callable[[socket.socket], int] | None = None) -> None:
        if not isinstance(reads, OperatorReads):
            raise TypeError("this socket serves an OperatorReads and nothing else")
        if max_connections < 1 or read_deadline_s <= 0:
            raise ValueError("max_connections and read_deadline_s must be positive")
        self._reads, self._path = reads, path
        self._deadline, self._max = float(read_deadline_s), int(max_connections)
        self._peer_uid = peer_uid or _peer_uid
        self._slots = threading.BoundedSemaphore(self._max)
        self._lock = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._identity: tuple[int, int] | None = None
        self._uid = -1

    @property
    def path(self) -> str | None:
        return self._path

    def start(self) -> OperatorReadsServer:
        if self._listener is not None or self._stop.is_set():
            raise RuntimeError("the server was already started")
        if _AF_UNIX is None or _SO_PEERCRED is None:
            raise PlatformNotSupported("AF_UNIX with SO_PEERCRED is needed to serve owner-only")
        self._uid = _getuid()
        path = self._path or default_socket_path()
        _secure_dir(os.path.dirname(path), self._uid)
        _clear_stale(path)
        listener = socket.socket(_AF_UNIX, socket.SOCK_STREAM)
        try:
            with _UMASK_LOCK:
                old = os.umask(0o077)
                try:
                    listener.bind(path)
                finally:
                    os.umask(old)
            try:
                os.chmod(path, 0o600)
                st = os.lstat(path)
                if not stat.S_ISSOCK(st.st_mode) or st.st_uid != self._uid or stat.S_IMODE(st.st_mode) != 0o600:
                    raise SocketUnsafe("the socket file is not an owner-only socket")
                listener.listen(_BACKLOG)
                listener.settimeout(0.1)
            except BaseException:
                os.unlink(path)
                raise
        except BaseException:
            listener.close()
            raise
        self._path, self._identity, self._listener = path, (st.st_dev, st.st_ino), listener
        self._thread = threading.Thread(target=self._accept_loop, name="operator-reads-accept", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        """Stop serving and remove this server's own socket file (never another file at the path)."""

        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is None:
            return
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        listener.close()
        with self._lock:
            conns, workers = list(self._conns), list(self._workers)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in workers:
            worker.join(timeout=5.0)
        try:
            st = os.lstat(self._path)
            if (st.st_dev, st.st_ino) == self._identity:
                os.unlink(self._path)
        except OSError:
            pass

    def __enter__(self) -> OperatorReadsServer:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        listener = self._listener
        while not self._stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                time.sleep(0.05)
                continue
            try:
                same_user = self._peer_uid(conn) == self._uid
            except Exception:  # noqa: BLE001 - a peer we cannot identify is refused
                same_user = False
            if not same_user:
                self._reject(conn, PEER_REFUSED)
                continue
            if not self._slots.acquire(blocking=False):
                self._reject(conn, BUSY)
                continue
            worker = threading.Thread(target=self._serve, args=(conn,), name="operator-reads-conn", daemon=True)
            with self._lock:
                self._conns.add(conn)
                self._workers = [w for w in self._workers if w.is_alive()] + [worker]
            try:
                worker.start()
            except RuntimeError:
                with self._lock:
                    self._conns.discard(conn)
                self._slots.release()
                self._reject(conn, BUSY)

    def _reject(self, conn: socket.socket, code: str) -> None:
        try:
            conn.settimeout(0.5)
            conn.sendall(self._encode(refusal(code, profile=self._reads.profile)))
        except OSError:
            pass
        finally:
            conn.close()

    def _serve(self, conn: socket.socket) -> None:
        try:
            reply = self._answer(conn)
            if reply is not None:
                conn.settimeout(self._deadline)
                conn.sendall(self._encode(reply))
        except Exception:  # noqa: BLE001 - a broken connection ends here; the server keeps serving
            pass
        finally:
            conn.close()
            with self._lock:
                self._conns.discard(conn)
            self._slots.release()

    def _refuse(self, code: str, detail: str | None = None) -> dict:
        return refusal(code, detail=detail, profile=self._reads.profile)

    def _answer(self, conn: socket.socket) -> dict | None:
        deadline = time.monotonic() + self._deadline
        buf = bytearray()
        while b"\n" not in buf:
            if len(buf) >= MAX_REQUEST_BYTES:
                return self._refuse(REQUEST_TOO_LARGE)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._refuse(REQUEST_TIMEOUT)
            conn.settimeout(remaining)
            try:
                chunk = conn.recv(MAX_REQUEST_BYTES + 1 - len(buf))
            except TimeoutError:
                return self._refuse(REQUEST_TIMEOUT)
            except OSError:
                return None
            if not chunk:
                return None if self._stop.is_set() else self._refuse(REQUEST_INVALID, "incomplete_request")
            buf += chunk
        line, _, rest = bytes(buf).partition(b"\n")
        if len(line) + 1 > MAX_REQUEST_BYTES:
            return self._refuse(REQUEST_TOO_LARGE)
        if rest:
            return self._refuse(REQUEST_INVALID, "trailing_data")
        try:
            request = json.loads(line.decode("utf-8", "strict"))
        except (ValueError, RecursionError):
            return self._refuse(REQUEST_INVALID, "not_json")
        if type(request) is not dict or not {"method"} <= set(request) <= {"method", "params"}:
            return self._refuse(REQUEST_INVALID, "request_shape")
        return self._reads.call(request["method"], request.get("params"))

    def _encode(self, reply: dict) -> bytes:
        try:
            data = json.dumps(reply, allow_nan=False, separators=(",", ":")).encode("ascii") + b"\n"
        except (TypeError, ValueError, RecursionError):
            data = b""
        if not data or len(data) > MAX_REPLY_BYTES:
            data = json.dumps(self._refuse(REPLY_TOO_LARGE), separators=(",", ":")).encode("ascii") + b"\n"
        return data


def _local(code: str, delivered: bool | str, detail: str) -> dict:
    return {"code": code, "status_code": code, "delivered": delivered, "detail": detail}


class OperatorReadsClient:
    """``call(method, params) -> dict`` over the socket, one connection per call. Never raises."""

    def __init__(self, path: str | None = None, *, timeout_s: float = 5.0) -> None:
        self._path, self._timeout = path, timeout_s

    def call(self, method: str, params: dict | None = None) -> dict:
        try:
            return self._call(method, params)
        except Exception as exc:  # noqa: BLE001 - the caller always gets a typed answer
            return _local(CONTROL_API_UNAVAILABLE, "unknown", type(exc).__name__)

    def _call(self, method: object, params: object) -> dict:
        if type(method) is not str or INSPECT_METHOD.fullmatch(method) is None:
            return _local(METHOD_NOT_ALLOWED, False, "not an operator inspect read; nothing was sent")
        if params is not None and (type(params) is not dict or params):
            return _local(REQUEST_INVALID, False, "params must be {}; nothing was sent")
        line = json.dumps({"method": method, "params": {}}, separators=(",", ":")).encode("ascii") + b"\n"
        if len(line) > MAX_REQUEST_BYTES:
            return _local(METHOD_NOT_ALLOWED, False, "method name too long; nothing was sent")
        if _AF_UNIX is None:
            return _local(CONTROL_API_UNAVAILABLE, False, PLATFORM_UNSUPPORTED)
        try:
            uid = _getuid()
            path = self._path or default_socket_path()
            timeout = float(self._timeout)
        except PlatformNotSupported:
            return _local(CONTROL_API_UNAVAILABLE, False, PLATFORM_UNSUPPORTED)
        except (TypeError, ValueError):
            return _local(CONTROL_API_UNAVAILABLE, False, "timeout_invalid")
        if not 0 < timeout < float("inf"):
            return _local(CONTROL_API_UNAVAILABLE, False, "timeout_invalid")
        deadline = time.monotonic() + timeout
        conn = socket.socket(_AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                conn.settimeout(timeout)
                conn.connect(path)
                if _SO_PEERCRED is not None and _peer_uid(conn) != uid:
                    return _local(CONTROL_API_UNAVAILABLE, False, "server_not_this_user")
            except (OSError, ValueError, TypeError, struct.error) as exc:
                return _local(CONTROL_API_UNAVAILABLE, False, f"connect_failed: {type(exc).__name__}")
            try:
                conn.sendall(line)
            except OSError:
                pass  # the server may already have answered (busy, peer_refused); read what is there
            buf = bytearray()
            while b"\n" not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _local(CONTROL_API_UNAVAILABLE, "unknown", "reply_timeout")
                if len(buf) > MAX_REPLY_BYTES:
                    return _local(CONTROL_API_UNAVAILABLE, "unknown", "reply_too_large")
                conn.settimeout(remaining)
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    return _local(CONTROL_API_UNAVAILABLE, "unknown", "reply_timeout")
                except OSError:
                    chunk = b""
                if not chunk:
                    return _local(CONTROL_API_UNAVAILABLE, "unknown", "no_complete_reply")
                buf += chunk
        finally:
            conn.close()
        try:
            reply = json.loads(bytes(buf).partition(b"\n")[0].decode("utf-8", "strict"))
        except (ValueError, RecursionError):
            reply = None
        if type(reply) is not dict or type(reply.get("code")) is not str:
            return _local(CONTROL_API_UNAVAILABLE, "unknown", "reply_invalid")
        return reply
