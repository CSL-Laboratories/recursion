"""Owner-only Unix socket for the operator inspect reads: permissions, peer uid, framing, caps, client."""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from recursion_core.control import operator_reads_socket as transport
from recursion_core.control.api import ControlAPI
from recursion_core.control.operator_reads import OperatorReads, connect
from recursion_core.control.operator_reads_socket import (
    MAX_REQUEST_BYTES,
    OperatorReadsClient,
    OperatorReadsServer,
    PlatformNotSupported,
    SocketUnsafe,
    default_socket_path,
)
from recursion_core.control.tests.fakes import FakeKillTree, make_api

POSITIONS, DECISIONS, BUDGETS = (
    "operator.positions.inspect", "operator.decisions.inspect", "operator.budgets.inspect")
HAVE_UNIX = hasattr(socket, "AF_UNIX")
CAN_SERVE = HAVE_UNIX and hasattr(socket, "SO_PEERCRED") and hasattr(os, "getuid")
NEEDS_SERVER = unittest.skipUnless(
    CAN_SERVE, "the owner-only server needs AF_UNIX, SO_PEERCRED and os.getuid (Linux); it refuses to serve elsewhere")


def composed() -> OperatorReads:
    return OperatorReads({
        "positions": lambda: {"venue": "paper", "rows": [{"instrument_id": "AAA", "quantity": "2"}]},
        "decisions": lambda: {"venue": "paper", "rows": [{"decision_id": "d1", "outcome": "recorded"}]},
        "budgets": lambda: {"venue": "paper", "rows": [{"budget_id": "b1", "remaining": "0.00"}]},
    }, profile={"profile_id": "p-1", "signature_state": "unsigned"})


def raw(path: str, payload: bytes, *, timeout: float = 5.0) -> dict:
    """Send raw bytes on a fresh connection and return the one reply line as a dict."""

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(path)
        try:
            conn.sendall(payload)
        except OSError:
            pass
        return read_line(conn)


def read_line(conn: socket.socket) -> dict:
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(65536)
        if not chunk:
            raise AssertionError(f"connection closed before a full reply line: {buf!r}")
        buf += chunk
    line, _, rest = buf.partition(b"\n")
    assert rest == b"", rest
    return json.loads(line)


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="opreads-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.path = os.path.join(self.base, "recursion", "operator-reads.sock")

    def serve(self, reads: OperatorReads | None = None, **kwargs) -> OperatorReadsServer:
        server = OperatorReadsServer(reads or composed(), self.path, **kwargs).start()
        self.addCleanup(server.close)
        return server


@NEEDS_SERVER
class RoundTripTest(TempDirCase):
    def test_three_reads_through_the_real_socket(self) -> None:
        self.serve()
        client = connect(self.path)
        for method, key in ((POSITIONS, "positions"), (DECISIONS, "decisions"), (BUDGETS, "budgets")):
            reply = client.call(method, {})
            self.assertEqual((reply["code"], reply["status_code"], reply["command"]), ("ok", "ok", method))
            self.assertEqual([r["venue"] for r in reply["result"][key]], ["paper"])
            self.assertEqual(reply["profile"], {"profile_id": "p-1", "signature_state": "unsigned"})
            self.assertEqual((reply["authority_effect"], reply["order_path"]), ("none", "none"))
            self.assertNotIn("delivered", reply)
        self.assertEqual(client.call(POSITIONS, None)["code"], "ok")

    def test_the_socket_answer_equals_the_in_process_answer(self) -> None:
        reads = composed()
        self.serve(reads)
        over_socket, in_process = connect(self.path).call(BUDGETS, {}), reads.call(BUDGETS, {})
        for reply in (over_socket, in_process):
            del reply["request_id"], reply["served_at"]
        self.assertEqual(over_socket, in_process)

    def test_unsourced_reads_are_ok_empty_through_the_socket(self) -> None:
        self.serve(OperatorReads())
        reply = connect(self.path).call(DECISIONS, {})
        self.assertEqual((reply["code"], reply["result"], reply["empty_reason"]),
                         ("ok", {"decisions": []}, "no_decisions_source_attached"))
        self.assertEqual(reply["profile"], {"profile_id": None, "signature_state": "no_profile"})

    def test_run_methods_through_the_socket_are_method_not_allowed_and_touch_no_control_api(self) -> None:
        api, registry, kill, starter = make_api(FakeKillTree())
        self.serve()
        for method, params in (("run.stop", {"run_id": "run-1"}), ("run.start", {"tenant": "t", "engine": "e", "prompt": "p"}),
                               ("run.stop", {}), ("run.list", {}), ("operator.orders.inspect", {}),
                               ("operator.positions.close", {})):
            line = json.dumps({"method": method, "params": params}).encode() + b"\n"
            reply = raw(self.path, line)
            self.assertEqual((reply["code"], reply["status_code"], reply["result"]),
                             ("method_not_allowed", "method_not_allowed", {}), method)
        self.assertEqual((starter.calls, kill.requests, kill.confirms, registry.list()), ([], [], [], []))
        self.assertEqual(api.call("run.list", {})["runs"], [])

    def test_the_server_serves_only_an_operator_reads(self) -> None:
        api, *_ = make_api(FakeKillTree())
        self.assertIsInstance(api, ControlAPI)

        class Lookalike:
            def call(self, method, params):
                return {"code": "ok"}

        for other in (api, Lookalike(), None):
            with self.assertRaises(TypeError):
                OperatorReadsServer(other, self.path)
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))

    def test_non_empty_params_through_the_socket_are_request_invalid(self) -> None:
        self.serve()
        for params in ({"venue": "stock"}, [], "x", 1):
            reply = raw(self.path, json.dumps({"method": POSITIONS, "params": params}).encode() + b"\n")
            self.assertEqual(reply["code"], "request_invalid", params)
        self.assertEqual(raw(self.path, json.dumps({"method": POSITIONS}).encode() + b"\n")["code"], "ok")
        self.assertEqual(raw(self.path, json.dumps({"method": POSITIONS, "params": None}).encode() + b"\n")["code"], "ok")

    def test_a_malformed_request_line_is_request_invalid(self) -> None:
        self.serve()
        good = json.dumps({"method": POSITIONS, "params": {}}).encode()
        cases = {
            b"not json\n": "not_json",
            b"\xff\xfe\n": "not_json",
            b"[" * 3000 + b"\n": "not_json",
            b'["operator.positions.inspect", {}]\n': "request_shape",
            b'{"params": {}}\n': "request_shape",
            b'{"method": "operator.positions.inspect", "params": {}, "token": "t"}\n': "request_shape",
            good + b"\n" + good + b"\n": "trailing_data",
        }
        for payload, detail in cases.items():
            with self.subTest(detail=detail, head=payload[:20]):
                reply = raw(self.path, payload)
                self.assertEqual((reply["code"], reply["detail"]), ("request_invalid", detail))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5.0)
            conn.connect(self.path)
            conn.sendall(good)  # no newline, then end of stream
            conn.shutdown(socket.SHUT_WR)
            reply = read_line(conn)
        self.assertEqual((reply["code"], reply["detail"]), ("request_invalid", "incomplete_request"))

    def test_an_oversized_request_is_refused_and_the_cap_is_exact(self) -> None:
        self.serve()
        reply = raw(self.path, b'{"method": "' + b"a" * 20_000 + b'", "params": {}}\n')
        self.assertEqual((reply["code"], reply["command"], reply["result"]), ("request_too_large", None, {}))
        head = b'{"method":"operator.positions.inspect","params":{}'
        at_cap = head + b" " * (MAX_REQUEST_BYTES - len(head) - 2) + b"}\n"
        self.assertEqual(len(at_cap), MAX_REQUEST_BYTES)
        self.assertEqual(raw(self.path, at_cap)["code"], "ok")
        over = head + b" " * (MAX_REQUEST_BYTES - len(head) - 1) + b"}\n"
        self.assertEqual(raw(self.path, over)["code"], "request_too_large")
        self.assertEqual(connect(self.path).call(POSITIONS, {})["code"], "ok")

    def test_a_slow_client_hits_the_read_deadline(self) -> None:
        self.serve(read_deadline_s=0.3)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5.0)
            conn.connect(self.path)
            began = time.monotonic()
            conn.sendall(b'{"method": "operator.positions.')
            reply = read_line(conn)
            waited = time.monotonic() - began
        self.assertEqual((reply["code"], reply["status_code"]), ("request_timeout", "request_timeout"))
        self.assertGreaterEqual(waited, 0.25)
        self.assertLess(waited, 4.0)
        # A client that trickles bytes does not extend the deadline: it is one deadline per connection.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5.0)
            conn.connect(self.path)
            try:
                for _ in range(40):
                    conn.sendall(b" ")
                    time.sleep(0.05)
            except OSError:
                pass
            reply = read_line(conn)
        self.assertEqual(reply["code"], "request_timeout")
        self.assertEqual(connect(self.path).call(POSITIONS, {})["code"], "ok")

    def test_connections_over_the_cap_are_refused_not_queued(self) -> None:
        self.serve(max_connections=2, read_deadline_s=3.0)
        held = []
        try:
            for _ in range(2):
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                held.append(conn)
                conn.settimeout(10.0)
                conn.connect(self.path)
            began = time.monotonic()
            extra = connect(self.path, timeout_s=5.0).call(POSITIONS, {})
            self.assertEqual((extra["code"], extra["status_code"], extra["result"]), ("busy", "busy", {}))
            self.assertLess(time.monotonic() - began, 2.5)  # answered at once, not after a slot frees
            # The held connections are still served: each can complete its own request.
            held[0].sendall(json.dumps({"method": BUDGETS, "params": {}}).encode() + b"\n")
            self.assertEqual(read_line(held[0])["code"], "ok")
        finally:
            for conn in held:
                conn.close()
        deadline = time.monotonic() + 10.0
        while True:  # the slots free once the held connections end
            reply = connect(self.path).call(POSITIONS, {})
            if reply["code"] == "ok" or time.monotonic() > deadline:
                break
            time.sleep(0.02)
        self.assertEqual(reply["code"], "ok")

    def test_many_clients_at_once(self) -> None:
        self.serve(max_connections=64)
        replies, lock = [], threading.Lock()

        def read(n: int) -> None:
            reply = connect(self.path).call((POSITIONS, DECISIONS, BUDGETS)[n % 3], {})
            with lock:
                replies.append(reply)

        threads = [threading.Thread(target=read, args=(n,)) for n in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([r["code"] for r in replies], ["ok"] * 24)
        self.assertEqual(len({r["request_id"] for r in replies}), 24)


@NEEDS_SERVER
class OwnerOnlyTest(TempDirCase):
    def test_directory_is_0700_and_socket_is_0600(self) -> None:
        old = os.umask(0o000)  # even under an open umask
        try:
            server = self.serve()
        finally:
            os.umask(old)
        directory = os.lstat(os.path.dirname(self.path))
        sock = os.lstat(self.path)
        self.assertEqual(stat.S_IMODE(directory.st_mode), 0o700)
        self.assertTrue(stat.S_ISSOCK(sock.st_mode))
        self.assertEqual(stat.S_IMODE(sock.st_mode), 0o600)
        self.assertEqual((directory.st_uid, sock.st_uid), (os.getuid(), os.getuid()))
        self.assertEqual(os.umask(old), old)  # the caller's umask is restored
        self.assertEqual(server.path, self.path)

    def test_refuses_a_directory_open_to_group_or_other(self) -> None:
        directory = os.path.dirname(self.path)
        for mode in (0o750, 0o705, 0o701, 0o770, 0o777):
            with self.subTest(mode=oct(mode)):
                os.mkdir(directory)
                os.chmod(directory, mode)
                with self.assertRaises(SocketUnsafe):
                    OperatorReadsServer(composed(), self.path).start()
                self.assertEqual(os.listdir(directory), [])
                self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), mode)  # not silently "fixed"
                os.rmdir(directory)

    def test_refuses_a_directory_that_is_a_symlink_or_a_file_or_not_ours(self) -> None:
        directory = os.path.dirname(self.path)
        real = os.path.join(self.base, "real")
        os.mkdir(real, 0o700)
        os.symlink(real, directory)
        with self.assertRaises(SocketUnsafe):
            OperatorReadsServer(composed(), self.path).start()
        os.unlink(directory)
        with open(directory, "w", encoding="utf-8"):
            pass
        with self.assertRaises(SocketUnsafe):
            OperatorReadsServer(composed(), self.path).start()
        os.unlink(directory)
        os.mkdir(directory, 0o700)
        with mock.patch.object(transport, "_getuid", lambda: os.getuid() + 1):  # as if another user owned it
            with self.assertRaises(SocketUnsafe):
                OperatorReadsServer(composed(), self.path).start()
        self.assertEqual(os.listdir(directory), [])

    def test_a_missing_parent_is_an_error_not_a_created_tree(self) -> None:
        path = os.path.join(self.base, "absent", "recursion", "operator-reads.sock")
        with self.assertRaises(OSError):
            OperatorReadsServer(composed(), path).start()
        self.assertFalse(os.path.exists(os.path.join(self.base, "absent")))

    def test_a_peer_of_another_uid_is_refused_before_its_request_is_read(self) -> None:
        calls = []

        def counted():
            calls.append(1)
            return {"venue": "paper", "rows": []}

        seen = []

        def other_uid(conn: socket.socket) -> int:
            seen.append(transport._peer_uid(conn))
            return os.getuid() + 1

        self.serve(OperatorReads({"positions": counted}), peer_uid=other_uid)
        reply = connect(self.path).call(POSITIONS, {})
        self.assertEqual((reply["code"], reply["status_code"], reply["result"]), ("peer_refused", "peer_refused", {}))
        self.assertEqual(calls, [])
        self.assertEqual(seen, [os.getuid()])  # the real SO_PEERCRED answer is this process's uid

        def broken(conn: socket.socket) -> int:
            raise OSError("no credentials")

        server = OperatorReadsServer(OperatorReads({"positions": counted}),
                                     os.path.join(self.base, "recursion", "second.sock"), peer_uid=broken).start()
        self.addCleanup(server.close)
        self.assertEqual(connect(server.path).call(POSITIONS, {})["code"], "peer_refused")
        self.assertEqual(calls, [])

    def test_the_client_refuses_a_server_of_another_uid(self) -> None:
        self.serve()
        with mock.patch.object(transport, "_peer_uid", lambda conn: os.getuid() + 1):
            reply = OperatorReadsClient(self.path).call(POSITIONS, {})
        self.assertEqual((reply["code"], reply["delivered"], reply["detail"]),
                         ("control_api_unavailable", False, "server_not_this_user"))

    def test_close_removes_only_its_own_socket_file(self) -> None:
        server = OperatorReadsServer(composed(), self.path).start()
        other = os.path.join(os.path.dirname(self.path), "control.sock")
        with open(other, "w", encoding="utf-8"):
            pass
        server.close()
        server.close()  # idempotent
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["control.sock"])
        with self.assertRaises(RuntimeError):
            server.start()
        # A file that replaced the socket at the same path is not this server's to remove.
        second = OperatorReadsServer(composed(), self.path).start()
        os.unlink(self.path)
        with open(self.path, "w", encoding="utf-8"):
            pass
        second.close()
        self.assertTrue(os.path.isfile(self.path))

    def test_a_live_socket_is_not_taken_over_and_a_stale_one_is_replaced(self) -> None:
        first = self.serve()
        with self.assertRaises(SocketUnsafe):
            OperatorReadsServer(composed(), self.path).start()
        self.assertEqual(connect(self.path).call(POSITIONS, {})["code"], "ok")  # the first still serves
        first.close()
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(self.path)
        stale.close()  # bound, never listening: a leftover file
        self.assertTrue(os.path.exists(self.path))
        with OperatorReadsServer(composed(), self.path) as second:
            self.assertEqual(connect(second.path).call(POSITIONS, {})["code"], "ok")
            self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)
        self.assertFalse(os.path.exists(self.path))
        with open(self.path, "w", encoding="utf-8"):
            pass
        with self.assertRaises(SocketUnsafe):  # a regular file at the path is never removed
            OperatorReadsServer(composed(), self.path).start()
        self.assertTrue(os.path.isfile(self.path))

    def test_a_reply_over_the_cap_is_answered_typed(self) -> None:
        big = [{"instrument_id": "A" * 1000, "n": n} for n in range(2000)]
        self.serve(OperatorReads({"positions": lambda: {"venue": "paper", "rows": big}}))
        reply = connect(self.path).call(POSITIONS, {})
        self.assertEqual((reply["code"], reply["status_code"], reply["result"]), ("reply_too_large", "reply_too_large", {}))


class DefaultPathTest(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "getuid"), "no uids on this platform")
    def test_default_path_rule(self) -> None:
        uid = os.getuid()
        self.assertEqual(default_socket_path({"XDG_RUNTIME_DIR": "/run/user/4242"}),
                         "/run/user/4242/recursion/operator-reads.sock")
        for environ in ({}, {"XDG_RUNTIME_DIR": ""}, {"XDG_RUNTIME_DIR": "relative/dir"}):
            self.assertEqual(default_socket_path(environ), f"/run/user/{uid}/recursion/operator-reads.sock")
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/tmp/xdg-test"}):
            self.assertEqual(default_socket_path(), "/tmp/xdg-test/recursion/operator-reads.sock")
        self.assertNotIn("control.sock", default_socket_path({}))


class PlatformTest(unittest.TestCase):
    """What macOS (no SO_PEERCRED) and Windows (no AF_UNIX, no uids) get, simulated on any platform."""

    def test_server_refuses_to_serve_without_peer_credentials(self) -> None:
        with mock.patch.object(transport, "_SO_PEERCRED", None):
            with self.assertRaises(PlatformNotSupported):
                OperatorReadsServer(OperatorReads(), "/nonexistent/recursion/operator-reads.sock").start()
        with mock.patch.object(transport, "_AF_UNIX", None):
            with self.assertRaises(PlatformNotSupported):
                OperatorReadsServer(OperatorReads(), "/nonexistent/recursion/operator-reads.sock").start()

    def test_client_answers_platform_unsupported_without_af_unix_or_uids(self) -> None:
        with mock.patch.object(transport, "_AF_UNIX", None):
            reply = connect("/nonexistent/operator-reads.sock").call(POSITIONS, {})
        self.assertEqual(reply, {"code": "control_api_unavailable", "status_code": "control_api_unavailable",
                                 "delivered": False, "detail": "platform_unsupported"})

        def no_uids() -> int:
            raise PlatformNotSupported("no uids on this platform")

        with mock.patch.object(transport, "_getuid", no_uids):
            if HAVE_UNIX:
                reply = connect().call(POSITIONS, {})
                self.assertEqual((reply["code"], reply["delivered"], reply["detail"]),
                                 ("control_api_unavailable", False, "platform_unsupported"))
            with self.assertRaises(PlatformNotSupported):
                default_socket_path({})

    def test_client_refuses_non_inspect_names_locally_on_every_platform(self) -> None:
        client = connect("/nonexistent/recursion/operator-reads.sock", timeout_s=0.2)
        for method in ("run.stop", "run.start", "run.list", "operator.positions.close", "operator..inspect",
                       "operator.Positions.inspect", "operator.positions.inspect\n", "", None, 5,
                       "operator." + "a" * 5000 + ".inspect"):
            reply = client.call(method, {})
            self.assertEqual((reply["code"], reply["status_code"], reply["delivered"]),
                             ("method_not_allowed", "method_not_allowed", False), repr(method)[:40])
        for params in ({"run_id": "r"}, [], "x", 0):
            reply = client.call(POSITIONS, params)
            self.assertEqual((reply["code"], reply["delivered"]), ("request_invalid", False))


@NEEDS_SERVER
class ClientNeverRaisesTest(TempDirCase):
    def test_no_server(self) -> None:
        for path in (self.path, os.path.join(self.base, "x" * 300), self.base, "\0bad"):
            reply = connect(path, timeout_s=0.5).call(POSITIONS, {})
            self.assertEqual((reply["code"], reply["status_code"], reply["delivered"]),
                             ("control_api_unavailable", "control_api_unavailable", False), path)
            self.assertTrue(reply["detail"].startswith("connect_failed: "), reply)
        for timeout in ("soon", None, float("nan"), float("inf"), 0, -1.0):
            reply = connect(self.path, timeout_s=timeout).call(POSITIONS, {})
            self.assertEqual((reply["code"], reply["delivered"], reply["detail"]),
                             ("control_api_unavailable", False, "timeout_invalid"), timeout)

    def test_a_closed_server_is_unreachable(self) -> None:
        server = self.serve()
        self.assertEqual(connect(self.path).call(POSITIONS, {})["code"], "ok")
        server.close()
        reply = connect(self.path).call(POSITIONS, {})
        self.assertEqual((reply["code"], reply["delivered"]), ("control_api_unavailable", False))

    def fake_server(self, respond) -> str:
        """A bare listener that reads the request line and then does ``respond(conn)``."""

        self.fakes = getattr(self, "fakes", 0) + 1
        path = os.path.join(self.base, f"fake-{self.fakes}.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(4)
        listener.settimeout(0.1)
        stop = threading.Event()

        def loop() -> None:
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with conn:
                    conn.settimeout(5.0)
                    try:
                        buf = b""
                        while b"\n" not in buf:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            buf += chunk
                        respond(conn)
                    except OSError:
                        pass

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()

        def cleanup() -> None:
            stop.set()
            thread.join(timeout=5.0)
            listener.close()

        self.addCleanup(cleanup)
        return path

    def test_server_closed_mid_reply_is_delivered_unknown(self) -> None:
        responses = {
            "no_complete_reply": lambda conn: conn.sendall(b'{"code": "ok", "result": {"positions": ['),
            "no_complete_reply ": lambda conn: None,
            "reply_invalid": lambda conn: conn.sendall(b"not json\n"),
            "reply_invalid ": lambda conn: conn.sendall(b'["ok"]\n'),
            "reply_invalid  ": lambda conn: conn.sendall(b'{"status_code": "ok"}\n'),
            "reply_invalid   ": lambda conn: conn.sendall(b"\xff\n"),
            "reply_timeout": lambda conn: time.sleep(0.6),
        }
        for detail, respond in responses.items():
            with self.subTest(detail=detail):
                path = self.fake_server(respond)
                reply = connect(path, timeout_s=0.3).call(POSITIONS, {})
                self.assertEqual(reply, {"code": "control_api_unavailable", "status_code": "control_api_unavailable",
                                         "delivered": "unknown", "detail": detail.strip()})

    def test_a_reply_over_the_client_cap_is_delivered_unknown(self) -> None:
        def flood(conn: socket.socket) -> None:
            conn.sendall(b"x" * (transport.MAX_REPLY_BYTES + 200_000))

        reply = connect(self.fake_server(flood), timeout_s=5.0).call(POSITIONS, {})
        self.assertEqual((reply["code"], reply["delivered"], reply["detail"]),
                         ("control_api_unavailable", "unknown", "reply_too_large"))

    def test_a_complete_reply_is_returned_as_sent(self) -> None:
        path = self.fake_server(lambda conn: conn.sendall(b'{"code": "ok", "status_code": "ok", "result": {}}\n'))
        self.assertEqual(connect(path).call(POSITIONS, {}), {"code": "ok", "status_code": "ok", "result": {}})


if __name__ == "__main__":
    unittest.main()
