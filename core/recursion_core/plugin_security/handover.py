"""Hand a connected socket to a plugin host over its channel, and cut it again.

The core, and never a plugin, gives a plugin's host an open connection by passing its
descriptor over the host's channel, a Unix stream socket (SCM_RIGHTS). Nothing here
connects, checks a destination or starts a host: the caller does those and gives this
module the connected socket, the host's channel and the host's process id.

What this module keeps, each with its own test in ``tests/test_handover.py``:

- The receiving side takes a descriptor only with ``socket.recvmsg`` and the flag
  ``MSG_CMSG_CLOEXEC``, so the descriptor is close-on-exec from the moment it arrives
  and a child the host starts never inherits it. The standard library's
  ``socket.recv_fds`` is not used: on 3.12 and 3.14 it drops its ``flags`` argument.
  A message carrying more than one descriptor, or a cut control message
  (``MSG_CTRUNC``), closes every descriptor it brought and is refused.
- The receiving side sets its own blocking mode (the timeout it is given) on every
  socket it receives. The mode lives on the open connection both sides share, so
  without this the sender's mode would leak into the host. The sending side never
  changes the mode of the copy it keeps.
- The sender keeps its own copy of each handed socket in a table per host. A cut is
  ``shutdown(SHUT_RDWR)`` on that copy: the host's read ends with end of file and its
  writes fail with EPIPE. ``close()`` alone would leave the host's copy working.
- A send is capped in time. A full channel answers ``HOST_SLOW``: the socket is not
  handed, not kept, and stays the caller's. A host whose process has ended is refused
  at once and the connection is closed.
- A host in another user namespace than the core's is refused: any process of the
  same user could take a socket out of such a host (``pidfd_getfd``). The host's
  process id comes from the caller, because on a socket pair ``SO_PEERCRED`` names the
  process that made the pair, which is the core itself, not the host.
- Where handing over is not shown (anything but Linux, or a Python without
  ``SCM_RIGHTS`` or ``MSG_CMSG_CLOEXEC``) every call is refused with
  ``platform_unsupported``.

Every refusal is the package's ``Refused``: ``platform_unsupported`` with the words of
``NOT_SHOWN``, or ``handover_refused`` with one of the reasons named below as detail.
"""

from __future__ import annotations

import array
import errno
import os
import select
import socket
import sys
import threading
import time
from dataclasses import dataclass

from .errors import Refused

__all__ = [
    "HANDED", "HANDOVER_SEND_TIMEOUT_S", "HOST_SLOW", "NOT_SHOWN", "PLATFORM_CODE",
    "REFUSED_CODE", "HandoverResult", "HandoverTable", "handover_shown", "receive_handover",
    "require_handover_platform",
]

# First value of the later settings row broker.network.handover_send_timeout_s.
HANDOVER_SEND_TIMEOUT_S = 0.5

PLATFORM_CODE = "platform_unsupported"
NOT_SHOWN = "handing a connection to a plugin host is not shown on this platform"
REFUSED_CODE = "handover_refused"

# Reasons carried as the detail of ``handover_refused``.
HOST_GONE = "host_gone"
OTHER_USER_NAMESPACE = "other_user_namespace"
USER_NAMESPACE_UNREADABLE = "user_namespace_unreadable"
CHANNEL_BROKEN = "channel_broken"
DESCRIPTOR_COUNT = "descriptor_count"
CONTROL_TRUNCATED = "control_truncated"
NOT_A_SOCKET = "not_a_socket"

HANDED = "handed"
HOST_SLOW = "host_slow"

# Room for two descriptors: one is a hand-over, two is refused, more is cut (MSG_CTRUNC).
_ANCILLARY_FDS = 2
_POLL_SLICE_S = 0.02


@dataclass(frozen=True)
class HandoverResult:
    """``outcome`` is ``HANDED`` (``handle`` names the kept copy) or ``HOST_SLOW`` (``handle`` is None)."""

    outcome: str
    handle: int | None = None


def handover_shown(platform: str | None = None, sock_module=None) -> bool:
    """Whether handing a connection to a plugin host is shown on this platform.

    Linux only, and only where the socket module has ``SCM_RIGHTS``, ``MSG_CMSG_CLOEXEC``
    and ``SO_PEERCRED``. macOS and native Windows answer False: not shown there.
    """
    platform = sys.platform if platform is None else platform
    module = socket if sock_module is None else sock_module
    if not platform.startswith("linux"):
        return False
    return all(hasattr(module, name) for name in ("SCM_RIGHTS", "MSG_CMSG_CLOEXEC", "SO_PEERCRED"))


def require_handover_platform(platform: str | None = None, sock_module=None) -> None:
    """Raise ``Refused(platform_unsupported, NOT_SHOWN)`` where ``handover_shown`` is False."""
    if not handover_shown(platform, sock_module):
        raise Refused(PLATFORM_CODE, NOT_SHOWN)


def _check_user_namespace(host_pid: int) -> None:
    """Refuse a host whose user namespace is not the core's own, or cannot be read."""
    try:
        theirs = os.stat(f"/proc/{host_pid}/ns/user")
        mine = os.stat("/proc/self/ns/user")
    except FileNotFoundError:
        raise Refused(REFUSED_CODE, HOST_GONE) from None
    except OSError:
        raise Refused(REFUSED_CODE, USER_NAMESPACE_UNREADABLE) from None
    if (theirs.st_dev, theirs.st_ino) != (mine.st_dev, mine.st_ino):
        raise Refused(REFUSED_CODE, OTHER_USER_NAMESPACE)


def _cut_fd(fd: int) -> None:
    """shutdown(SHUT_RDWR) on the kept copy, then close it. A peer already gone is no error."""
    sock = socket.socket(fileno=fd)
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError as exc:
        if exc.errno not in (errno.ENOTCONN, errno.EBADF):
            raise
    finally:
        sock.close()


class HandoverTable:
    """The core's own copies of the sockets it handed to plugin hosts, one table per host."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hosts: dict[str, dict[int, int]] = {}
        self._host_locks: dict[str, threading.Lock] = {}
        self._next = 1

    def _host_lock(self, host_id: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host_id, threading.Lock())

    def handed(self, host_id: str) -> tuple[int, ...]:
        """The handles of the sockets this table keeps for ``host_id``."""
        with self._lock:
            return tuple(sorted(self._hosts.get(host_id, {})))

    def hand_over(self, host_id: str, channel: socket.socket, sock: socket.socket, payload: bytes,
                  *, host_pid: int, send_timeout_s: float = HANDOVER_SEND_TIMEOUT_S) -> HandoverResult:
        """Send ``sock`` with ``payload`` over ``channel`` to the host ``host_id`` (process ``host_pid``).

        HANDED: the descriptor is in the channel and the table keeps the core's copy (``sock``
        is detached and must not be used by the caller). HOST_SLOW: the channel stayed full for
        ``send_timeout_s``; nothing was handed or kept and ``sock`` is still the caller's.
        Every refusal closes ``sock``.
        """
        if not isinstance(host_id, str) or not host_id:
            raise TypeError("host_id must be a non-empty str")
        if not isinstance(channel, socket.socket) or not isinstance(sock, socket.socket):
            raise TypeError("channel and sock must be sockets")
        if not isinstance(payload, bytes) or not payload:
            raise TypeError("payload must be non-empty bytes")
        if isinstance(host_pid, bool) or not isinstance(host_pid, int) or host_pid <= 0:
            raise TypeError("host_pid must be a positive int")
        if (isinstance(send_timeout_s, bool) or not isinstance(send_timeout_s, (int, float))
                or not send_timeout_s >= 0):
            raise TypeError("send_timeout_s must be a number of seconds, 0 or more")
        if sock.fileno() < 0:
            raise ValueError("sock is closed")
        try:
            require_handover_platform()
            _check_user_namespace(host_pid)
        except Refused:
            sock.close()
            raise
        with self._host_lock(host_id):
            outcome = self._send(channel, sock, payload, send_timeout_s)
            if outcome == HOST_SLOW:
                return HandoverResult(HOST_SLOW)
            with self._lock:
                handle = self._next
                self._next += 1
                self._hosts.setdefault(host_id, {})[handle] = sock.detach()
            return HandoverResult(HANDED, handle)

    def _send(self, channel: socket.socket, sock: socket.socket, payload: bytes, send_timeout_s: float) -> str:
        """One sendmsg with the descriptor, never blocking past the cap; the channel's own mode is untouched."""
        deadline = time.monotonic() + send_timeout_s
        flags = socket.MSG_DONTWAIT | getattr(socket, "MSG_NOSIGNAL", 0)
        rights = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [sock.fileno()]))]
        while True:
            try:
                sent = channel.sendmsg([payload], rights, flags)
                break
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    return HOST_SLOW
                _wait_writable(channel, min(left, _POLL_SLICE_S))
            except (BrokenPipeError, ConnectionResetError):
                sock.close()
                raise Refused(REFUSED_CODE, HOST_GONE) from None
            except OSError:
                sock.close()
                raise
        rest = payload[sent:]
        while rest:  # the descriptor went with the first byte; the rest of the message must follow
            try:
                rest = rest[channel.send(rest, flags):]
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    _cut_fd(sock.detach())
                    raise Refused(REFUSED_CODE, CHANNEL_BROKEN) from None
                _wait_writable(channel, min(left, _POLL_SLICE_S))
            except OSError:
                _cut_fd(sock.detach())
                raise Refused(REFUSED_CODE, CHANNEL_BROKEN) from None
        return HANDED

    def cut(self, host_id: str, handle: int) -> bool:
        """Cut one handed socket: shutdown(SHUT_RDWR) on the kept copy, then close it. False if unknown."""
        with self._lock:
            fd = self._hosts.get(host_id, {}).pop(handle, None)
        if fd is None:
            return False
        _cut_fd(fd)
        return True

    def cut_host(self, host_id: str) -> int:
        """Cut every socket handed to ``host_id`` (a revoke, a grant change, Stop). Answers how many."""
        with self._host_lock(host_id):
            with self._lock:
                fds = list(self._hosts.pop(host_id, {}).values())
            for fd in fds:
                _cut_fd(fd)
        return len(fds)


def _wait_writable(channel: socket.socket, timeout: float) -> None:
    poller = select.poll()
    poller.register(channel.fileno(), select.POLLOUT)
    poller.poll(max(0, int(timeout * 1000)))


def receive_handover(channel: socket.socket, bufsize: int, *, timeout: float | None):
    """Read one message from ``channel``; answer ``(data, sock)``, ``sock`` None when none came.

    The descriptor is taken with ``recvmsg`` and ``MSG_CMSG_CLOEXEC`` only. The received
    socket gets ``timeout`` as its own mode (None: blocking). A message with more than one
    descriptor, a cut control message, or a descriptor that is not a socket closes every
    descriptor it brought and raises ``Refused(handover_refused, <reason>)``.
    """
    if isinstance(bufsize, bool) or not isinstance(bufsize, int) or bufsize <= 0:
        raise TypeError("bufsize must be a positive int")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                or not timeout >= 0):
        raise TypeError("timeout must be None or a number of seconds, 0 or more")
    require_handover_platform()
    itemsize = array.array("i").itemsize
    data, ancdata, msg_flags, _ = channel.recvmsg(
        bufsize, socket.CMSG_SPACE(_ANCILLARY_FDS * itemsize), socket.MSG_CMSG_CLOEXEC)
    fds: list[int] = []
    for level, kind, cdata in ancdata:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            got = array.array("i")
            got.frombytes(cdata[:len(cdata) - (len(cdata) % itemsize)])
            fds.extend(got)
    reason = None
    if msg_flags & socket.MSG_CTRUNC:
        reason = CONTROL_TRUNCATED
    elif len(fds) > 1:
        reason = DESCRIPTOR_COUNT
    if reason is not None:
        for fd in fds:
            os.close(fd)
        raise Refused(REFUSED_CODE, reason)
    if not fds:
        return data, None
    try:
        sock = socket.socket(fileno=fds[0])
    except OSError:
        os.close(fds[0])
        raise Refused(REFUSED_CODE, NOT_A_SOCKET) from None
    sock.settimeout(timeout)
    return data, sock
