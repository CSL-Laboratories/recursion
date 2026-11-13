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
while an ``O_PATH`` fd holds that inode and ``renameat2(RENAME_NOREPLACE)``
can move the directory entry aside. The hidden name is no longer than the
socket's own name. The recheck opens that name with ``O_PATH|O_NOFOLLOW``,
checks the fd is the inode just verified, and connects to
``/proc/self/fd/<that fd>``. A symlink or a directory round-trip undone
inside connect cannot redirect that fd. After ``ECONNREFUSED`` the name is
lstat'd again (``AT_SYMLINK_NOFOLLOW``) and unlinked only when it is still
that inode. Anything else moves the hidden name back with
``RENAME_NOREPLACE``. If that move back fails, the error names the hidden
entry. Without ``renameat2``, without ``O_PATH``, or when that proc path
cannot be used, the socket stays in place.

``start`` also removes our own earlier hidden sockets that are still
``ECONNREFUSED``, and only when the name is one the quarantine generator
can produce: the public name's byte length, its prefix, and a suffix from
``token_hex`` (``0-9a-f``). A dead ``.q`` prefix of 6 bytes, or that prefix
plus one extra character, is left alone. Non-sockets and live listeners
stay.

A socket that ``listen``s on the hidden name after that second connect
returns and before the hidden name is unlinked can still lose that name.
That window remains. Names shorter than 8 bytes cannot be quarantined and
are not reaped. Platforms without ``renameat2`` or ``O_PATH`` keep the
socket. ``RENAME_NOREPLACE`` closes the earlier window where a quarantine
name was chosen and then replaced.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import ipaddress
import os
import re
import secrets
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


# O_PATH pins and the directory fds used to rename them. Fork copies the fd
# table; O_CLOEXEC does not run then. ``_PIN_FDS_LOCK`` is held across fork so
# the child closes exactly the fds that were open, then drops the lock.
_PIN_FDS: set[int] = set()
_PIN_FDS_LOCK = threading.Lock()


def _remember_pin(fd: int) -> int:
    with _PIN_FDS_LOCK:
        _PIN_FDS.add(fd)
    return fd


def _close_pin(fd: int | None) -> None:
    if not isinstance(fd, int):
        return
    with _PIN_FDS_LOCK:
        _PIN_FDS.discard(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def _pins_before_fork() -> None:
    _PIN_FDS_LOCK.acquire()


def _pins_after_fork_parent() -> None:
    _PIN_FDS_LOCK.release()


def _drop_inherited_fds() -> None:
    """Close lock fds and inode pins inherited across fork.

    ``O_CLOEXEC`` does not run on fork. The child must not keep the parent's
    flock or an ``O_PATH`` pin of the bound socket.
    """
    try:
        pins = list(_PIN_FDS)
        _PIN_FDS.clear()
        for server in list(_LIVE_SERVERS):
            lock_fd = server._lock_fd
            server._lock_fd = None
            pin_fd = server._pin_fd
            server._pin_fd = None
            for fd in (lock_fd, pin_fd):
                if isinstance(fd, int):
                    pins.append(fd)
        seen: set[int] = set()
        for fd in pins:
            if fd in seen:
                continue
            seen.add(fd)
            try:
                os.close(fd)
            except OSError:
                pass
    finally:
        try:
            _PIN_FDS_LOCK.release()
        except RuntimeError:
            pass


if hasattr(os, "register_at_fork"):  # pragma: no branch - POSIX
    os.register_at_fork(
        before=_pins_before_fork,
        after_in_parent=_pins_after_fork_parent,
        after_in_child=_drop_inherited_fds,
    )


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


def _can_pin() -> bool:
    """True when an ``O_PATH`` fd can hold an inode and a directory for rename."""
    return (
        getattr(os, "O_PATH", None) is not None
        and getattr(os, "O_NOFOLLOW", None) is not None
        and getattr(os, "O_DIRECTORY", None) is not None
    )


def _open_inode_pin(path: str) -> int | None:
    """Fd that holds ``path``'s inode until closed, or ``None`` if the name is gone.

    ``O_PATH|O_NOFOLLOW`` does not connect. With both flags a symlink yields an
    fd for the link itself; the caller rejects that from ``fstat``. The fd is
    what stops the inode being reused after the name is removed. It is recorded
    so a forked child closes its dup.
    """
    try:
        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InsecureSocketPath(f"{path} exists and is not our socket") from exc
    return _remember_pin(fd)


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


# renameat2(RENAME_NOREPLACE). Linux rejects a replace; sockets cannot be link()ed.
_RENAME_NOREPLACE = 1
_renameat2 = None
_renameat2_ready = False


def _load_renameat2():
    """``renameat2`` from libc, or ``None`` when this platform has no such symbol."""
    global _renameat2, _renameat2_ready
    if _renameat2_ready:
        return _renameat2
    _renameat2_ready = True
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.renameat2
    except (AttributeError, OSError):
        _renameat2 = None
        return None
    fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    fn.restype = ctypes.c_int
    _renameat2 = fn
    return fn


def _rename_noreplace(dfd: int, src: str, dst: str) -> str:
    """Move ``src`` onto ``dst`` inside ``dfd`` without replacing ``dst``.

    ``"moved"`` when the name moved. ``"exists"`` when ``dst`` is already
    there (``EEXIST``), including when it is a directory. ``"gone"`` when
    ``src`` was already absent. ``"unsupported"`` when ``renameat2`` cannot
    be used. Any other errno is raised and the name is not moved. There is
    no ``os.rename`` fallback: that call replaces an entry created after the
    check.
    """
    fn = _load_renameat2()
    if fn is None:
        return "unsupported"
    ctypes.set_errno(0)
    rc = fn(dfd, os.fsencode(src), dfd, os.fsencode(dst), _RENAME_NOREPLACE)
    if rc == 0:
        return "moved"
    err = ctypes.get_errno()
    if err in (errno.EEXIST, errno.ENOTEMPTY):
        return "exists"
    if err == errno.ENOENT:
        return "gone"
    if err in (errno.ENOSYS, errno.EINVAL, getattr(errno, "ENOTSUP", 0), getattr(errno, "EOPNOTSUPP", 0)):
        return "unsupported"
    if err == 0:
        raise OSError(errno.EIO, "renameat2 failed", src)
    if err in (errno.EACCES, errno.EPERM):
        raise PermissionError(err, os.strerror(err), src)
    raise OSError(err, os.strerror(err), src)


def _pinned_match(listed: os.stat_result, held: os.stat_result) -> bool:
    """True when ``listed`` is the socket inode ``held`` (the pin's fstat)."""
    return (
        not stat.S_ISLNK(listed.st_mode)
        and not stat.S_ISLNK(held.st_mode)
        and stat.S_ISSOCK(listed.st_mode)
        and stat.S_ISSOCK(held.st_mode)
        and listed.st_uid == os.geteuid()
        and held.st_uid == os.geteuid()
        and (listed.st_dev, listed.st_ino) == (held.st_dev, held.st_ino)
    )


def _open_parent_dir(directory: str) -> int:
    """``O_PATH|O_DIRECTORY`` fd for dir-relative rename, lstat and unlink."""
    flags = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return _remember_pin(os.open(directory, flags))


# ".q" plus 4 hex chars of the socket name, and at least 2 hex chars that vary.
# A shorter socket name cannot be quarantined without growing the path.
_QUARANTINE_PREFIX_LEN = 6
_QUARANTINE_SUFFIX_MIN = 2


def _quarantine_prefix(socket_name: str) -> str | None:
    """``.q`` plus a short tag of ``socket_name``, or ``None`` if it cannot fit.

    The whole quarantine name stays within ``socket_name``'s byte length, so a
    recheck ``connect`` cannot exceed the path length that was already accepted.
    """
    raw = os.fsencode(socket_name)
    prefix = ".q" + hashlib.sha256(raw).hexdigest()[:4]
    if len(prefix) != _QUARANTINE_PREFIX_LEN:
        return None
    if len(raw) < len(prefix) + _QUARANTINE_SUFFIX_MIN:
        return None
    return prefix


def _quarantine_name(socket_name: str) -> str | None:
    """One hidden name, the same byte length as ``socket_name``.

    The suffix is ``secrets.token_hex``: lowercase ``0-9a-f`` only.
    """
    prefix = _quarantine_prefix(socket_name)
    if prefix is None:
        return None
    limit = len(os.fsencode(socket_name))
    suffix_len = limit - len(prefix)
    suffix = secrets.token_hex((suffix_len + 1) // 2)[:suffix_len]
    name = prefix + suffix
    if len(os.fsencode(name)) != limit or name in (".", ".."):
        return None
    return name


# ``secrets.token_hex`` alphabet. Reap must not accept a wider set.
_QUARANTINE_ALPHABET = frozenset("0123456789abcdef")


def _is_quarantine_name(entry: str, socket_name: str) -> bool:
    """True when ``entry`` is a name ``_quarantine_name`` can return.

    Same byte length as ``socket_name``, that name's prefix, and a suffix of
    at least two ``token_hex`` characters. The 6-byte prefix alone, or the
    prefix plus one other character, is not such a name.
    """
    prefix = _quarantine_prefix(socket_name)
    if prefix is None or entry in (socket_name, ".", ".."):
        return False
    if len(os.fsencode(entry)) != len(os.fsencode(socket_name)):
        return False
    if not entry.startswith(prefix):
        return False
    suffix = entry[len(prefix):]
    if len(suffix) < _QUARANTINE_SUFFIX_MIN:
        return False
    return all(ch in _QUARANTINE_ALPHABET for ch in suffix)


def _can_quarantine(directory: str, socket_name: str) -> bool:
    """True when a hidden name can be moved aside without growing the path."""
    if _load_renameat2() is None:
        return False
    prefix = _quarantine_prefix(socket_name)
    if prefix is None:
        return False
    limit = len(os.fsencode(socket_name))
    sample = prefix + ("0" * (limit - len(prefix)))
    return len(os.fsencode(os.path.join(directory, sample))) <= _MAX_SUN_PATH


def _same_dir(directory: str, ident: tuple[int, int]) -> bool:
    """True when ``directory`` still resolves to the inode ``ident``."""
    try:
        st = os.stat(directory)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and (st.st_dev, st.st_ino) == ident


def _restore_noreplace(dfd: int, qname: str, name: str) -> bool:
    """Move ``qname`` back onto ``name`` without replacing ``name``.

    False means ``qname`` is still there. ``os.rename`` is not used.
    """
    result = _rename_noreplace(dfd, qname, name)
    return result in ("moved", "gone")


def _left_in_place(path: str) -> str:
    """Refusal that tells the operator the socket was not removed."""
    code = errno.errorcode.get(errno.ECONNREFUSED, str(errno.ECONNREFUSED))
    return f"{path} is not connectable ({code}). Left in place. Remove this socket if it is stale."


def _name_is_inode(dfd: int, name: str, verified: os.stat_result) -> bool:
    """True when ``name`` in ``dfd`` still lstats as ``verified``.

    ``os.lstat`` with ``dir_fd`` is ``fstatat(..., AT_SYMLINK_NOFOLLOW)``.
    A symlink at ``name`` does not match.
    """
    try:
        listed = os.lstat(name, dir_fd=dfd)
    except OSError:
        return False
    return _pinned_match(listed, verified)


def _open_matching_socket(dfd: int, name: str, verified: os.stat_result) -> int:
    """``O_PATH|O_NOFOLLOW`` fd for ``name`` when it is the inode ``verified``.

    The caller closes the fd. A symlink, a different inode, or an open that
    cannot be made raises and the caller does not unlink.
    """
    if getattr(os, "O_PATH", None) is None or getattr(os, "O_NOFOLLOW", None) is None:
        raise InsecureSocketPath(_left_in_place(name))
    flags = os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, dir_fd=dfd)
    except OSError as exc:
        raise InsecureSocketPath(_left_in_place(name)) from exc
    fd = _remember_pin(fd)
    try:
        held = os.fstat(fd)
    except OSError as exc:
        _close_pin(fd)
        raise InsecureSocketPath(_left_in_place(name)) from exc
    if not _pinned_match(held, verified):
        _close_pin(fd)
        raise InsecureSocketPath(f"{name} exists and is not our socket")
    return fd


def _proc_inode_path(fd: int) -> str | None:
    """``/proc/self/fd/<fd>`` when that string can be passed to ``connect``."""
    proc = f"/proc/self/fd/{fd}"
    if len(os.fsencode(proc)) > _MAX_SUN_PATH:
        return None
    try:
        st = os.stat("/proc/self/fd")
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return None
    return proc


def _recheck_hidden(dfd: int, directory: str, qname: str, verified: os.stat_result) -> int | None:
    """Probe the inode ``verified``, not the directory entry ``qname``.

    Opens ``qname`` with ``O_PATH|O_NOFOLLOW`` on ``dfd`` and connects to
    ``/proc/self/fd/<that fd>``. Connect through that fd sees the inode: a
    live listener succeeds and a dead socket returns ``ECONNREFUSED``. A
    final-component symlink or a directory rename undone inside connect
    cannot change which inode is probed. After ``ECONNREFUSED``, ``qname``
    is lstat'd again and must still be ``verified``; a different inode is
    not a finished reclaim. If ``O_PATH`` or the proc path cannot be used,
    this raises and the caller leaves the socket in place. The parent
    directory's ``(dev, ino)`` is checked immediately before and after the
    connect. ``/proc/net/unix`` is not consulted.
    """
    ident = os.fstat(dfd)
    held_dir = (ident.st_dev, ident.st_ino)
    if not _same_dir(directory, held_dir):
        raise InsecureSocketPath(f"{directory} changed during reclaim")
    full = os.path.join(directory, qname)
    fd = _open_matching_socket(dfd, qname, verified)
    try:
        proc = _proc_inode_path(fd)
        if proc is None:
            raise InsecureSocketPath(_left_in_place(full))
        err = _probe_listener(proc)
    finally:
        _close_pin(fd)
    if not _same_dir(directory, held_dir) or err == errno.ENOENT:
        raise InsecureSocketPath(f"{directory} changed during reclaim")
    if err == errno.ECONNREFUSED and not _name_is_inode(dfd, qname, verified):
        raise InsecureSocketPath(f"{directory} changed during reclaim")
    return err


def _quarantine_move(dfd: int, src: str, socket_name: str) -> str | None:
    """Rename ``src`` to a fresh hidden name. ``None`` if ``src`` is already gone.

    ``EEXIST`` picks another name. Any other failure leaves ``src`` where it
    is and raises. ``renameat2`` is the only move.
    """
    if _load_renameat2() is None or _quarantine_prefix(socket_name) is None:
        raise InsecureSocketPath(
            f"{src} is not connectable (ECONNREFUSED). Left in place. Remove this socket if it is stale."
        )
    for _ in range(16):
        dst = _quarantine_name(socket_name)
        if dst is None or dst == src:
            break
        result = _rename_noreplace(dfd, src, dst)
        if result == "moved":
            return dst
        if result == "gone":
            return None
        if result == "exists":
            continue
        if result == "unsupported":
            raise InsecureSocketPath(
                f"{src} is not connectable (ECONNREFUSED). Left in place. Remove this socket if it is stale."
            )
    raise InsecureSocketPath(f"{src} could not be quarantined. Left in place.")


def _unlink_pinned_in(
    dfd: int,
    directory: str,
    name: str,
    path: str,
    pin: int,
    *,
    socket_name: str,
    recheck: bool,
) -> None:
    """Quarantine ``name`` inside ``dfd`` and unlink it only when it is still ``pin``.

    ``socket_name`` sizes the hidden name. After a successful move, this
    always tries to move the hidden name back unless the unlink of that
    verified name succeeded. A failed move-back raises ``InsecureSocketPath``
    and names the hidden entry.

    A socket that ``listen``s after the recheck connect and before the unlink
    can still lose the hidden name. That is the remaining window.
    """
    if not _can_quarantine(directory, socket_name):
        _refuse_unreclaimed(path, errno.ECONNREFUSED)
        return
    held = os.fstat(pin)
    qname: str | None = None
    try:
        qname = _quarantine_move(dfd, name, socket_name)
        if qname is None:
            return
        try:
            listed = os.lstat(qname, dir_fd=dfd)
        except FileNotFoundError:
            qname = None
            return
        if not _pinned_match(listed, held):
            raise InsecureSocketPath(f"{path} exists and is not our socket")
        if recheck:
            still = _recheck_hidden(dfd, directory, qname, held)
            if still != errno.ECONNREFUSED:
                if still is None:
                    raise InsecureSocketPath(f"{path} is live: another server is listening")
                # ENOENT is a parent mismatch, not a finished reclaim. Restore, then refuse.
                _refuse_unreclaimed(path, still)
                raise InsecureSocketPath(f"{directory} changed during reclaim")
            # The connect was bound to ``held``. Unlink only if the name still is.
            if not _name_is_inode(dfd, qname, held):
                raise InsecureSocketPath(f"{directory} changed during reclaim")
        try:
            os.unlink(qname, dir_fd=dfd)
        except FileNotFoundError:
            qname = None
            return
        qname = None
    finally:
        if qname is not None:
            qpath = os.path.join(directory, qname)
            if not _restore_noreplace(dfd, qname, name):
                raise InsecureSocketPath(f"{path} could not be restored. Left at {qpath}.")


def _unlink_pinned(path: str, pin: int, *, recheck: bool = False) -> None:
    """Remove ``path`` only when its quarantined entry is the inode ``pin`` holds.

    The move is ``renameat2(RENAME_NOREPLACE)`` inside an ``O_PATH`` directory
    fd. A listener that binds ``path`` after that move gets a new directory
    entry and is not unlinked. The hidden name is unlinked only when its
    ``lstat`` matches ``fstat(pin)``. Any other inode, a live recheck, or a
    parent that no longer matches the directory fd is moved back. When the
    original name is already taken, the hidden name is left in place and
    ``InsecureSocketPath`` names it.

    ``recheck`` connects through an ``O_PATH`` fd of the hidden inode before
    that unlink, then lstats the name again. Close does not recheck: the
    listen fd is already shut, and an inherited dup must not keep the name.

    A socket that ``listen``s after the recheck connect and before the hidden
    name is unlinked can still lose that hidden name. That is the remaining
    window.
    """
    directory, name = os.path.split(path)
    if not name or name in (".", "..") or not directory:
        raise InsecureSocketPath(f"{path} exists and is not our socket")
    dfd = _open_parent_dir(directory)
    try:
        _unlink_pinned_in(dfd, directory, name, path, pin, socket_name=name, recheck=recheck)
    finally:
        _close_pin(dfd)


def _clear_stale_socket(path: str) -> None:
    """Drop a socket we own only when connect proves nothing is listening.

    The inode is pinned before the probe and held until the quarantine check
    returns. ``ECONNREFUSED`` means this namespace has no listener on that
    inode, so ``renameat2(RENAME_NOREPLACE)`` may move it aside. The hidden
    name is probed through an ``O_PATH`` fd of that same inode, not by the
    path. A replacement bound while the pin is open has a different inode:
    the quarantine entry does not match and is moved back. Without ``O_PATH``,
    without ``renameat2``, when the proc path cannot be used, or when the
    socket name cannot hold a hidden name no longer than itself,
    ``ECONNREFUSED`` is left in place rather than reclaimed.

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
    if not _can_pin():
        # No fd can hold the inode, so a reused number could match a new listener.
        st = _stat_our_socket(path)
        if st is None:
            return
        _refuse_unreclaimed(path, _probe_listener(path))
        return
    # Pin before any lstat. A socket bound after this open cannot reuse the
    # inode, including one that has bind()ed and not yet listen()ed.
    pin = _open_inode_pin(path)
    if pin is None:
        return
    try:
        held = os.fstat(pin)
        if stat.S_ISLNK(held.st_mode) or not stat.S_ISSOCK(held.st_mode) or held.st_uid != os.geteuid():
            raise InsecureSocketPath(f"{path} exists and is not our socket")
        err = _probe_listener(path)
        if err == errno.ECONNREFUSED:
            directory, name = os.path.split(path)
            # No safe hidden name: same refusal as a platform with no O_PATH.
            if not directory or not _can_quarantine(directory, name):
                _refuse_unreclaimed(path, err)
                return
            _unlink_pinned(path, pin, recheck=True)
            return
        _refuse_unreclaimed(path, err)
    finally:
        _close_pin(pin)


def _reap_quarantine(directory: str, socket_name: str) -> None:
    """Remove our abandoned quarantine sockets. Leave everything else alone.

    Called from ``start`` while the directory flock is held. An entry is
    removed only when its name is one ``_quarantine_name`` can produce, it
    is a socket we own, and a connect to that inode returns ``ECONNREFUSED``.
    A shorter name that only starts with the prefix is left alone, as is a
    name of the right length whose suffix is not ``token_hex``. Non-sockets,
    symlinks, other users' sockets, and live listeners stay. Removal goes
    through the same pin, inode recheck and ``RENAME_NOREPLACE`` restore as
    a stale public socket.
    """
    if not _can_pin() or not _can_quarantine(directory, socket_name):
        return
    prefix = _quarantine_prefix(socket_name)
    if prefix is None:
        return
    try:
        dfd = _open_parent_dir(directory)
    except OSError:
        return
    try:
        ident = os.fstat(dfd)
        held_dir = (ident.st_dev, ident.st_ino)
        if not _same_dir(directory, held_dir):
            return
        try:
            names = [entry.name for entry in os.scandir(f"/proc/self/fd/{dfd}")]
        except OSError:
            return
        if not _same_dir(directory, held_dir):
            return
        for entry in names:
            if not _is_quarantine_name(entry, socket_name):
                continue
            try:
                listed = os.lstat(entry, dir_fd=dfd)
            except OSError:
                continue
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISSOCK(listed.st_mode) or listed.st_uid != os.geteuid():
                continue
            try:
                still = _recheck_hidden(dfd, directory, entry, listed)
            except (InsecureSocketPath, OSError):
                continue
            if still != errno.ECONNREFUSED:
                continue
            try:
                pin = os.open(entry, os.O_PATH | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
            except OSError:
                continue
            pin = _remember_pin(pin)
            try:
                try:
                    held = os.fstat(pin)
                except OSError:
                    continue
                if not _pinned_match(listed, held):
                    continue
                full = os.path.join(directory, entry)
                try:
                    _unlink_pinned_in(
                        dfd, directory, entry, full, pin, socket_name=socket_name, recheck=True
                    )
                except (InsecureSocketPath, OSError):
                    pass
            finally:
                _close_pin(pin)
    finally:
        _close_pin(dfd)


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
        self._pin_fd: int | None = None
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
                # Under the flock: drop hidden sockets a failed restore left behind,
                # then reclaim the public name if it is still ECONNREFUSED.
                _reap_quarantine(self.directory, os.path.basename(self._path))
                _clear_stale_socket(self._path)
                # fstat(fd) is the sockfs inode, not the directory entry, so it cannot
                # name the file we bound. Record that entry, then chmod it without
                # following a symlink planted in between: a path swap must not change
                # some other file's mode or become the inode we listen under.
                sock.bind(self._path)
                # Pin immediately. A socket that bind()s in this gap and reuses
                # the inode is the one fstat names; anything bound after the pin
                # cannot reuse it. The pin stays open for the server's life so
                # close() can quarantine this inode.
                pin = _open_inode_pin(self._path) if _can_pin() else None
                if _can_pin() and pin is None:
                    raise InsecureSocketPath(f"{self._path} changed under us after bind")
                self._pin_fd = pin
                if pin is not None:
                    held = os.fstat(pin)
                    if stat.S_ISLNK(held.st_mode) or not stat.S_ISSOCK(held.st_mode) or held.st_uid != self._uid:
                        raise InsecureSocketPath(f"{self._path} changed under us after bind")
                    bound_id = (held.st_dev, held.st_ino)
                try:
                    before = os.lstat(self._path)
                except FileNotFoundError as exc:
                    raise InsecureSocketPath(f"{self._path} changed under us after bind") from exc
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISSOCK(before.st_mode) or before.st_uid != self._uid:
                    raise InsecureSocketPath(f"{self._path} changed under us after bind")
                if bound_id is None:
                    bound_id = (before.st_dev, before.st_ino)
                elif (before.st_dev, before.st_ino) != bound_id:
                    raise InsecureSocketPath(f"{self._path} changed under us after bind")
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
                pin = self._pin_fd
                self._pin_fd = None
                try:
                    if pin is not None:
                        _unlink_pinned(self._path, pin)
                    elif bound_id is not None and not _can_pin():
                        _unlink_inode(self._path, bound_id)
                finally:
                    _close_pin(pin)
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
        """Close the listen socket, quarantine that inode, then drop the flock.

        The ``O_PATH`` pin taken at bind is held across the rename, so a
        successor that reused the inode does not match and is moved back.
        ``InsecureSocketPath`` is raised when the name was not our inode. The
        flock is released before that exception leaves: once it is gone, a
        peer may be woken.
        """
        with self._lock:
            sock, self._sock = self._sock, None
            lock_fd, self._lock_fd = self._lock_fd, None
            bound, self._bound = self._bound, None
            pin, self._pin_fd = self._pin_fd, None
        try:
            if sock is not None:
                sock.close()
                if pin is not None:
                    _unlink_pinned(self._path, pin)
                elif bound is not None and not _can_pin():
                    _unlink_inode(self._path, bound)
        finally:
            _close_pin(pin)
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
