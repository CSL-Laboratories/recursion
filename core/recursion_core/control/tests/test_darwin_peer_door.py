"""Darwin owner-only door for operator reads, decided on Linux with a fake socket.

The server must read a peer uid from LOCAL_PEERCRED (getsockopt(0, 1, n) -> xucred) when
SO_PEERCRED is absent. A foreign uid is peer_refused and shut unread, the owner is served,
a wrong cr_version or a short xucred is refused, and a socket path over 104 bytes is refused
before bind. Windows still refuses to serve. Nothing here opens a Mac kernel credential.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from unittest import mock

from recursion_core.control import operator_reads_socket as transport
from recursion_core.control.operator_reads import OperatorReads, connect
from recursion_core.control.operator_reads_socket import (
    OperatorReadsServer,
    PlatformNotSupported,
    SocketUnsafe,
)
from recursion_core.control_transport import peercred
from recursion_core.control_transport.peercred import get_peer_cred

POSITIONS = "operator.positions.inspect"
HAVE_UNIX = hasattr(socket, "AF_UNIX") and hasattr(os, "getuid")
# xucred prefix that today's darwin branch already unpacks: cr_version, cr_uid.
_PREFIX = struct.Struct("=II")


def pack_xucred(uid: int, version: int = 0) -> bytes:
    """76-byte xucred: version, uid, ngroups, pad, 16 groups. The layout peercred already sizes."""

    raw = struct.pack("=IIH", version, uid, 1) + b"\x00\x00" + bytes(4 * 16)
    if len(raw) != peercred._XUCRED_SIZE:
        raise AssertionError(f"packed xucred is {len(raw)} bytes, layout is {peercred._XUCRED_SIZE}")
    return raw


class FakeSocket:
    """Stands in for an accepted AF_UNIX socket. getsockopt(0, 1, n) is LOCAL_PEERCRED."""

    family = socket.AF_UNIX if HAVE_UNIX else object()

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[int, int, int]] = []

    def getsockopt(self, level: int, opt: int, n: int) -> bytes:
        self.calls.append((level, opt, n))
        return self.payload


def _reads(calls: list[str]) -> OperatorReads:
    def positions() -> dict:
        calls.append("positions")
        return {"venue": "paper", "rows": []}

    return OperatorReads({"positions": positions})


def _exchange(path: str, payload: bytes) -> dict:
    """One raw connection: write the request, read the one reply line. No peer-uid check of our own."""

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5.0)
        conn.connect(path)
        try:
            conn.sendall(payload)
        except OSError:
            pass
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
    line, _, rest = buf.partition(b"\n")
    if rest:
        raise AssertionError(rest)
    return json.loads(line)


@contextmanager
def _darwin_without_so_peercred():
    """Darwin has AF_UNIX and uids, and no SO_PEERCRED. This host has the constant, so clear it."""

    with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(transport, "_SO_PEERCRED", None):
        yield


@unittest.skipUnless(HAVE_UNIX, "darwin peer-uid decisions are simulated on a POSIX host with AF_UNIX")
class DarwinPeerDoorTest(unittest.TestCase):
    def setUp(self) -> None:
        root = "/tmp" if os.path.isdir("/tmp") else None
        self.base = tempfile.mkdtemp(prefix="dpd-", dir=root)
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = os.path.join(self.base, "recursion", "operator-reads.sock")

    def test_linux_socketpair_peer_uid_is_unchanged(self) -> None:
        if not sys.platform.startswith("linux"):
            self.skipTest("the Linux SO_PEERCRED path is the one this host runs for real")
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with left, right:
            cred = get_peer_cred(left)
        self.assertEqual((cred.pid, cred.uid, cred.gid), (os.getpid(), os.getuid(), os.getgid()))

    def test_other_uid_is_peer_refused_shut_unread(self) -> None:
        foreign = os.getuid() + 1
        payload = pack_xucred(foreign, version=0)
        fake = FakeSocket(payload)
        with mock.patch.object(sys, "platform", "darwin"):
            cred = get_peer_cred(fake)
        self.assertEqual(fake.calls, [(0, 1, peercred._XUCRED_SIZE)])
        self.assertEqual(peercred._SOL_LOCAL, 0)
        self.assertEqual(peercred._LOCAL_PEERCRED, 0x001)
        self.assertEqual(getattr(peercred, "XUCRED_VERSION", 0), 0)
        self.assertEqual((cred.pid, cred.uid, cred.gid), (None, foreign, None))

        calls: list[str] = []
        server_recvs: list[str] = []
        seen: list[tuple[int, int, int]] = []
        real_get = socket.socket.getsockopt
        real_recv = socket.socket.recv

        def getsockopt(sock: socket.socket, level: int, opt: int, n: int = 0) -> bytes:
            if (level, opt) == (0, 1):
                seen.append((level, opt, n))
                return payload
            return real_get(sock, level, opt, n)

        def recv(sock: socket.socket, *args: object, **kwargs: object) -> bytes:
            if threading.current_thread().name.startswith("operator-reads"):
                server_recvs.append(threading.current_thread().name)
            return real_recv(sock, *args, **kwargs)

        server = OperatorReadsServer(_reads(calls), self.path)
        request = b'{"method":"operator.positions.inspect","params":{}}\n'
        try:
            with _darwin_without_so_peercred(), mock.patch.object(socket.socket, "getsockopt", getsockopt), \
                    mock.patch.object(socket.socket, "recv", recv):
                server.start()
                reply = _exchange(self.path, request)
        finally:
            if server._listener is not None:
                server.close()
        self.assertEqual((reply["code"], reply["status_code"], reply["result"]), ("peer_refused", "peer_refused", {}))
        self.assertEqual(calls, [])
        self.assertEqual(server_recvs, [])
        self.assertIn((0, 1, peercred._XUCRED_SIZE), seen)

    def test_owner_is_served(self) -> None:
        calls: list[str] = []
        payload = pack_xucred(os.getuid(), version=0)
        real_get = socket.socket.getsockopt

        def getsockopt(sock: socket.socket, level: int, opt: int, n: int = 0) -> bytes:
            if (level, opt) == (0, 1):
                return payload
            return real_get(sock, level, opt, n)

        server = OperatorReadsServer(_reads(calls), self.path)
        try:
            with _darwin_without_so_peercred(), mock.patch.object(socket.socket, "getsockopt", getsockopt):
                server.start()
                reply = connect(self.path).call(POSITIONS, {})
        finally:
            if server._listener is not None:
                server.close()
        self.assertEqual(reply["code"], "ok")
        self.assertEqual(calls, ["positions"])

    def test_a_refused_peer_does_not_block_the_owner(self) -> None:
        calls: list[str] = []
        current = {"uid": os.getuid() + 1, "version": 0}
        real_get = socket.socket.getsockopt

        def getsockopt(sock: socket.socket, level: int, opt: int, n: int = 0) -> bytes:
            if (level, opt) == (0, 1):
                return pack_xucred(current["uid"], version=current["version"])
            return real_get(sock, level, opt, n)

        server = OperatorReadsServer(_reads(calls), self.path)
        request = b'{"method":"operator.positions.inspect","params":{}}\n'
        try:
            with _darwin_without_so_peercred(), mock.patch.object(socket.socket, "getsockopt", getsockopt):
                server.start()
                refused = _exchange(self.path, request)
                current["uid"] = os.getuid()
                served = connect(self.path).call(POSITIONS, {})
        finally:
            if server._listener is not None:
                server.close()
        self.assertEqual(refused["code"], "peer_refused")
        self.assertEqual(served["code"], "ok")
        self.assertEqual(calls, ["positions"])

    def test_wrong_cr_version_is_refused(self) -> None:
        payload = pack_xucred(os.getuid(), version=1)
        fake = FakeSocket(payload)
        with mock.patch.object(sys, "platform", "darwin"):
            with self.assertRaises(OSError):
                get_peer_cred(fake)
        self.assertEqual(fake.calls, [(0, 1, peercred._XUCRED_SIZE)])
        version, uid = _PREFIX.unpack_from(payload)
        self.assertEqual((version, uid), (1, os.getuid()))

        calls: list[str] = []
        real_get = socket.socket.getsockopt

        def getsockopt(sock: socket.socket, level: int, opt: int, n: int = 0) -> bytes:
            if (level, opt) == (0, 1):
                return payload
            return real_get(sock, level, opt, n)

        server = OperatorReadsServer(_reads(calls), self.path)
        request = b'{"method":"operator.positions.inspect","params":{}}\n'
        try:
            with _darwin_without_so_peercred(), mock.patch.object(socket.socket, "getsockopt", getsockopt):
                server.start()
                reply = _exchange(self.path, request)
        finally:
            if server._listener is not None:
                server.close()
        self.assertEqual(reply["code"], "peer_refused")
        self.assertEqual(calls, [])

    def test_short_xucred_is_refused(self) -> None:
        # Long enough for the prefix today's branch unpacks, short of the layout, so length is not ignored.
        for size in (8, peercred._XUCRED_SIZE - 1):
            with self.subTest(size=size):
                payload = pack_xucred(os.getuid(), version=0)[:size]
                self.assertEqual(_PREFIX.unpack_from(payload)[1], os.getuid())
                fake = FakeSocket(payload)
                with mock.patch.object(sys, "platform", "darwin"):
                    with self.assertRaises(OSError):
                        get_peer_cred(fake)
                self.assertEqual(fake.calls, [(0, 1, peercred._XUCRED_SIZE)])

        calls: list[str] = []
        short = pack_xucred(os.getuid(), version=0)[:8]
        real_get = socket.socket.getsockopt

        def getsockopt(sock: socket.socket, level: int, opt: int, n: int = 0) -> bytes:
            if (level, opt) == (0, 1):
                return short
            return real_get(sock, level, opt, n)

        server = OperatorReadsServer(_reads(calls), self.path)
        request = b'{"method":"operator.positions.inspect","params":{}}\n'
        try:
            with _darwin_without_so_peercred(), mock.patch.object(socket.socket, "getsockopt", getsockopt):
                server.start()
                reply = _exchange(self.path, request)
        finally:
            if server._listener is not None:
                server.close()
        self.assertEqual(reply["code"], "peer_refused")
        self.assertEqual(calls, [])

    def test_socket_path_over_104_bytes_is_refused_before_bind(self) -> None:
        path = _path_of_length(self.base, 105)
        self.assertGreater(len(os.fsencode(path)), 104)
        self.assertLessEqual(len(os.fsencode(path)), 107)  # Linux can bind this; darwin must not try
        binds: list[object] = []
        real_bind = socket.socket.bind

        def bind(sock: socket.socket, address: object) -> None:
            binds.append(address)
            return real_bind(sock, address)

        server = OperatorReadsServer(OperatorReads(), path)
        try:
            with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(socket.socket, "bind", bind):
                with self.assertRaises(SocketUnsafe) as caught:
                    server.start()
        finally:
            if server._listener is not None:
                server.close()
        self.assertIn("104", str(caught.exception))
        self.assertEqual(binds, [])
        self.assertFalse(os.path.exists(path))

    def test_windows_still_refuses_to_serve(self) -> None:
        binds: list[object] = []
        real_bind = socket.socket.bind

        def bind(sock: socket.socket, address: object) -> None:
            binds.append(address)
            return real_bind(sock, address)

        server = OperatorReadsServer(OperatorReads(), self.path)
        try:
            with mock.patch.object(sys, "platform", "win32"), mock.patch.object(socket.socket, "bind", bind):
                with self.assertRaises(PlatformNotSupported):
                    server.start()
        finally:
            if server._listener is not None:
                server.close()
        self.assertEqual(binds, [])
        self.assertFalse(os.path.exists(self.path))


def _path_of_length(base: str, n: int) -> str:
    """An absolute socket path of ``n`` encoded bytes whose parent directory exists."""

    name = "operator-reads.sock"
    pad_len = n - len(os.fsencode(base)) - len(name) - 2  # two slashes
    if pad_len < 1:
        raise AssertionError(f"{base} is too long to build a {n}-byte socket path")
    return f"{base}/{'p' * pad_len}/{name}"


if __name__ == "__main__":
    unittest.main()
