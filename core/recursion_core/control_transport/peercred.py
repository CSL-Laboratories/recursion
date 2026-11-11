"""Read the uid of the process at the other end of a connected AF_UNIX socket.

Linux (tested): ``SO_PEERCRED`` returns ``struct ucred {pid_t pid; uid_t uid; gid_t gid;}``.
macOS (designed in, not tested here): ``LOCAL_PEERCRED`` at ``SOL_LOCAL`` returns
``struct xucred``; same AF_UNIX family, same check. Any other platform raises, and the
server treats a raise as a refusal (fail closed).
"""

from __future__ import annotations

import socket
import struct
import sys
from dataclasses import dataclass

_UCRED = struct.Struct("=iII")  # pid_t, uid_t, gid_t
# xucred: u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]
_XUCRED_PREFIX = struct.Struct("=II")
_XUCRED_SIZE = 4 + 4 + 2 + 2 + 4 * 16
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001


@dataclass(frozen=True)
class PeerCred:
    pid: int | None
    uid: int
    gid: int | None


def get_peer_cred(sock: socket.socket) -> PeerCred:
    if sock.family != getattr(socket, "AF_UNIX", None):  # Windows CPython has no AF_UNIX
        raise OSError("peer credentials need an AF_UNIX socket")
    if sys.platform.startswith("linux"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
        pid, uid, gid = _UCRED.unpack(raw)
        return PeerCred(pid=pid, uid=uid, gid=gid)
    if sys.platform == "darwin":  # pragma: no cover - Linux is the tested path
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
        _version, uid = _XUCRED_PREFIX.unpack_from(raw)
        return PeerCred(pid=None, uid=uid, gid=None)
    raise OSError(f"no peer credential source on {sys.platform}")
