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

A connect that fails with ``EACCES`` or ``EPERM`` does not reclaim the socket.
Owner decision: that errno is not proof the file is stale, so a dead mode-000
socket stays until the operator removes it. ``ECONNREFUSED`` reclaims only
while an ``O_PATH`` fd holds that inode, and only when the path still names it.
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
import weakref
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
# and frees its slot; this extra fd only reads that tail, on one deadline.
# The write side is shut first, so a later request on the same socket sees EOF
# instead of sitting in this drain.
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


# Servers whose lock fd a forked child must drop. flock is on the open file
# description, so the child's dup keeps the lock after the parent closes its own.
_LIVE_SERVERS: weakref.WeakSet = weakref.WeakSet()


def _drop_lock_after_fork() -> None:
    """Close lock fds inherited across fork. ``O_CLOEXEC`` does not run on fork."""
    for server in list(_LIVE_SERVERS):
        fd = server._lock_fd
        server._lock_fd = None
        if not isinstance(fd, int):
            continue
        try:
            os.close(fd)
        except OSError:
            pass


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX
    os.register_at_fork(after_in_child=_drop_lock_after_fork)


def _unlink_inode(path: str, ident: tuple[int, int] | None) -> None:
    """Unlink ``path`` only when it is still the inode ``ident`` and not a symlink."""
    if ident is None:
        return
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode) or (st.st_dev, st.st_ino) != ident:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _stat_our_socket(path: str) -> os.stat_result | None:
    """The socket at ``path`` when it is ours. ``None`` when the path is gone."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid():
        raise InsecureSocketPath(f"{path} exists and is not our socket")
    return st


def _probe_listener(path: str) -> int | None:
    """``None`` when a listener is present. Otherwise ``connect``'s errno.

    A full accept queue fails this non-blocking connect with ``BlockingIOError``
    (``EAGAIN``). That listener is live: return ``None`` so the caller does not
    unlink. Errnos other than refused, missing, and permission-denied propagate.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        try:
            probe.connect(path)
        except BlockingIOError:
            return None
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return None
            if exc.errno in (errno.ECONNREFUSED, errno.ENOENT, errno.EACCES, errno.EPERM):
                return exc.errno
            raise
    finally:
        probe.close()
    return None


def _open_inode_pin(path: str) -> int | None:
    """Fd that holds ``path``'s inode until closed, or ``None`` if the name is gone.

    ``O_PATH|O_NOFOLLOW`` does not connect. With both flags a symlink yields an
    fd for the link itself; the caller rejects that from ``fstat``. The fd is
    what stops the inode being reused after the name is removed.
    """
    try:
        return os.open(path, os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InsecureSocketPath(f"{path} exists and is not our socket") from exc


def _refuse_unreclaimed(path: str, err: int | None) -> None:
    """Raise for a live or unconnectable socket. A missing name returns. No unlink."""
    if err is None:
        raise InsecureSocketPath(f"{path} is live: another server is listening")
    if err == errno.ENOENT:
        return
    code = errno.errorcode.get(err, str(err))
    raise InsecureSocketPath(
        f"{path} is not connectable ({code}). Left in place. Remove this socket if it is stale."
    )


def _unlink_our_socket(path: str, pin: int) -> None:
    """Unlink ``path`` only when a fresh lstat still names the inode ``pin`` holds.

    ``pin`` is the ``O_PATH`` fd opened before the probe. That inode cannot be
    reused, so a listener bound at ``path`` after the old name was removed does
    not match ``fstat(pin)`` and is left in place. ``_unlink_inode`` checks again.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    held = os.fstat(pin)
    if (
        stat.S_ISLNK(st.st_mode)
        or not stat.S_ISSOCK(st.st_mode)
        or not stat.S_ISSOCK(held.st_mode)
        or st.st_uid != os.geteuid()
        or (st.st_dev, st.st_ino) != (held.st_dev, held.st_ino)
    ):
        raise InsecureSocketPath(f"{path} exists and is not our socket")
    _unlink_inode(path, (held.st_dev, held.st_ino))


def _clear_stale_socket(path: str) -> None:
    """Drop a socket we own only when connect proves nothing is listening.

    The inode is pinned before the probe and held until the unlink check
    returns. ``ECONNREFUSED`` means this namespace has no listener on that
    inode, so the inode-checked unlink may reclaim it. A replacement bound
    in between has a different inode while the pin is open, and is refused.
    Without ``O_PATH`` the inode cannot be held, so ``ECONNREFUSED`` is left
    in place rather than reclaimed.

    ``EACCES`` and ``EPERM`` do not reclaim: a live mode-000 or mode-070
    listener fails connect the same way a dead socket does. ``/proc/net/unix``
    cannot separate those. Another net namespace, a bind mount in another
    mount namespace, a symlink or ``..`` alias, and a path ``str.splitlines``
    would split (newline, vertical tab) all omit the listener while it is
    still live. Do not chmod on this path either: chmod would leave a listener
    at 0600, and ``follow_symlinks=False`` still walks a parent symlink.

    A full accept queue surfaces as ``EAGAIN`` / ``BlockingIOError``. That is
    a live listener: the same refusal as a successful connect, and no unlink.

    Owner decision: never unlink on ``EACCES`` or ``EPERM``. A dead mode-000
    socket stays until the operator removes it.
    """
    st = _stat_our_socket(path)
    if st is None:
        return
    if getattr(os, "O_PATH", None) is None or getattr(os, "O_NOFOLLOW", None) is None:
        # No fd can hold the inode, so a reused number could match a new listener.
        _refuse_unreclaimed(path, _probe_listener(path))
        return
    pin = _open_inode_pin(path)
    if pin is None:
        return
    try:
        held = os.fstat(pin)
        if (
            stat.S_ISLNK(held.st_mode)
            or not stat.S_ISSOCK(held.st_mode)
            or held.st_uid != os.geteuid()
            or (held.st_dev, held.st_ino) != (st.st_dev, st.st_ino)
        ):
            raise InsecureSocketPath(f"{path} exists and is not our socket")
        err = _probe_listener(path)
        if err == errno.ECONNREFUSED:
            _unlink_our_socket(path, pin)
            return
        _refuse_unreclaimed(path, err)
    finally:
        os.close(pin)


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
        _LIVE_SERVERS.add(self)

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
            # Published before bind so a fork during startup drops this fd in the child.
            self._lock_fd = lock_fd
            sock: socket.socket | None = None
            bound_id: tuple[int, int] | None = None
            try:
                # socket() is inside the cleanup: a failure here must not leave the flock held.
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                _clear_stale_socket(self._path)
                # fstat(fd) is the sockfs inode, not the directory entry, so it cannot
                # name the file we bound. Record that entry, then chmod it without
                # following a symlink planted in between: a path swap must not change
                # some other file's mode or become the inode we listen under.
                sock.bind(self._path)
                try:
                    before = os.lstat(self._path)
                except FileNotFoundError as exc:
                    raise InsecureSocketPath(f"{self._path} changed under us after bind") from exc
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISSOCK(before.st_mode) or before.st_uid != self._uid:
                    raise InsecureSocketPath(f"{self._path} changed under us after bind")
                bound_id = (before.st_dev, before.st_ino)
                try:
                    os.chmod(self._path, 0o600, follow_symlinks=False)
                except (OSError, NotImplementedError) as exc:
                    raise InsecureSocketPath(f"{self._path} changed under us after bind") from exc
                listed = os.lstat(self._path)
                if (
                    stat.S_ISLNK(listed.st_mode)
                    or not stat.S_ISSOCK(listed.st_mode)
                    or listed.st_uid != self._uid
                    or (listed.st_dev, listed.st_ino) != bound_id
                    or stat.S_IMODE(listed.st_mode) & 0o077
                ):
                    raise InsecureSocketPath(f"{self._path} changed under us after bind")
                sock.listen(socket.SOMAXCONN)
                sock.settimeout(_ACCEPT_POLL_S)
                self._sock = sock
                self._bound = bound_id
                self._thread = threading.Thread(target=self._accept_loop, name="control-transport-accept", daemon=True)
                self._thread.start()
            except BaseException:
                if sock is not None:
                    sock.close()
                self._sock = None
                self._bound = None
                self._thread = None
                self._lock_fd = None
                _unlink_inode(self._path, bound_id)
                os.close(lock_fd)
                raise

    def close(self) -> None:
        """Stop accepting and drop live connections under one shared deadline.

        Each join waits only for the time still left on that deadline, so N blocked
        handlers do not cost N timeouts. Handlers still running when the deadline
        passes are cancelled. ``_serve`` does not write a result that arrives after that.

        The flock is released before any peer-observable shutdown. ``shutdown``
        wakes a client blocked in ``read``, and that client may start a successor
        before ``close`` returns. Once the flock is gone that successor can also
        bind while a handler from this server is still running: the lock
        serialises listeners, not a handler already inside ``_serve``. That
        overlap is accepted. This close still shuts the old connections down.
        Joins stay inside this cleanup: a handler that stops the server is
        running on a worker, and joining that thread raises.

        Unlink can fail after the listen socket is closed (the directory is
        not writable). Shutdown, the worker joins and abandon still run, then
        the unlink error propagates.
        """
        self._stop.set()
        thread = self._thread
        try:
            self._requests.begin_close()
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=self._requests.remaining())
            with self._lock:
                workers = list(self._workers)
                conns = list(self._conns)
            try:
                # Drop the listener and the flock before waking peers.
                # Unlink sits in that release and may raise; the flock close
                # is in its own finally, so it has already happened.
                self._release_bound_socket()
            finally:
                for conn in conns:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                for worker in workers:
                    if worker is threading.current_thread() or worker.ident is None:
                        continue
                    worker.join(timeout=self._requests.remaining())
                # The wait is over. Cancel whoever is still inside a handler, even if the clock
                # is a hair short of the deadline, and do not give the rest another timeout.
                self._requests.abandon()
        finally:
            # A failure before the release above still has to drop the flock.
            # Do not shut peers down from here: that would run before the flock
            # is gone when the try did not reach the release.
            self._release_bound_socket()

    def _release_bound_socket(self) -> None:
        """Close the listen socket, unlink that inode, then drop the flock.

        Unlink is inode-checked, so a successor that already bound this path
        stays. The flock is last: once it is gone, a peer may be woken.
        """
        with self._lock:
            sock, self._sock = self._sock, None
            lock_fd, self._lock_fd = self._lock_fd, None
            bound, self._bound = self._bound, None
        try:
            if sock is not None:
                sock.close()
                _unlink_inode(self._path, bound)
        finally:
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

        ``frame_too_large`` shuts the write side after the reply. The next request
        on that socket fails at once. A duplicate fd keeps reading the rest of the
        oversized body so the reply is not reset, and that read does not restart.
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
                # Dup first, so closing this fd cannot reset the peer while the
                # reply is still unread. Then half-close writes: the client reads
                # bad_frame and the next recv is EOF, not a 2s drain of a new request.
                _hold_peer_open(conn)
                try:
                    conn.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
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
        """Write ``message``. False means this connection cannot stay aligned.

        When ``request`` carried an id, that id is copied onto an error, including
        null, so the client that sent it does not see ``reply_id_mismatch``.
        ``encode_frame`` runs before ``sendall``. ``TypeError`` (bytes, set) and
        ``ValueError`` (NaN) mean nothing was written: the client gets
        ``internal_error`` and the connection stays up. An id is echoed only when
        it can be written back. ``OSError`` can land mid-frame, so the connection ends.
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
        except (TypeError, ValueError):
            fallback: Message = {"ok": False, "error": {"code": "internal_error"}}
            if request is not None and "id" in request:
                fallback["id"] = request["id"]
            try:
                write_frame(conn, _drop_unencodable_id(fallback), max_bytes=self._max_frame_bytes)
            except (OSError, FrameError, TypeError, ValueError):
                return False
            return True
        except OSError:
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
    """Read the rest of an oversized body on a duplicate fd. Do not answer it.

    The caller shuts the write side, so this drain cannot be a second request.
    The deadline is fixed. Arriving bytes do not extend it, and the worker has
    already released its connection slot.
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
    except (OSError, FrameError, TypeError, ValueError):
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
        self._io = threading.Lock()
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
        """Send one frame and return its reply. A socket failure closes the client.

        A local encode error writes nothing and leaves the connection open. A timeout
        or partial read leaves the stream mid-frame, so the next read would hand back
        an earlier request's late reply (a Stop's answer as a ping's). The socket is
        closed instead, and a reply whose ``id`` differs from the request's is refused.
        ``timeout_s`` bounds the whole reply, not each ``recv``. ``close`` shuts the
        socket down without waiting out that read. A server at its connection cap
        raises ``TransportBusy``: the request was not delivered.
        """
        # Encode before touching the socket. Anything after sendall can desync the stream.
        payload = encode_frame(message, max_bytes=self._max_frame_bytes)
        with self._io:
            with self._lock:
                sock = self._sock
                if sock is None:
                    raise ConnectionError("client closed")
            try:
                deadline = time.monotonic() + self._timeout_s
                sock.settimeout(self._timeout_s)
                try:
                    sock.sendall(payload)
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
                with self._lock:
                    if self._sock is sock:
                        self._sock = None
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
                raise
        return response

    def close(self) -> None:
        # Do not take ``_io``: an in-flight request holds it across the read, and
        # shutdown is what unblocks that read.
        with self._lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
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
