"""Adversarial control transport tests (CSL-719 hardening).

Races (two clients with one wrong uid, two servers on one stale socket), path swaps
(symlinked socket or lock file), a stale socket after a real SIGKILL, peer-credential
edge cases (SO_PEERCRED is read at connect time; SO_PASSCRED ancillary data is ignored),
and the no-network and Windows-stub guarantees checked at runtime as well as in source.
"""

from __future__ import annotations

import ast
import os
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from recursion_core import control_transport as ct
from recursion_core.control_transport import windows
from recursion_core.control_transport.peercred import PeerCred, get_peer_cred
from recursion_core.control_transport.unix import UnixControlClient, UnixControlServer, check_peer

LINUX = sys.platform.startswith("linux")
PACKAGE_DIR = Path(ct.__file__).resolve().parent
CORE_DIR = PACKAGE_DIR.parents[1]


def _raw_frame_bytes(path: str, payload: bytes = b'{"op":"ping"}', sock: socket.socket | None = None) -> bytes:
    """Connect, send one frame, return whatever comes back before close (b"" for a refusal)."""
    s = sock or socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(5.0)
        s.connect(path)
        try:
            s.sendall(struct.pack(">I", len(payload)) + payload)
        except OSError:
            return b""
        chunks = []
        while True:
            try:
                chunk = s.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if len(b"".join(chunks)) >= 4:
                (length,) = struct.unpack(">I", b"".join(chunks)[:4])
                if len(b"".join(chunks)) >= 4 + length:
                    break
        return b"".join(chunks)
    finally:
        s.close()


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="cth-", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(self._rm)
        self.sock_dir = os.path.join(self.base, "run")
        self.sock_path = os.path.join(self.sock_dir, "control.sock")

    def _rm(self) -> None:
        for root, dirs, files in os.walk(self.base, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    os.unlink(path)
                else:
                    os.chmod(path, 0o700)
                    os.rmdir(path)
        os.rmdir(self.base)

    def server(self, **kwargs) -> UnixControlServer:
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health(), **kwargs)
        srv.start()
        self.addCleanup(srv.close)
        return srv

    def stale_socket(self) -> None:
        os.makedirs(self.sock_dir, 0o700, exist_ok=True)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.sock_path)
        stale.close()


@unittest.skipUnless(LINUX, "Linux is the tested path")
class PeerRaceTests(_TmpCase):
    def test_two_clients_one_wrong_uid_race(self) -> None:
        """A foreign-uid client hammering the socket gets zero bytes; the good one is unaffected."""
        bad_dir = os.path.join(self.base, "bad")
        os.mkdir(bad_dir, 0o700)

        def getter(sock: socket.socket) -> PeerCred:
            cred = get_peer_cred(sock)
            peer = sock.getpeername()
            if isinstance(peer, str) and peer.startswith(bad_dir):
                return PeerCred(pid=cred.pid, uid=cred.uid + 1, gid=cred.gid)
            return cred

        srv = self.server(peer_cred=getter)
        bad_replies: list[bytes] = []
        errors: list[BaseException] = []
        stop = threading.Event()

        def bad_client() -> None:
            n = 0
            while not stop.is_set():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.bind(os.path.join(bad_dir, f"c{n}"))
                n += 1
                try:
                    bad_replies.append(_raw_frame_bytes(srv.path, sock=s))
                except OSError:
                    bad_replies.append(b"")

        def good_client() -> None:
            try:
                with UnixControlClient(srv.path) as client:
                    for i in range(50):
                        reply = client.request({"op": "ping", "id": i})
                        assert reply == {"ok": True, "op": "pong", "id": i}, reply
            except BaseException as exc:  # surfaced below
                errors.append(exc)

        bad = threading.Thread(target=bad_client)
        bad.start()
        goods = [threading.Thread(target=good_client) for _ in range(2)]
        for t in goods:
            t.start()
        for t in goods:
            t.join(30)
        stop.set()
        bad.join(30)
        self.assertEqual(errors, [])
        self.assertTrue(bad_replies)
        self.assertEqual(set(bad_replies), {b""})
        self.assertGreaterEqual(len(srv.refused), len(bad_replies))
        self.assertEqual(srv.accepted, 2)

    def test_getter_raising_any_exception_refuses_and_keeps_accepting(self) -> None:
        calls: list[int] = []

        def flaky(sock: socket.socket) -> PeerCred:
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("unexpected getter failure")
            if len(calls) == 2:
                raise struct.error("short ucred")
            return get_peer_cred(sock)

        srv = self.server(peer_cred=flaky)
        self.assertEqual(_raw_frame_bytes(srv.path), b"")
        self.assertEqual(_raw_frame_bytes(srv.path), b"")
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        self.assertEqual(len(srv.refused), 2)

    def test_malformed_credentials_fail_closed(self) -> None:
        sock_a, sock_b = socket.socketpair(socket.AF_UNIX)
        self.addCleanup(sock_a.close)
        self.addCleanup(sock_b.close)
        uid = os.geteuid()
        for bad in (None, (0, uid, 0), PeerCred(pid=1, uid=str(uid), gid=0), PeerCred(pid=1, uid=True, gid=0)):
            with self.subTest(bad=bad):
                with self.assertRaises(ct.PeerRefused):
                    check_peer(sock_a, lambda _s, bad=bad: bad, uid if bad is not None else 1)

    def test_peer_closing_before_accept_does_not_kill_the_server(self) -> None:
        srv = self.server()
        for _ in range(20):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(srv.path)
            s.close()
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        self.assertTrue(srv._thread is not None and srv._thread.is_alive())


@unittest.skipUnless(LINUX, "Linux is the tested path")
class PeerCredEdgeTests(_TmpCase):
    def test_peercred_is_bound_at_connect_and_matches_socketpair(self) -> None:
        a, b = socket.socketpair(socket.AF_UNIX)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        cred = get_peer_cred(a)
        self.assertEqual((cred.pid, cred.uid, cred.gid), (os.getpid(), os.geteuid(), os.getegid()))

    def test_non_unix_socket_has_no_peer_credentials(self) -> None:
        # Constructed only to show the getter refuses it; never bound or connected.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        with self.assertRaises(ct.PeerRefused):
            check_peer(s, get_peer_cred, os.geteuid())

    @unittest.skipUnless(hasattr(socket, "SO_PASSCRED") and hasattr(socket, "SCM_CREDENTIALS"), "needs SO_PASSCRED")
    def test_so_passcred_ancillary_data_is_ignored_for_identity(self) -> None:
        """A client asserting credentials with SCM_CREDENTIALS does not change who the server sees."""
        seen: list[PeerCred] = []

        def recording(sock: socket.socket) -> PeerCred:
            cred = get_peer_cred(sock)
            seen.append(cred)
            return cred

        srv = self.server(peer_cred=recording)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(s.close)
        s.settimeout(5.0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        s.connect(srv.path)
        payload = b'{"op":"ping"}'
        ucred = struct.pack("=iII", os.getpid(), os.geteuid(), os.getegid())
        s.sendmsg([struct.pack(">I", len(payload)) + payload], [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS, ucred)])
        header = s.recv(4)
        (length,) = struct.unpack(">I", header)
        self.assertIn(b"pong", s.recv(length))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].uid, os.geteuid())

    @unittest.skipUnless(LINUX and os.geteuid() == 0, "needs root to drop to a real foreign uid")
    def test_real_foreign_uid_gets_no_frame(self) -> None:  # pragma: no cover - root-only
        srv = self.server()
        os.chmod(self.sock_dir, 0o755)  # chmod escape: open the dir, the peer check must still refuse
        os.chmod(srv.path, 0o666)
        pid = os.fork()
        if pid == 0:
            try:
                os.setgid(65534)
                os.setuid(65534)
                os._exit(0 if _raw_frame_bytes(srv.path) == b"" else 1)
            except BaseException:
                os._exit(2)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)


@unittest.skipUnless(LINUX, "Linux is the tested path")
class PathSwapTests(_TmpCase):
    def test_symlink_at_socket_path_is_refused_and_left_alone(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        elsewhere = os.path.join(self.base, "elsewhere.sock")
        target = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(target.close)
        target.bind(elsewhere)
        target.listen(1)
        os.symlink(elsewhere, self.sock_path)
        with self.assertRaises(ct.InsecureSocketPath):
            UnixControlServer(self.sock_dir, ct.with_ping_health()).start()
        self.assertTrue(os.path.islink(self.sock_path))
        self.assertTrue(stat.S_ISSOCK(os.lstat(elsewhere).st_mode))

    def test_dangling_symlink_at_socket_path_is_refused(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        os.symlink(os.path.join(self.base, "nowhere.sock"), self.sock_path)
        with self.assertRaises(ct.InsecureSocketPath):
            UnixControlServer(self.sock_dir, ct.with_ping_health()).start()
        self.assertFalse(os.path.exists(os.path.join(self.base, "nowhere.sock")))

    def test_symlinked_or_non_file_lock_is_refused(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        victim = os.path.join(self.base, "victim")
        Path(victim).write_text("keep")
        os.symlink(victim, self.sock_path + ".lock")
        with self.assertRaises(ct.InsecureSocketPath):
            UnixControlServer(self.sock_dir, ct.with_ping_health()).start()
        self.assertEqual(Path(victim).read_text(), "keep")
        os.unlink(self.sock_path + ".lock")
        os.mkdir(self.sock_path + ".lock")
        with self.assertRaises(ct.InsecureSocketPath):
            UnixControlServer(self.sock_dir, ct.with_ping_health()).start()
        self.assertFalse(os.path.exists(self.sock_path))

    def test_client_following_a_symlink_to_a_foreign_socket_refuses(self) -> None:
        """The client's own peer check catches a path swapped to another uid's server."""
        srv = self.server()
        link = os.path.join(self.base, "swapped.sock")
        os.symlink(srv.path, link)

        def foreign(sock: socket.socket) -> PeerCred:
            return PeerCred(pid=1, uid=os.geteuid() + 1, gid=0)

        with self.assertRaises(ct.PeerRefused):
            UnixControlClient(link, peer_cred=foreign)

    def test_close_leaves_a_successor_socket_alone(self) -> None:
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        os.unlink(srv.path)  # operator removed the socket while the old server was wedged
        successor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(successor.close)
        successor.bind(self.sock_path)
        srv.close()
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.sock_path).st_mode))

    def test_close_then_start_again_is_refused(self) -> None:
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        srv.start()
        srv.close()
        with self.assertRaises(RuntimeError):
            srv.start()

    def test_embedded_nul_is_refused(self) -> None:
        for address in ("/tmp/a\0b", b"/tmp/a\0b"):
            with self.subTest(address=address):
                with self.assertRaises(ct.InsecureSocketPath):
                    UnixControlServer(address, ct.with_ping_health())
                with self.assertRaises(ct.InsecureSocketPath):
                    UnixControlClient(address)


@unittest.skipUnless(LINUX, "Linux is the tested path")
class StaleSocketTests(_TmpCase):
    def test_two_servers_racing_on_one_stale_socket_never_both_start(self) -> None:
        """Without the lock, both probe ECONNREFUSED and the loser listens on an unlinked inode."""
        for _ in range(60):
            self.stale_socket()
            servers = [UnixControlServer(self.sock_dir, ct.with_ping_health()) for _ in range(2)]
            barrier = threading.Barrier(2)
            outcomes: list[str] = ["", ""]

            def go(k: int) -> None:
                barrier.wait()
                try:
                    servers[k].start()
                    outcomes[k] = "started"
                except ct.InsecureSocketPath:
                    outcomes[k] = "refused"

            threads = [threading.Thread(target=go, args=(k,)) for k in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            self.assertEqual(sorted(outcomes), ["refused", "started"])
            winner = servers[outcomes.index("started")]
            with UnixControlClient(self.sock_path) as client:
                self.assertEqual(client.request({"op": "ping"})["op"], "pong")
            winner.close()
            self.assertFalse(os.path.exists(self.sock_path))

    def test_stale_socket_after_sigkill_is_taken_over(self) -> None:
        code = (
            "import sys\n"
            "from recursion_core.control_transport import UnixControlServer, with_ping_health\n"
            "srv = UnixControlServer(sys.argv[1], with_ping_health()); srv.start()\n"
            "print('ready', flush=True)\n"
            "sys.stdin.read()\n"
        )
        env = dict(os.environ, PYTHONPATH=str(CORE_DIR))
        proc = subprocess.Popen(
            [sys.executable, "-B", "-c", code, self.sock_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), b"ready")
            with self.assertRaises(ct.InsecureSocketPath):  # live: lock held
                UnixControlServer(self.sock_dir, ct.with_ping_health()).start()
            proc.send_signal(signal.SIGKILL)
            proc.wait(10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
            proc.stdin.close()
            proc.stdout.close()
        self.assertTrue(stat.S_ISSOCK(os.lstat(self.sock_path).st_mode))  # crash left it behind
        srv = self.server()
        self.assertEqual(stat.S_IMODE(os.lstat(srv.path).st_mode), 0o600)
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "health"})["transport"], "unix")


@unittest.skipUnless(LINUX, "Linux is the tested path")
class NoNetworkRuntimeTests(_TmpCase):
    def test_only_af_unix_sockets_are_created(self) -> None:
        families: list[int] = []
        real = socket.socket

        class Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, family=-1, *args, **kwargs):
                families.append(family)
                super().__init__(family, *args, **kwargs)

        socket.socket = Recording  # type: ignore[misc]
        try:
            self.stale_socket()
            srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
            srv.start()
            try:
                with UnixControlClient(srv.path) as client:
                    client.request({"op": "ping"})
            finally:
                srv.close()
        finally:
            socket.socket = real  # type: ignore[misc]
        self.assertTrue(families)
        self.assertEqual(set(families), {socket.AF_UNIX})

    def test_source_has_no_numeric_or_from_imported_network_family(self) -> None:
        """Closes gaps in the attribute check: ``from socket import AF_INET`` and ``socket.socket(2, ...)``."""
        banned = {"AF_INET", "AF_INET6", "AF_VSOCK", "AF_PACKET", "create_server", "create_connection"}
        sources = [p for p in PACKAGE_DIR.rglob("*.py") if "tests" not in p.relative_to(PACKAGE_DIR).parts]
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "socket":
                    for alias in node.names:
                        self.assertNotIn(alias.name, banned | {"*"}, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Name):
                    self.assertNotIn(node.id, banned, f"{path.name}:{node.lineno}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "socket"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "socket"
                ):
                    first = node.args[0] if node.args else None
                    self.assertTrue(
                        isinstance(first, ast.Attribute) and first.attr == "AF_UNIX",
                        f"{path.name}:{node.lineno} socket.socket() must name AF_UNIX",
                    )
                if isinstance(node, ast.Attribute) and node.attr in {"bind", "listen"}:
                    self.assertEqual(path.name, "unix.py", f"{path.name}:{node.lineno} binds outside unix.py")


class WindowsStubHardenTests(_TmpCase):
    def test_windows_classes_construct_nothing_even_with_a_valid_linux_dir(self) -> None:
        for cls, arg in ((windows.WindowsControlServer, self.sock_dir), (windows.WindowsControlClient, self.sock_path)):
            with self.subTest(cls=cls.__name__):
                with self.assertRaises(ct.PlatformNotSupported) as err:
                    cls(arg, ct.with_ping_health()) if cls is windows.WindowsControlServer else cls(arg)
                self.assertIn("F40", str(err.exception))
        self.assertFalse(os.path.exists(self.sock_dir))

    def test_windows_classes_are_not_unix_transports(self) -> None:
        for cls in (windows.WindowsControlServer, windows.WindowsControlClient):
            self.assertFalse(issubclass(cls, (UnixControlServer, UnixControlClient)))
            for attr in ("start", "request", "path"):
                self.assertFalse(hasattr(cls, attr), f"{cls.__name__}.{attr} would look usable")

    def test_linux_factory_never_returns_the_stub(self) -> None:
        for platform in ("linux", "linux2", "darwin", "freebsd14"):
            with self.subTest(platform=platform):
                self.assertIsInstance(ct.create_server(self.sock_dir, ct.with_ping_health(), platform=platform), UnixControlServer)
        self.assertFalse(os.path.exists(self.sock_dir))  # constructed, never started

    def test_windows_module_imports_no_socket(self) -> None:
        tree = ast.parse((PACKAGE_DIR / "windows.py").read_text(encoding="utf-8"))
        imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("socket", imported)


if __name__ == "__main__":
    unittest.main()
