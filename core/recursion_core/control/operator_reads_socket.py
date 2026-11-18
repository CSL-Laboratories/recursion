"""Owner-only Unix socket for the operator inspect reads (CSL-798, candidate). See README.md.

The server hands every request to ``OperatorReads.call`` and to nothing else, so this socket cannot
carry a run start or stop. It has its own socket file, apart from any control socket.

Security model: no token. Access is the socket owner's user only.

- Runtime folder: when no path is given, the folder the default path lives in (``$XDG_RUNTIME_DIR``,
  else ``/run/user/<uid>``) must exist, be a real directory owned by this uid and be closed to group
  and other, or the server refuses to serve (``SocketUnsafe``). A folder others can write to would let
  them swap the ``recursion`` directory under it.
- Directory: created ``0700`` if missing. The server refuses to serve (``SocketUnsafe``) if the
  directory is a symlink, is not owned by this uid, or is open to group or other.
- Start lock: before it looks at the socket path, a server takes an exclusive ``flock`` on
  ``<path>.lock`` (an owner-only regular file it creates) and holds it while it serves; a second server
  for the same path refuses (``SocketUnsafe``) instead of probing, unlinking and binding alongside the
  first. ``close()`` removes the lock file. A crashed holder's lock is dropped by the kernel.
- Socket file: bound under umask ``0o077``, set to ``0600`` and checked before ``listen``.
- Peer: each connection's uid is read with ``SO_PEERCRED``; another uid is answered ``peer_refused`` with
  no host profile (``NO_PROFILE``) and closed unread. Refusals (``peer_refused``, ``busy``) are written
  without blocking the accept thread. Where ``AF_UNIX``, ``SO_PEERCRED``, ``flock`` or ``os.getuid`` is
  missing (macOS, Windows) the server refuses to serve with ``PlatformNotSupported``.

Framing: one request per connection. The client sends one JSON object line ``{"method": str,
"params": {}}`` of at most ``MAX_REQUEST_BYTES`` bytes; the server answers one JSON line and closes.
The request must arrive within the read deadline (``request_timeout``), and at most ``max_connections``
are served at once; one more is answered ``busy`` and closed, not queued.

Default path: ``$XDG_RUNTIME_DIR/recursion/operator-reads.sock``, else
``/run/user/<uid>/recursion/operator-reads.sock``.

The client's ``call`` never raises. Unreachable answers ``control_api_unavailable`` with ``delivered``
``False``; a request that was written but got no complete reply answers ``delivered`` ``"unknown"``.
The client checks the server's uid with ``SO_PEERCRED`` before it writes; where that is missing it
connects to nothing and answers ``control_api_unavailable``, ``delivered`` ``False``, detail
``platform_unsupported``, as the server refuses to serve there.
"""

from __future__ import annotations

import json
import math
import os
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping

try:
    import fcntl
except ImportError:  # Windows has no fcntl; start() refuses PlatformNotSupported there
    fcntl = None  # type: ignore[assignment]

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
_LOCK_SUFFIX = ".lock"
# A refusal is one non-blocking send on the accept thread; past this size it goes without the host profile, so
# it always fits a fresh socket's buffer and arrives whole.
_REFUSAL_MAX_BYTES = 16384
# A holder that closes removes the lock file; one who opened it just before may lock the removed file.
# Such a server opens the path again, this many times at most, then refuses.
_LOCK_ATTEMPTS = 3


def _frame_code(data: bytes) -> str | None:
    """The ``code`` of one JSON line, or None when the line is not that shape."""

    try:
        body = json.loads(data)
    except (ValueError, UnicodeError):
        return None
    if type(body) is not dict:
        return None
    code = body.get("code")
    return code if type(code) is str else None


class PlatformNotSupported(RuntimeError):
    """This platform lacks AF_UNIX, SO_PEERCRED or uids, so the owner-only rule cannot be enforced."""


class SocketUnsafe(RuntimeError):
    """The socket directory or file is not owner-only, or the path is taken; nothing is served."""


def _getuid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise PlatformNotSupported("no uids on this platform")
    return getuid()


def _runtime_dir(environ: Mapping[str, str] | None = None) -> str:
    """``$XDG_RUNTIME_DIR`` when it is an absolute path, else ``/run/user/<uid>``."""

    uid = _getuid()
    base = (os.environ if environ is None else environ).get("XDG_RUNTIME_DIR") or ""
    if not os.path.isabs(base):
        base = f"/run/user/{uid}"
    return base


def default_socket_path(environ: Mapping[str, str] | None = None) -> str:
    """``$XDG_RUNTIME_DIR/recursion/operator-reads.sock``, else under ``/run/user/<uid>``."""

    return os.path.join(_runtime_dir(environ), SOCKET_DIR, SOCKET_NAME)


def _check_runtime_dir(directory: str, uid: int) -> None:
    """The runtime folder must exist and be a real directory of this uid, closed to group and other."""

    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        raise SocketUnsafe("the runtime folder does not exist") from None
    if not stat.S_ISDIR(st.st_mode):
        raise SocketUnsafe("the runtime folder is not a directory")
    if st.st_uid != uid:
        raise SocketUnsafe("the runtime folder is not owned by this user")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise SocketUnsafe("the runtime folder is open to group or other")


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


def _take_start_lock(path: str, uid: int) -> tuple[int, tuple[int, int]]:
    """Take an exclusive ``flock`` on ``path`` (an owner-only regular file) and return ``(fd, identity)``.

    Held while the server serves, it orders the stale-socket probe, unlink and bind of two servers on
    one path: without it both could find the same stale socket, both unlink and bind, and the first
    would go on listening on an inode nobody can reach. A holder that closes removes the file, so a
    server whose lock landed on a file already removed opens the path again.
    """

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_LOCK_ATTEMPTS):
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise SocketUnsafe(f"the start lock cannot be opened ({type(exc).__name__})") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) & 0o077:
                raise SocketUnsafe("the start lock is not an owner-only file of this user")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SocketUnsafe("another server is starting or serving on this path") from None
            identity = (st.st_dev, st.st_ino)
            try:
                now = os.lstat(path)
            except FileNotFoundError:
                now = None
            if now is not None and (now.st_dev, now.st_ino) == identity:
                return fd, identity
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)  # the file we locked was removed by the holder before us: open the path again
    raise SocketUnsafe("the start lock kept changing under this server")


def _release_start_lock(path: str, held: tuple[int, tuple[int, int]]) -> None:
    """Remove the lock file if it is still the one we locked, then drop the lock."""

    fd, identity = held
    try:
        st = os.lstat(path)
        if (st.st_dev, st.st_ino) == identity:
            os.unlink(path)
    except OSError:
        pass
    finally:
        os.close(fd)


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
    except FileNotFoundError:
        return  # removed since the lstat: nothing is left to clear
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
        deadline = float(read_deadline_s)
        # NaN and infinity are not <= 0. settimeout then raises and the request is dropped with no answer.
        if not math.isfinite(deadline):
            raise ValueError("max_connections and read_deadline_s must be positive")
        self._reads, self._path = reads, path
        self._deadline, self._max = deadline, int(max_connections)
        self._peer_uid = peer_uid or _peer_uid
        self._slots = threading.BoundedSemaphore(self._max)
        self._lock = threading.Lock()
        # start and close share this. close must not decide the listener is absent while start can still publish.
        self._gate = threading.Lock()
        self._conns: set[socket.socket] = set()
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._identity: tuple[int, int] | None = None
        self._start_lock: tuple[int, tuple[int, int]] | None = None
        self._uid = -1

    @property
    def path(self) -> str | None:
        return self._path

    def start(self) -> OperatorReadsServer:
        with self._gate:
            return self._start_locked()

    def _start_locked(self) -> OperatorReadsServer:
        if self._listener is not None or self._stop.is_set():
            raise RuntimeError("the server was already started")
        if _AF_UNIX is None or _SO_PEERCRED is None or fcntl is None:
            raise PlatformNotSupported("AF_UNIX with SO_PEERCRED and flock is needed to serve owner-only")
        self._uid = _getuid()
        path = self._path
        if path is None:
            runtime = _runtime_dir()
            _check_runtime_dir(runtime, self._uid)
            path = os.path.join(runtime, SOCKET_DIR, SOCKET_NAME)
        _secure_dir(os.path.dirname(path), self._uid)
        lock_path = path + _LOCK_SUFFIX
        held = _take_start_lock(lock_path, self._uid)
        try:
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
        except BaseException:
            _release_start_lock(lock_path, held)
            raise
        self._path, self._identity, self._listener = path, (st.st_dev, st.st_ino), listener
        self._start_lock = held
        try:
            thread = threading.Thread(target=self._accept_loop, name="operator-reads-accept", daemon=True)
            thread.start()
        except BaseException:
            # Nothing serves: undo this start whole, so the next server finds neither our socket nor our lock.
            self._listener, self._start_lock, self._identity = None, None, None
            listener.close()
            try:
                now = os.lstat(path)
                if (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino):
                    os.unlink(path)
            except OSError:
                pass
            _release_start_lock(lock_path, held)
            raise
        self._thread = thread
        return self

    def close(self) -> None:
        """Stop serving and remove this server's own socket file (never another file at the path)."""

        with self._gate:
            self._stop.set()
            # Start holds this lock until it has published or rolled back, so a missing listener
            # here is not published afterwards. A later call finishes cleanup the first one did not,
            # and does nothing once the socket lock is gone and no source call is still running.
            if (self._listener is None and self._start_lock is None
                    and not any(worker.is_alive() for worker in self._workers)):
                return
            if self._thread is not None:
                self._thread.join(timeout=5.0)
            listener, self._listener = self._listener, None
            if listener is not None:
                listener.close()
            with self._lock:
                conns, workers = list(self._conns), list(self._workers)
            for conn in conns:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            for worker in workers:
                # A timed join used to return while the source call could still write. Wait it out.
                worker.join(timeout=5.0)
                while worker.is_alive():
                    worker.join(timeout=5.0)
            try:
                st = os.lstat(self._path)
                if (st.st_dev, st.st_ino) == self._identity:
                    os.unlink(self._path)
            except OSError:
                pass
            held, self._start_lock = self._start_lock, None
            if held is not None:  # after the socket is gone, so the next server finds nothing live
                _release_start_lock(self._path + _LOCK_SUFFIX, held)

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
                # An unstarted thread cannot be joined. Leaving it tracked makes close raise
                # before the socket path is removed, and the slot must be given back.
                with self._lock:
                    self._conns.discard(conn)
                    self._workers = [w for w in self._workers if w is not worker]
                self._slots.release()
                self._reject(conn, BUSY)

    def _reject(self, conn: socket.socket, code: str) -> None:
        """Answer a refused connection on the accept thread, without ever waiting on the peer.

        A peer of another uid (or one whose uid cannot be read) is not authenticated, so its refusal
        carries no host profile. The frame is written once, non-blocking: a peer that does not read
        cannot stall the accept loop.
        """
        profile = None if code == PEER_REFUSED else self._reads.profile
        data = self._encode(refusal(code, profile=profile))
        # One non-blocking send must carry the original code whole. A host profile too large for that
        # send is left out. A profile large enough that the reply cap substitutes a short
        # reply_too_large is left out the same way, and the original code is what is written.
        if len(data) > _REFUSAL_MAX_BYTES or _frame_code(data) != code:
            data = self._encode(refusal(code, profile=None))
        try:
            conn.setblocking(False)
            conn.send(data)
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
            # The host profile that made the answer too large must not ride along in the fallback.
            data = json.dumps(self._refuse(REPLY_TOO_LARGE), separators=(",", ":")).encode("ascii") + b"\n"
            if len(data) > MAX_REPLY_BYTES:
                data = json.dumps(refusal(REPLY_TOO_LARGE), separators=(",", ":")).encode("ascii") + b"\n"
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
        if _AF_UNIX is None or _SO_PEERCRED is None:
            # Without SO_PEERCRED the server's uid cannot be checked: connect to nothing, as the server
            # refuses to serve there (finding 2 of the Nov 12 review).
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
                if _peer_uid(conn) != uid:
                    return _local(CONTROL_API_UNAVAILABLE, False, "server_not_this_user")
            except (OSError, ValueError, TypeError, struct.error, OverflowError) as exc:
                # settimeout can raise OverflowError before connect. Nothing was contacted.
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
                # A buffer already at the cap has no room for the newline that would finish a legal reply.
                if len(buf) >= MAX_REPLY_BYTES:
                    return _local(CONTROL_API_UNAVAILABLE, "unknown", "reply_too_large")
                conn.settimeout(remaining)
                try:
                    room = MAX_REPLY_BYTES + 1 - len(buf)
                    chunk = conn.recv(min(65536, room))
                except TimeoutError:
                    return _local(CONTROL_API_UNAVAILABLE, "unknown", "reply_timeout")
                except OSError:
                    chunk = b""
                if not chunk:
                    return _local(CONTROL_API_UNAVAILABLE, "unknown", "no_complete_reply")
                buf += chunk
                # The recv that carries the newline is held to the same cap. Count through the newline,
                # so bytes after a short line in the same chunk are not part of the frame.
                cut = buf.find(b"\n")
                measured = (cut + 1) if cut >= 0 else len(buf)
                if measured > MAX_REPLY_BYTES:
                    return _local(CONTROL_API_UNAVAILABLE, "unknown", "reply_too_large")
        finally:
            conn.close()
        try:
            reply = json.loads(bytes(buf).partition(b"\n")[0].decode("utf-8", "strict"))
        except (ValueError, RecursionError):
            reply = None
        if type(reply) is not dict or type(reply.get("code")) is not str:
            return _local(CONTROL_API_UNAVAILABLE, "unknown", "reply_invalid")
        return reply
