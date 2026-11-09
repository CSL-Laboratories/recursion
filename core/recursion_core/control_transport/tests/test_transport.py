"""Control transport tests (CSL-719): permissions, peer uid, round trip, remote refusal, Windows stub."""

from __future__ import annotations

import ast
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from recursion_core import control_transport as ct
from recursion_core.control_transport import framing
from recursion_core.control_transport.peercred import PeerCred, get_peer_cred
from recursion_core.control_transport.unix import UnixControlClient, UnixControlServer

LINUX = sys.platform.startswith("linux")
PACKAGE_DIR = Path(ct.__file__).resolve().parent


def _foreign_uid(sock: socket.socket) -> PeerCred:
    return PeerCred(pid=1, uid=os.geteuid() + 1, gid=0)


def _unreadable(sock: socket.socket) -> PeerCred:
    raise OSError("simulated getsockopt failure")


class _Recorder:
    """Stop-shaped delegate: records requests, never touches a process."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: list[dict] = []

    def __call__(self, message: dict, conn: ct.Connection | None = None) -> dict:
        with self.lock:
            self.seen.append(message)
        if message.get("op") == "stop":
            return {"ok": True, "op": "stop", "run_id": message.get("run_id"), "state": "stop_requested"}
        return ct.handlers.unknown_op(message)


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        # Short base path: sun_path is 108 bytes.
        self.base = tempfile.mkdtemp(prefix="ct-", dir="/tmp" if os.path.isdir("/tmp") else None)
        self.addCleanup(self._rm)
        self.sock_dir = os.path.join(self.base, "run")

    def _rm(self) -> None:
        for root, dirs, files in os.walk(self.base, topdown=False):
            for name in files:
                os.unlink(os.path.join(root, name))
            for name in dirs:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    os.unlink(path)
                else:
                    os.rmdir(path)
        os.rmdir(self.base)

    def server(self, handler=None, **kwargs) -> UnixControlServer:
        srv = UnixControlServer(self.sock_dir, handler or ct.with_ping_health(_Recorder()), **kwargs)
        srv.start()
        self.addCleanup(srv.close)
        return srv


@unittest.skipUnless(LINUX, "Linux is the tested path")
class SocketPermissionTests(_TmpCase):
    def test_directory_is_0700_and_socket_is_owner_only(self) -> None:
        srv = self.server()
        dir_st = os.lstat(self.sock_dir)
        self.assertTrue(stat.S_ISDIR(dir_st.st_mode))
        self.assertEqual(stat.S_IMODE(dir_st.st_mode), 0o700)
        self.assertEqual(dir_st.st_uid, os.geteuid())
        sock_st = os.lstat(srv.path)
        self.assertTrue(stat.S_ISSOCK(sock_st.st_mode))
        self.assertEqual(stat.S_IMODE(sock_st.st_mode) & 0o077, 0, oct(sock_st.st_mode))
        self.assertEqual(stat.S_IMODE(sock_st.st_mode), 0o600)
        self.assertEqual(sock_st.st_uid, os.geteuid())

    def test_socket_mode_holds_under_a_permissive_umask(self) -> None:
        old = os.umask(0)
        try:
            srv = self.server()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.lstat(self.sock_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(srv.path).st_mode), 0o600)

    def test_existing_group_or_world_readable_dir_is_refused_not_repaired(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        os.chmod(self.sock_dir, 0o755)
        srv = UnixControlServer(self.sock_dir, ct.with_ping_health())
        with self.assertRaises(ct.InsecureSocketPath):
            srv.start()
        self.assertEqual(stat.S_IMODE(os.lstat(self.sock_dir).st_mode), 0o755)

    def test_symlinked_dir_is_refused(self) -> None:
        real = os.path.join(self.base, "real")
        os.mkdir(real, 0o700)
        os.symlink(real, self.sock_dir)
        with self.assertRaises(ct.InsecureSocketPath):
            UnixControlServer(self.sock_dir, ct.with_ping_health()).start()

    def test_non_socket_at_path_is_refused_and_left_alone(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        planted = os.path.join(self.sock_dir, "control.sock")
        Path(planted).write_text("not a socket")
        with self.assertRaises(ct.InsecureSocketPath):
            UnixControlServer(self.sock_dir, ct.with_ping_health()).start()
        self.assertEqual(Path(planted).read_text(), "not a socket")

    def test_stale_socket_is_replaced_but_live_one_is_refused(self) -> None:
        os.mkdir(self.sock_dir, 0o700)
        stale_path = os.path.join(self.sock_dir, "control.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(stale_path)
        stale.close()  # file stays, nobody listens
        srv = self.server()
        with UnixControlClient(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        with self.assertRaises(ct.InsecureSocketPath):
            UnixControlServer(self.sock_dir, ct.with_ping_health()).start()

    def test_close_does_not_wait_on_connected_idle_clients(self) -> None:
        srv = self.server(idle_timeout_s=30.0)
        client = UnixControlClient(srv.path)
        self.addCleanup(client.close)
        self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        started = time.monotonic()
        srv.close()
        self.assertLess(time.monotonic() - started, 5.0)
        with self.assertRaises((ConnectionError, OSError, ct.FrameError)):
            client.request({"op": "ping"})

    def test_close_removes_socket(self) -> None:
        srv = self.server()
        path = srv.path
        srv.close()
        self.assertFalse(os.path.exists(path))
        self.assertEqual(stat.S_IMODE(os.lstat(self.sock_dir).st_mode), 0o700)


@unittest.skipUnless(LINUX, "Linux is the tested path")
class PeerUidTests(_TmpCase):
    def test_real_so_peercred_reports_our_uid_and_pid(self) -> None:
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with a, b:
            cred = get_peer_cred(a)
        self.assertEqual(cred.uid, os.geteuid())
        self.assertEqual(cred.pid, os.getpid())

    def test_wrong_uid_peer_is_refused_before_any_frame(self) -> None:
        recorder = _Recorder()
        srv = self.server(ct.with_ping_health(recorder), peer_cred=_foreign_uid)
        # The client's own check sees the real server uid, so it connects.
        client = UnixControlClient(srv.path)
        self.addCleanup(client.close)
        with self.assertRaises((ConnectionError, ct.FrameError, OSError)):
            client.request({"op": "stop", "run_id": "r1"})
        self.assertEqual(recorder.seen, [])
        self.assertEqual(srv.accepted, 0)
        self.assertEqual(len(srv.refused), 1)
        self.assertIn(f"peer uid {os.geteuid() + 1}", srv.refused[0])

    def test_unreadable_peer_credentials_fail_closed(self) -> None:
        srv = self.server(peer_cred=_unreadable)
        client = UnixControlClient(srv.path)
        self.addCleanup(client.close)
        with self.assertRaises((ConnectionError, ct.FrameError, OSError)):
            client.request({"op": "ping"})
        self.assertIn("unreadable", srv.refused[0])

    def test_client_refuses_a_server_owned_by_another_uid(self) -> None:
        srv = self.server()
        with self.assertRaises(ct.PeerRefused):
            UnixControlClient(srv.path, peer_cred=_foreign_uid)

    def test_refused_peer_does_not_starve_a_good_one(self) -> None:
        calls = {"n": 0}
        lock = threading.Lock()

        def first_foreign(sock: socket.socket) -> PeerCred:
            with lock:
                calls["n"] += 1
                n = calls["n"]
            return _foreign_uid(sock) if n == 1 else get_peer_cred(sock)

        srv = self.server(peer_cred=first_foreign)
        bad = UnixControlClient(srv.path)
        self.addCleanup(bad.close)
        with self.assertRaises((ConnectionError, ct.FrameError, OSError)):
            bad.request({"op": "ping"})
        with UnixControlClient(srv.path) as good:
            self.assertEqual(good.request({"op": "ping"}), {"ok": True, "op": "pong"})


@unittest.skipUnless(LINUX, "Linux is the tested path")
class RoundTripTests(_TmpCase):
    def test_ping_health_and_stop_shaped_request_round_trip(self) -> None:
        recorder = _Recorder()
        srv = self.server(ct.with_ping_health(recorder))
        with ct.create_client(srv.path) as client:
            self.assertEqual(client.request({"op": "ping"}), {"ok": True, "op": "pong"})
            health = client.request({"op": "health", "id": 7})
            self.assertEqual(health["id"], 7)
            self.assertTrue(health["ok"])
            self.assertEqual(health["transport"], "unix")
            stop = client.request({"op": "stop", "run_id": "run-1", "id": "c1"})
            self.assertEqual(stop, {"ok": True, "op": "stop", "run_id": "run-1", "state": "stop_requested", "id": "c1"})
            unknown = client.request({"op": "nope"})
            self.assertEqual(unknown["error"]["code"], "unknown_op")
        self.assertEqual([m["op"] for m in recorder.seen], ["stop", "nope"])
        self.assertEqual(srv.accepted, 1)
        self.assertEqual(srv.refused, [])

    def test_handler_exception_becomes_internal_error_without_detail(self) -> None:
        def boom(message: dict, conn: ct.Connection) -> dict:
            raise RuntimeError("secret detail")

        srv = self.server(boom)
        with UnixControlClient(srv.path) as client:
            response = client.request({"op": "x"})
            self.assertEqual(response, {"ok": False, "error": {"code": "internal_error"}})
            self.assertNotIn("secret", repr(response))
            # Connection survives a handler error.
            self.assertEqual(client.request({"op": "y"})["error"]["code"], "internal_error")

    def test_concurrent_clients(self) -> None:
        srv = self.server()
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                with UnixControlClient(srv.path) as client:
                    for j in range(20):
                        r = client.request({"op": "ping", "id": f"{i}-{j}"})
                        assert r == {"ok": True, "op": "pong", "id": f"{i}-{j}"}, r
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(srv.accepted, 8)

    def test_oversized_and_malformed_frames_get_bad_frame(self) -> None:
        srv = self.server(max_frame_bytes=64)
        for payload in (
            framing.HEADER.pack(65) + b"x" * 65,
            framing.HEADER.pack(5) + b"nope!",
            framing.HEADER.pack(2) + b"[]",
        ):
            with self.subTest(payload=payload[:12]):
                raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                with raw:
                    raw.settimeout(5)
                    raw.connect(srv.path)
                    raw.sendall(payload)
                    reply = framing.read_frame(raw)
                    self.assertEqual(reply["error"]["code"], "bad_frame")
                    # Then the server hangs up (a reset when unread bytes were left behind).
                    try:
                        self.assertIsNone(framing.read_frame(raw))
                    except ConnectionResetError:
                        pass

    def test_frame_codec(self) -> None:
        frame = framing.encode_frame({"op": "ping", "s": "é"})
        (length,) = framing.HEADER.unpack(frame[:4])
        self.assertEqual(length, len(frame) - 4)
        self.assertEqual(framing.decode_body(frame[4:]), {"op": "ping", "s": "é"})
        with self.assertRaises(ct.FrameError):
            framing.encode_frame({"x": "y" * 100}, max_bytes=10)
        with self.assertRaises(ct.FrameError):
            framing.encode_frame(["not", "object"])  # type: ignore[arg-type]


class FrameDeadlineTests(unittest.TestCase):
    def test_read_frame_deadline_bounds_the_whole_frame_not_each_recv(self) -> None:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        frame = framing.encode_frame({"op": "ping", "pad": "x" * 64})
        stop = threading.Event()
        self.addCleanup(stop.set)

        def drip() -> None:
            for byte in frame:
                if stop.wait(0.05):
                    return
                try:
                    b.sendall(bytes([byte]))
                except OSError:
                    return

        threading.Thread(target=drip, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            framing.read_frame(a, deadline=started + 0.4)
        self.assertLess(time.monotonic() - started, 2.0)


def _wait_for(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _new_peer_served(path: str) -> bool:
    try:
        with UnixControlClient(path, timeout_s=1.0) as client:
            return client.request({"op": "ping"})["op"] == "pong"
    except (ConnectionError, OSError, ct.FrameError):
        return False


@unittest.skipUnless(LINUX, "Linux is the tested path")
class ClientResyncTests(_TmpCase):
    """A failed request must never let a later request read an earlier request's reply."""

    def test_timed_out_stop_reply_is_never_returned_for_the_next_request(self) -> None:
        release = threading.Event()
        self.addCleanup(release.set)

        def slow_stop(message: dict, conn: ct.Connection) -> dict:
            if message.get("op") == "stop":
                release.wait(1.5)
                return {"ok": True, "op": "stop", "state": "stopped"}
            return {"ok": True, "op": "pong"}

        srv = self.server(slow_stop)
        client = UnixControlClient(srv.path, timeout_s=0.3)
        self.addCleanup(client.close)
        with self.assertRaises(TimeoutError):
            client.request({"op": "stop", "id": "s1"})
        release.set()
        time.sleep(0.1)  # the late stop reply is now on the wire
        with self.assertRaises(ConnectionError):
            client.request({"op": "ping", "id": "p1"})
        # A fresh client is in sync.
        with UnixControlClient(srv.path) as fresh:
            self.assertEqual(fresh.request({"op": "ping", "id": "p2"}), {"ok": True, "op": "pong", "id": "p2"})

    def test_reply_with_another_id_is_refused_and_closes_the_client(self) -> None:
        srv = self.server(lambda message, conn: {"ok": True, "op": "stop", "state": "stopped", "id": "s1"})
        client = UnixControlClient(srv.path)
        self.addCleanup(client.close)
        with self.assertRaises(ct.FrameError) as caught:
            client.request({"op": "ping", "id": "p1"})
        self.assertIn("reply_id_mismatch", str(caught.exception))
        with self.assertRaises(ConnectionError):
            client.request({"op": "ping", "id": "p2"})

    def test_reply_missing_the_request_id_is_refused(self) -> None:
        def strip_id(message: dict, conn: ct.Connection) -> dict:
            return {"ok": True, "op": "pong", "id": None}

        srv = self.server(strip_id)
        with UnixControlClient(srv.path) as client:
            with self.assertRaises(ct.FrameError):
                client.request({"op": "ping", "id": "p1"})

    def test_partial_reply_then_timeout_closes_the_client(self) -> None:
        """A frame cut off mid-body must not leave the rest to be read as the next reply."""
        os.makedirs(self.sock_dir, 0o700, exist_ok=True)
        path = os.path.join(self.sock_dir, "half.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        done = threading.Event()
        self.addCleanup(done.set)
        body = b'{"ok":true,"op":"stop","state":"stopped","id":"s1"}'

        def half_server() -> None:
            conn, _ = listener.accept()
            with conn:
                framing.read_frame(conn)
                conn.sendall(framing.HEADER.pack(len(body)) + body[:10])
                done.wait(2.0)
                try:
                    conn.sendall(body[10:])
                except BrokenPipeError:  # the client already gave up, which is the point
                    pass
                done.wait(2.0)

        threading.Thread(target=half_server, daemon=True).start()
        client = UnixControlClient(path, timeout_s=0.3)
        self.addCleanup(client.close)
        with self.assertRaises(TimeoutError):
            client.request({"op": "stop", "id": "s1"})
        with self.assertRaises(ConnectionError):
            client.request({"op": "ping", "id": "p1"})


@unittest.skipUnless(LINUX, "Linux is the tested path")
class ConnectionContextTests(_TmpCase):
    """The handler learns who is on the other end, so the layer above can scope (D3, I-42)."""

    def test_handler_gets_peer_credentials_and_a_per_connection_context(self) -> None:
        seen: list[tuple[int, ct.PeerCred, int]] = []

        def handler(message: dict, conn: ct.Connection) -> dict:
            conn.state["n"] = conn.state.get("n", 0) + 1
            seen.append((conn.conn_id, conn.peer, conn.state["n"]))
            return {"ok": True}

        srv = self.server(handler)
        with UnixControlClient(srv.path) as first, UnixControlClient(srv.path) as second:
            first.request({"op": "a"})
            first.request({"op": "b"})
            second.request({"op": "c"})
        self.assertEqual(len(seen), 3)
        (id_a, peer_a, n_a), (id_b, _, n_b), (id_c, peer_c, n_c) = seen
        self.assertEqual(id_a, id_b)
        self.assertNotEqual(id_a, id_c)
        self.assertEqual((n_a, n_b, n_c), (1, 2, 1))  # state is per connection
        for peer in (peer_a, peer_c):
            self.assertEqual(peer.uid, os.geteuid())
            self.assertEqual(peer.pid, os.getpid())

    def test_injected_peer_cred_is_what_the_handler_sees(self) -> None:
        engine = PeerCred(pid=4242, uid=os.geteuid(), gid=0)
        seen: list[ct.PeerCred] = []

        def handler(message: dict, conn: ct.Connection) -> dict:
            seen.append(conn.peer)
            return {"ok": True}

        srv = self.server(handler, peer_cred=lambda _s: engine)
        with UnixControlClient(srv.path) as client:
            client.request({"op": "approve"})
        self.assertEqual(seen, [engine])


@unittest.skipUnless(LINUX, "Linux is the tested path")
class ConnectionBoundTests(_TmpCase):
    """The connection cap and idle timeout are the transport's flood bounds (CX-026)."""

    def test_connection_over_the_cap_gets_a_typed_busy_frame_and_is_counted(self) -> None:
        recorder = _Recorder()
        srv = self.server(ct.with_ping_health(recorder), max_connections=1)
        holder = UnixControlClient(srv.path)
        self.addCleanup(holder.close)
        self.assertEqual(holder.request({"op": "ping"})["op"], "pong")
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with raw:
            raw.settimeout(5)
            raw.connect(srv.path)
            try:
                raw.sendall(framing.encode_frame({"op": "stop", "run_id": "r1"}))
            except (ConnectionResetError, BrokenPipeError):
                pass
            reply = framing.read_frame(raw)
        self.assertEqual(
            reply,
            {"ok": False, "error": {"code": "busy", "layer": "transport", "delivered": False, "retryable": True}},
        )
        self.assertTrue(_wait_for(lambda: srv.refused == ["max_connections"]), srv.refused)
        self.assertEqual(srv.accepted, 1)
        self.assertEqual(recorder.seen, [])
        # The held slot still works, and closing it hands the slot to a new peer.
        self.assertEqual(holder.request({"op": "ping"})["op"], "pong")
        holder.close()
        self.assertTrue(_wait_for(lambda: _new_peer_served(srv.path)))

    def test_stop_with_every_slot_held_gets_a_typed_refusal_not_a_silent_close(self) -> None:
        """S6/D3: a Stop the server cannot take must come back as 'not delivered', typed."""
        recorder = _Recorder()
        srv = self.server(ct.with_ping_health(recorder), max_connections=2)
        holders = [UnixControlClient(srv.path) for _ in range(2)]
        for holder in holders:
            self.addCleanup(holder.close)
            self.assertEqual(holder.request({"op": "ping"})["op"], "pong")
        for _ in range(5):  # either race (busy read before or after our write) gives the same answer
            with UnixControlClient(srv.path) as client:
                with self.assertRaises(ct.TransportBusy) as caught:
                    client.request({"op": "stop", "run_id": "r1", "id": "s1"})
                self.assertIsInstance(caught.exception, ConnectionError)
                self.assertNotIsInstance(caught.exception, ct.PeerRefused)
                with self.assertRaises(ConnectionError):
                    client.request({"op": "ping"})
        self.assertEqual(recorder.seen, [])
        holders[0].close()
        replies: list[dict] = []

        def stop_delivered() -> bool:
            try:
                with UnixControlClient(srv.path) as client:
                    replies.append(client.request({"op": "stop", "run_id": "r1", "id": "s2"}))
                    return True
            except ct.TransportBusy:  # the freed slot is released just after the close
                return False

        self.assertTrue(_wait_for(stop_delivered))
        self.assertEqual(replies[-1]["state"], "stop_requested")

    def test_foreign_uid_still_gets_no_frame_even_when_full(self) -> None:
        srv = self.server(max_connections=1, peer_cred=_foreign_uid)
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with raw:
            raw.settimeout(5)
            raw.connect(srv.path)
            try:
                reply = raw.recv(4096)
            except ConnectionResetError:
                reply = b""
        self.assertEqual(reply, b"")

    def test_peer_dripping_a_byte_at_a_time_is_dropped_at_the_frame_deadline(self) -> None:
        srv = self.server(max_connections=1, idle_timeout_s=0.5)
        frame = framing.encode_frame({"op": "ping", "pad": "x" * 64})
        drip = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with drip:
            drip.settimeout(5)
            drip.connect(srv.path)
            started = time.monotonic()
            closed_at = None
            for byte in frame:  # each gap is well under the deadline; the whole frame is not
                try:
                    drip.sendall(bytes([byte]))
                except (BrokenPipeError, ConnectionResetError):
                    closed_at = time.monotonic()
                    break
                time.sleep(0.1)
            if closed_at is None:
                self.fail("server kept a byte-dripping peer past the frame deadline")
            self.assertLess(closed_at - started, 3.0)
        self.assertTrue(_wait_for(lambda: _new_peer_served(srv.path)))

    def test_busy_client_between_frames_keeps_its_slot_within_the_deadline(self) -> None:
        srv = self.server(idle_timeout_s=0.6)
        with UnixControlClient(srv.path) as client:
            for _ in range(4):  # 4 x 0.3 s > one deadline: the clock restarts per request
                time.sleep(0.3)
                self.assertEqual(client.request({"op": "ping"})["op"], "pong")

    def test_idle_peer_is_dropped_and_gives_its_slot_back(self) -> None:
        srv = self.server(max_connections=1, idle_timeout_s=0.3)
        idle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with idle:
            idle.settimeout(5)
            idle.connect(srv.path)
            started = time.monotonic()
            self.assertEqual(idle.recv(4096), b"")  # server hangs up, no frame
            self.assertLess(time.monotonic() - started, 4.0)
        self.assertTrue(_wait_for(lambda: _new_peer_served(srv.path)))

    def test_idle_client_between_requests_is_dropped(self) -> None:
        srv = self.server(idle_timeout_s=0.3)
        client = UnixControlClient(srv.path)
        self.addCleanup(client.close)
        self.assertEqual(client.request({"op": "ping"})["op"], "pong")
        time.sleep(0.8)
        with self.assertRaises((ConnectionError, OSError, ct.FrameError)):
            client.request({"op": "ping"})


class RemoteBindRefusedTests(_TmpCase):
    NETWORK = (
        ("0.0.0.0", 8080),
        ("127.0.0.1", 0),
        ("::", 0, 0, 0),
        8080,
        "0.0.0.0:8080",
        "127.0.0.1:9000",
        "localhost:22",
        "[::1]:9000",
        "example.com:443",
        "localhost",
        "0.0.0.0",
        "::1",
        "tcp://127.0.0.1:9000",
        "http://localhost:8080",
        "https://example.com",
        "vsock://3:1024",
    )

    def test_network_addresses_raise_remote_bind_refused(self) -> None:
        for address in self.NETWORK:
            with self.subTest(address=address):
                with self.assertRaises(ct.RemoteBindRefused):
                    UnixControlServer(address, ct.with_ping_health())
                with self.assertRaises(ct.RemoteBindRefused):
                    ct.create_server(address, ct.with_ping_health(), platform="linux")
                with self.assertRaises(ct.RemoteBindRefused):
                    UnixControlClient(address)

    def test_remote_bind_refused_is_typed(self) -> None:
        self.assertTrue(issubclass(ct.RemoteBindRefused, ct.TransportRefused))
        self.assertTrue(issubclass(ct.RemoteBindRefused, ValueError))

    def test_abstract_and_relative_names_are_refused(self) -> None:
        for address in ("\0recursion-control", "", "run/control", b"\0abs"):
            with self.subTest(address=address):
                with self.assertRaises(ct.InsecureSocketPath):
                    UnixControlServer(address, ct.with_ping_health())

    def test_socket_name_cannot_escape_the_directory(self) -> None:
        for name in ("../x.sock", "a/b", "..", ""):
            with self.subTest(name=name):
                with self.assertRaises(ct.InsecureSocketPath):
                    UnixControlServer(self.sock_dir, ct.with_ping_health(), name=name)

    def test_unix_scheme_and_pathlike_are_accepted(self) -> None:
        self.assertEqual(ct.refuse_remote_bind("unix://" + self.sock_dir), self.sock_dir)
        self.assertEqual(ct.refuse_remote_bind(Path(self.sock_dir)), self.sock_dir)

    def test_package_source_opens_no_network_socket(self) -> None:
        """No AF_INET/AF_INET6/AF_VSOCK, no create_server/create_connection, no http/socketserver."""
        banned_attrs = {"AF_INET", "AF_INET6", "AF_VSOCK", "AF_PACKET", "create_server", "create_connection"}
        banned_modules = {"http", "socketserver", "asyncio", "ssl", "urllib", "xmlrpc"}
        sources = [p for p in PACKAGE_DIR.rglob("*.py") if "tests" not in p.relative_to(PACKAGE_DIR).parts]
        self.assertGreater(len(sources), 3)
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "socket":
                    self.assertNotIn(node.attr, banned_attrs, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn(alias.name.split(".")[0], banned_modules, f"{path.name}:{node.lineno}")
                if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    self.assertNotIn(node.module.split(".")[0], banned_modules, f"{path.name}:{node.lineno}")

    def test_cockpit_is_not_imported(self) -> None:
        for path in PACKAGE_DIR.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    self.assertNotIn("cockpit", name.lower(), f"{path.name}:{node.lineno}")
                    self.assertNotEqual(name.split(".")[0], "engine", f"{path.name}:{node.lineno}")


class WindowsStubTests(unittest.TestCase):
    def test_windows_server_and_client_raise_labelled_not_implemented(self) -> None:
        for platform in ("win32", "cygwin"):
            with self.subTest(platform=platform):
                with self.assertRaises(NotImplementedError) as server_err:
                    ct.create_server("C:\\run", ct.with_ping_health(), platform=platform)
                with self.assertRaises(ct.PlatformNotSupported) as client_err:
                    ct.create_client("C:\\run\\control.sock", platform=platform)
                for err in (server_err.exception, client_err.exception):
                    self.assertIn("F40", str(err))
                    self.assertIn("not implemented", str(err))
                    self.assertIsInstance(err, ct.TransportRefused)

    def test_windows_stub_is_labelled_in_docstrings(self) -> None:
        from recursion_core.control_transport import windows

        for obj in (windows, windows.WindowsControlServer, windows.WindowsControlClient):
            self.assertIn("F40", obj.__doc__ or "", obj)
            self.assertIn("stub", (obj.__doc__ or "").lower(), obj)

    def test_windows_stub_never_falls_back_to_tcp(self) -> None:
        with self.assertRaises(ct.PlatformNotSupported):
            ct.create_server(("127.0.0.1", 0), ct.with_ping_health(), platform="win32")

    def test_mac_is_designed_in_on_the_unix_path(self) -> None:
        self.assertIn("macOS", ct.__doc__ or "")
        srv = ct.create_server("/tmp/ct-mac-designed-in", ct.with_ping_health(), platform="darwin")
        self.assertIsInstance(srv, UnixControlServer)  # constructed, never started


if __name__ == "__main__":
    unittest.main()
