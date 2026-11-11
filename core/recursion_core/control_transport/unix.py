"""Linux-first AF_UNIX control transport (CSL-719; D02, D50, CX-026).

The server listens only on a filesystem Unix socket inside an owner-only (0700) directory,
chmods the socket to 0600, and checks the peer's uid on every accepted connection before
reading a byte. A refused peer gets no frame back: there is no pre-auth face (CX-026). An
authenticated peer over the connection cap gets a typed ``busy`` frame instead. The uid
check is authentication only: the handler gets the peer's credentials and grants scopes.
Remote control reaches this socket over an SSH stdio relay; nothing here opens a network
listener, and any network-shaped address raises ``RemoteBindRefused``.

Linux is the tested path. macOS uses the same AF_UNIX family and is designed in through
``peercred.get_peer_cred``; Windows is a labelled stub (``windows.py``, F40).
"""

from __future__ import annotations

import errno
import ipaddress
import os
import re
import socket
import stat
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl; it is the F40 stub path
    fcntl = None  # type: ignore[assignment]

from .correlation import ACCEPTED, CANCELLED, IGNORED, MISMATCH, MISSING_ID, SETTLED_CAP, RequestTable, same_id
from .errors import FrameError, InsecureSocketPath, PeerRefused, RemoteBindRefused, TransportBusy
from .framing import HEADER, MAX_FRAME_BYTES, decode_body, encode_frame, id_reencodes, read_frame, write_frame
from .handlers import Connection, Handler, Message
from .peercred import PeerCred, get_peer_cred

PeerCredGetter = Callable[[socket.socket], PeerCred]

DEFAULT_SOCKET_NAME = "control.sock"
MAX_CONNECTIONS = 16
# Whole-frame deadline: each request must arrive complete within this long of the server
# being ready for it (connect or previous reply), however the bytes are spaced.
IDLE_TIMEOUT_S = 30.0
# One budget for every handler still running in close(). It is not multiplied by the
# handler count: N blocked handlers share this deadline, then the ones still running
# are cancelled and their late results are not written.
CLOSE_TIMEOUT_S = 5.0
# After frame_too_large the peer may still be writing the body. The worker returns
# and frees its slot; this extra fd stays open so those writes are not reset.
# One deadline, not restarted by arriving bytes.
_PEER_HOLD_S = 2.0
_peer_holds = threading.BoundedSemaphore(MAX_CONNECTIONS)

# Sent, without reading anything, to an authenticated peer over the connection cap, so a
# client can tell "not delivered, server busy" from a refusal or a crash.
BUSY_FRAME = encode_frame(
    {"ok": False, "error": {"code": "busy", "layer": "transport", "delivered": False, "retryable": True}}
)
_ACCEPT_POLL_S = 0.1
# Linux sun_path is 108 bytes including the trailing NUL.
_MAX_SUN_PATH = 107
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_HOST_PORT = re.compile(r"^\[?[^/\\]*\]?:\d+$")


class TransportServer(Protocol):
    """What a control API (for example #327's) needs from a server transport."""

    @property
    def path(self) -> str: ...

    def start(self) -> None: ...

    def close(self) -> None: ...


class TransportClient(Protocol):
    """What a control CLI needs from a client transport: one request, one response."""

    def request(self, message: Message) -> Message: ...

    def close(self) -> None: ...


def refuse_remote_bind(address: object) -> str:
    """Return ``address`` as an absolute socket path, or raise a typed refusal.

    Tuples, ints, URLs and ``host:port`` strings are network addresses and raise
    ``RemoteBindRefused``. Abstract-namespace names (leading NUL) have no filesystem
    permissions and relative paths depend on the cwd; both raise ``InsecureSocketPath``.
    """
    if isinstance(address, (tuple, list, int)) or isinstance(address, bool):
        raise RemoteBindRefused(f"remote_bind_refused: {address!r} is not an AF_UNIX path")
    if isinstance(address, os.PathLike):
        address = os.fspath(address)
    if isinstance(address, bytes):
        try:
            address = address.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InsecureSocketPath("socket path is not UTF-8") from exc
    if not isinstance(address, str):
        raise RemoteBindRefused(f"remote_bind_refused: {type(address).__name__} is not an AF_UNIX path")
    if _URL_SCHEME.match(address) and not address.startswith("unix://"):
        raise RemoteBindRefused(f"remote_bind_refused: {address!r}")
    if address.startswith("unix://"):
        address = address[len("unix://"):]
    if _HOST_PORT.match(address) or _is_host(address):
        raise RemoteBindRefused(f"remote_bind_refused: {address!r}")
    if not address or address.startswith("\0"):
        raise InsecureSocketPath("abstract or empty socket names are refused; use a 0700 directory")
    if "\0" in address:
        raise InsecureSocketPath("socket path contains a NUL byte")
    if not os.path.isabs(address):
        raise InsecureSocketPath("socket path must be absolute")
    if len(os.fsencode(address)) > _MAX_SUN_PATH:
        raise InsecureSocketPath("socket path is longer than sun_path allows")
    return address


def _is_host(address: str) -> bool:
    if address in ("localhost", "*"):
        return True
    try:
        ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False
    return True


def ensure_private_dir(directory: str) -> None:
    """Create ``directory`` as 0700, or check an existing one is ours, real and 0700.

    An existing directory that is a symlink, belongs to another uid, or grants any group or
    other bit is refused, not repaired: the caller picked the wrong place.
    """
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    st = os.lstat(directory)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise InsecureSocketPath(f"{directory} is not a real directory")
    if st.st_uid != os.geteuid():
        raise InsecureSocketPath(f"{directory} is owned by uid {st.st_uid}")
    if st.st_mode & 0o077:
        raise InsecureSocketPath(f"{directory} has mode {stat.S_IMODE(st.st_mode):04o}, want 0700")


def _acquire_lock(path: str) -> int:
    """Take an exclusive ``flock`` on ``path`` (a regular file of ours) and return its fd.

    Held for the server's lifetime, it serialises the stale-socket probe, unlink and bind:
    without it two servers starting on one stale socket can both bind, and the loser keeps
    listening on an unlinked inode nobody can reach. The lock file is never unlinked, so a
    later server always locks the same inode.
    """
    if fcntl is None:  # pragma: no cover - POSIX only
        raise InsecureSocketPath("no flock on this platform")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise InsecureSocketPath(f"{path} cannot be opened as our lock file: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise InsecureSocketPath(f"{path} exists and is not our lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InsecureSocketPath(f"{path} is held: another server is starting or live") from exc
    except BaseException:
        os.close(fd)
        raise
    return fd


def _clear_stale_socket(path: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid():
        raise InsecureSocketPath(f"{path} exists and is not our socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(path)
    except OSError as exc:
        if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            raise
        os.unlink(path)
        return
    finally:
        probe.close()
    raise InsecureSocketPath(f"{path} is live: another server is listening")


def check_peer(sock: socket.socket, getter: PeerCredGetter, expected_uid: int) -> PeerCred:
    """Fail closed: unreadable credentials and a foreign uid both raise ``PeerRefused``."""
    try:
        cred = getter(sock)
    except Exception as exc:  # any getter failure is a refusal, never a dead accept loop
        raise PeerRefused(f"peer credentials unreadable: {exc}") from exc
    if not isinstance(cred, PeerCred) or type(cred.uid) is not int:
        raise PeerRefused("peer credentials malformed")
    if cred.uid != expected_uid:
        raise PeerRefused(f"peer uid {cred.uid} is not {expected_uid}")
    return cred


class UnixControlServer:
    """Owner-only AF_UNIX listener that hands each frame to ``handler`` and writes its reply.

    ``handler(request, connection)`` gets the peer's credentials and a per-connection
    ``Connection``; the uid check here is authentication only and grants no scope.
    ``peer_cred`` is injectable so tests can simulate a foreign uid without changing the
    real one; production uses ``get_peer_cred`` (SO_PEERCRED on Linux).
    """

    def __init__(
        self,
        directory: object,
        handler: Handler,
        *,
        name: str = DEFAULT_SOCKET_NAME,
        peer_cred: PeerCredGetter = get_peer_cred,
        max_connections: int = MAX_CONNECTIONS,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        max_frame_bytes: int = MAX_FRAME_BYTES,
        close_timeout_s: float = CLOSE_TIMEOUT_S,
        settled_cap: int = SETTLED_CAP,
    ) -> None:
        directory = refuse_remote_bind(directory)
        if not name or "/" in name or name in (".", ".."):
            raise InsecureSocketPath("socket name must be a single path component")
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        self.directory = directory
        self._path = refuse_remote_bind(os.path.join(directory, name))
        self._handler = handler
        self._peer_cred = peer_cred
        self._uid = os.geteuid()
        self._slots = threading.BoundedSemaphore(max_connections)
        self._idle_timeout_s = idle_timeout_s
        self._max_frame_bytes = max_frame_bytes
        self._requests = RequestTable(close_timeout_s=close_timeout_s, settled_cap=settled_cap)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._lock_fd: int | None = None
        self._bound: tuple[int, int] | None = None
        self._thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._conns: set[socket.socket] = set()
        self._next_conn_id = 0
        self.refused: list[str] = []
        self.accepted = 0
        self.late_dropped = 0

    @property
    def path(self) -> str:
        return self._path

    def settled_count(self) -> int:
        """Settled ids still remembered across open connections."""
        return self._requests.settled_count()

    def start(self) -> None:
        with self._lock:
            if self._sock is not None:
                raise RuntimeError("server already started")
            if self._stop.is_set():
                raise RuntimeError("server already closed")
            ensure_private_dir(self.directory)
            lock_fd = _acquire_lock(self._path + ".lock")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            bound = False
            try:
                _clear_stale_socket(self._path)
                # The 0700 directory already keeps other uids out of the bind-to-chmod window;
                # chmod rather than umask because umask is process-wide and racy across threads.
                sock.bind(self._path)
                bound = True
                os.chmod(self._path, 0o600)
                st = os.lstat(self._path)
                if not stat.S_ISSOCK(st.st_mode) or st.st_uid != self._uid:
                    raise InsecureSocketPath(f"{self._path} changed under us after bind")
                sock.listen(socket.SOMAXCONN)
                sock.settimeout(_ACCEPT_POLL_S)
            except BaseException:
                sock.close()
                if bound:
                    try:
                        os.unlink(self._path)
                    except FileNotFoundError:
                        pass
                os.close(lock_fd)
                raise
            self._sock = sock
            self._lock_fd = lock_fd
            self._bound = (st.st_dev, st.st_ino)
            self._thread = threading.Thread(target=self._accept_loop, name="control-transport-accept", daemon=True)
            self._thread.start()

    def close(self) -> None:
        """Stop accepting and drop live connections under one shared deadline.

        Each join waits only for the time still left on that deadline, so N blocked
        handlers do not cost N timeouts. Handlers still running when the deadline
        passes are cancelled. ``_serve`` does not write a result that arrives after that.
        """
        self._stop.set()
        self._requests.begin_close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._requests.remaining())
        with self._lock:
            workers = list(self._workers)
            conns = list(self._conns)
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for worker in workers:
            if worker is threading.current_thread():
                continue
            worker.join(timeout=self._requests.remaining())
        # The wait is over. Cancel whoever is still inside a handler, even if the clock
        # is a hair short of the deadline, and do not give the rest another timeout.
        self._requests.abandon()
        with self._lock:
            sock, self._sock = self._sock, None
            lock_fd, self._lock_fd = self._lock_fd, None
            bound, self._bound = self._bound, None
        if sock is not None:
            sock.close()
            # Unlink only the inode we bound: if an operator removed our socket and a
            # successor bound the path, closing us must not take its socket away.
            try:
                st = os.lstat(self._path)
            except FileNotFoundError:
                st = None
            if st is not None and bound == (st.st_dev, st.st_ino):
                try:
                    os.unlink(self._path)
                except FileNotFoundError:
                    pass
        if lock_fd is not None:
            os.close(lock_fd)

    def __enter__(self) -> UnixControlServer:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _refuse(self, reason: str) -> None:
        with self._lock:
            self.refused.append(reason)

    def _accept_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            try:
                cred = check_peer(conn, self._peer_cred, self._uid)
            except PeerRefused as exc:
                self._refuse(str(exc))
                conn.close()
                continue
            if not self._slots.acquire(blocking=False):
                # The peer is authenticated, so it gets a typed answer instead of a silent
                # close it could not tell from a foreign-uid refusal.
                self._refuse("max_connections")
                _send_busy(conn)
                conn.close()
                continue
            with self._lock:
                self._next_conn_id += 1
                connection = Connection(conn_id=self._next_conn_id, peer=cred)
            worker = threading.Thread(
                target=self._serve, args=(conn, connection), name="control-transport-conn", daemon=True
            )
            with self._lock:
                self.accepted += 1
                self._workers.add(worker)
                self._conns.add(conn)
            try:
                worker.start()
            except RuntimeError:  # thread limit: drop this connection, keep accepting
                self._refuse("worker_start_failed")
                with self._lock:
                    self._workers.discard(worker)
                    self._conns.discard(conn)
                conn.close()
                self._slots.release()

    def _serve(self, conn: socket.socket, connection: Connection) -> None:
        try:
            while not self._stop.is_set():
                request = self._read_request(conn)
                if request is None:
                    return
                if request.get("op") == "cancel":
                    if not self._write_reply(conn, self._cancel_message(request), request):
                        return
                    continue
                if "id" not in request:
                    claim = self._requests.claim_absent(scope=connection.conn_id)
                else:
                    claim = self._requests.claim(request["id"], scope=connection.conn_id)
                if not claim.accepted:
                    code = claim.code if claim.code is not None else MISSING_ID
                    if not self._write_reply(conn, _correlated_error(code, request), request):
                        return
                    continue
                connection.cancelled = claim.event
                response = self._call_handler(request, connection)
                outcome = self._requests.finish(claim, response)
                if outcome == MISMATCH:
                    # The handler named a different id. That body is not this request's
                    # reply, so it is not written. The error carries the request's own id.
                    if not self._write_reply(conn, _correlated_error("internal_error", request), request):
                        return
                    continue
                if outcome == IGNORED:
                    self._count_late()
                    # The handler's body is not this request's reply. A live cancel still
                    # tells that client the request was cancelled; a close does not.
                    # A handler that never watches cancelled hits this same path: close
                    # has already abandoned the claim, so the late body is not written.
                    if self._requests.past_deadline() or claim.request_id is None:
                        return
                    if not self._write_reply(conn, _correlated_error(CANCELLED, request), request):
                        return
                    continue
                if outcome == ACCEPTED and claim.request_id is not None and "id" not in response:
                    response = {**response, "id": claim.request_id}
                if not self._write_reply(conn, response, request):
                    return
        finally:
            # The connection's settled ids die with it. A replay on a connection
            # that is still open is unchanged.
            self._requests.drop_scope(connection.conn_id)
            conn.close()
            self._slots.release()
            with self._lock:
                self._workers.discard(threading.current_thread())
                self._conns.discard(conn)

    def _read_request(self, conn: socket.socket) -> Message | None:
        """Read one request. A bad frame is answered and ends the connection.

        The answer carries the request id when the frame yielded one, including
        null, so a client that sent an id does not see ``reply_id_mismatch``.
        A frame that is not JSON, or whose id cannot be read, is answered with
        no id. That id-less reply is the one bad_frame the client cannot correlate.
        """
        deadline = time.monotonic() + self._idle_timeout_s
        try:
            return read_frame(conn, max_bytes=self._max_frame_bytes, deadline=deadline)
        except FrameError as exc:
            reply: Message = {"ok": False, "error": {"code": "bad_frame", "detail": str(exc)}}
            if exc.id_known:
                reply["id"] = exc.request_id
            _send_quietly(conn, reply)
            if str(exc) == "frame_too_large":
                _hold_peer_open(conn)
            return None
        except OSError:
            return None

    def _call_handler(self, request: Message, connection: Connection) -> Message:
        try:
            response = self._handler(request, connection)
            if not isinstance(response, dict):
                raise TypeError("handler returned a non-object")
        except Exception:
            response = {"ok": False, "error": {"code": "internal_error"}}
        return _handler_reply(response)

    def _cancel_message(self, request: Message) -> Message:
        code = self._requests.cancel(request["id"] if "id" in request else None)
        if code == CANCELLED:
            body: Message = {"ok": True, "op": "cancel", "state": "cancelled"}
            if "id" in request:
                body["id"] = request["id"]
            return body
        return _correlated_error(code, request)

    def _write_reply(self, conn: socket.socket, message: Message, request: Message | None = None) -> bool:
        """Write ``message``. A frame that does not fit is ``response_too_large``.

        When ``request`` carried an id, that id is copied onto the error, including
        null, so the client that sent it does not see ``reply_id_mismatch``.
        """
        try:
            conn.settimeout(self._idle_timeout_s)  # sendall's timeout bounds the whole write
            write_frame(conn, _drop_unencodable_id(message), max_bytes=self._max_frame_bytes)
        except FrameError:
            reply: Message = {"ok": False, "error": {"code": "response_too_large"}}
            if request is not None and "id" in request:
                reply["id"] = request["id"]
            _send_quietly(conn, reply)
            return False
        except (OSError, ValueError):
            # A reply that still cannot be encoded ends this request. It must not
            # kill the worker.
            return False
        return True

    def _count_late(self) -> None:
        with self._lock:
            self.late_dropped += 1


def _send_busy(conn: socket.socket) -> None:
    """Best effort, never blocking the accept loop: the frame fits any socket buffer."""
    try:
        conn.setblocking(False)
        conn.send(BUSY_FRAME)
    except OSError:
        pass


# The one transport-layer frame: what the accept loop sends a peer over the cap.
_BUSY_MESSAGE = decode_body(BUSY_FRAME[HEADER.size:])


def _handler_reply(response: Message) -> Message:
    """A handler reply may not speak for the transport layer.

    ``layer: "transport"`` is reserved for the accept-path busy frame, which the client
    reads as "not delivered". A handler that saw the request and answered with that layer
    (a Stop that ran, then said busy) would make the client report not delivered and
    invite a duplicate retry. Such an error is rewritten to ``layer: "handler"`` and
    ``delivered: True``; the code is kept.
    """
    error = response.get("error")
    if isinstance(error, dict) and error.get("layer") == "transport":
        return {**response, "error": {**error, "layer": "handler", "delivered": True}}
    return response


def _is_busy(response: Message) -> bool:
    # Only the exact accept-path busy frame counts; handler replies can never match it
    # because ``_serve`` rewrites any handler error that claims the transport layer.
    return response == _BUSY_MESSAGE


def _hold_peer_open(conn: socket.socket) -> None:
    """Keep one extra fd until a short deadline so a peer still writing is not reset.

    The worker has already answered and is about to release the connection slot.
    Arriving bytes do not extend the deadline.
    """
    if not _peer_holds.acquire(blocking=False):
        return
    try:
        held = conn.dup()
    except OSError:
        _peer_holds.release()
        return

    def drain() -> None:
        try:
            deadline = time.monotonic() + _PEER_HOLD_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                held.settimeout(remaining)
                try:
                    chunk = held.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
        finally:
            held.close()
            _peer_holds.release()

    thread = threading.Thread(target=drain, name="control-transport-hold", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        held.close()
        _peer_holds.release()


def _drop_unencodable_id(message: Message) -> Message:
    """Omit an id that ``encode_frame`` cannot write. The rest of the reply stands."""
    if "id" not in message or id_reencodes(message["id"]):
        return message
    return {key: value for key, value in message.items() if key != "id"}


def _send_quietly(conn: socket.socket, message: Message) -> None:
    try:
        write_frame(conn, _drop_unencodable_id(message))
    except (OSError, FrameError, ValueError):
        pass


def _correlated_error(code: str, request: Message) -> Message:
    """A refusal that names ``code`` and echoes the request id when the request had one."""
    body: Message = {"ok": False, "error": {"code": code, "retryable": False}}
    if "id" in request:
        body["id"] = request["id"]
    return body


class UnixControlClient:
    """Connects to a ``UnixControlServer`` and checks the server's uid is ours too.

    The mutual check stops a client from talking to a socket another uid planted.
    """

    def __init__(
        self,
        path: object,
        *,
        timeout_s: float = 5.0,
        peer_cred: PeerCredGetter = get_peer_cred,
        max_frame_bytes: int = MAX_FRAME_BYTES,
    ) -> None:
        self.path = refuse_remote_bind(path)
        self._timeout_s = timeout_s
        self._max_frame_bytes = max_frame_bytes
        self._lock = threading.Lock()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout_s)
            sock.connect(self.path)
            check_peer(sock, peer_cred, os.geteuid())
        except BaseException:
            sock.close()
            raise
        self._sock: socket.socket | None = sock

    def request(self, message: Message) -> Message:
        """Send one frame and return its reply; any failure closes the client for good.

        A timeout or partial read leaves the stream mid-frame, so the next read would hand
        back an earlier request's late reply (a Stop's answer as a ping's). The socket is
        closed instead, and a reply whose ``id`` differs from the request's is refused.
        ``timeout_s`` bounds the whole reply, not each ``recv``. A server at its connection
        cap raises ``TransportBusy``: the request was not delivered.
        """
        with self._lock:
            sock = self._sock
            if sock is None:
                raise ConnectionError("client closed")
            try:
                deadline = time.monotonic() + self._timeout_s
                sock.settimeout(self._timeout_s)
                try:
                    write_frame(sock, message, max_bytes=self._max_frame_bytes)
                except OSError:
                    # A busy server may have answered and hung up before our write landed.
                    if _pending_busy(sock):
                        raise TransportBusy("busy: server at its connection cap; request not delivered") from None
                    raise
                response = read_frame(sock, max_bytes=self._max_frame_bytes, deadline=deadline)
                if response is None:
                    raise ConnectionError("server closed the connection")
                if _is_busy(response):
                    raise TransportBusy("busy: server at its connection cap; request not delivered")
                if "id" in message and not same_id(response.get("id"), message["id"]):
                    raise FrameError("reply_id_mismatch")
            except BaseException:
                self._sock = None
                sock.close()
                raise
        return response

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            sock.close()

    def __enter__(self) -> UnixControlClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _pending_busy(sock: socket.socket) -> bool:
    try:
        reply = read_frame(sock, deadline=time.monotonic() + 0.5)
    except (OSError, FrameError):
        return False
    return reply is not None and _is_busy(reply)
