"""Windows named-pipe door, decided on Linux against a recording pipe.

Today ``WindowsControlServer(...)`` raises ``PlatformNotSupported``. After the
door lands, the same calls against ``RecordingPipe`` check the create flags and
the user-only SDDL, a foreign token shut unread, a failed impersonation, an
engine-member pid, a taken pipe name, a foreign server sid, busy and the
reserved Stop slot, and a remote pipe name. The page sets
``SO_EXCLUSIVEADDRUSE`` before bind when the platform is ``nt``.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
import unittest
from unittest import mock

from recursion_core import control_transport as ct
from recursion_core.control_transport.peercred import PeerCred
from recursion_core.control_transport.windows import WindowsControlClient, WindowsControlServer
from recursion_core.daemon.gui import _Handler, _Server
from recursion_core.win32.pipe import (
    FILE_FLAG_FIRST_PIPE_INSTANCE,
    PIPE_ACCESS_DUPLEX,
    PIPE_READMODE_BYTE,
    PIPE_REJECT_REMOTE_CLIENTS,
    PIPE_TYPE_BYTE,
    PIPE_WAIT,
    SERVER_PID,
    RecordingPipe,
    pipe_name,
)

USER = "S-1-5-21-111-222-333-1001"
OTHER = "S-1-5-21-111-222-333-9999"
ENGINE_PID = 4242
_ACE = re.compile(r"\(([^)]*)\)")
_BANNED = {"WD", "AU", "BU", "S-1-1-0", "S-1-5-11"}


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _dacl(sddl: str) -> tuple[str, str]:
    start = sddl.find("D:")
    if start < 0:
        raise AssertionError(sddl)
    rest = sddl[start + 2 :]
    flags: list[str] = []
    index = 0
    while index < len(rest) and rest[index] != "(":
        flags.append(rest[index])
        index += 1
    body = rest[index:]
    sacl = body.find("S:")
    if sacl >= 0:
        body = body[:sacl]
    return "".join(flags), body


def _aces(sddl: str) -> list[tuple[str, str, str, str]]:
    _flags, body = _dacl(sddl)
    found = []
    for raw in _ACE.findall(body):
        parts = raw.split(";")
        if len(parts) != 6:
            raise AssertionError(raw)
        found.append((parts[0], parts[1], parts[2], parts[5]))
    return found


def _ok(_message, _conn):
    return {"ok": True}


class WindowsPipeDoorTests(unittest.TestCase):
    def test_create_carries_first_instance_remote_reject_and_a_user_only_dacl(self) -> None:
        seen: list[PeerCred] = []

        def handler(message, conn):
            seen.append(conn.peer)
            return {"ok": True, "op": message.get("op"), "id": message.get("id")}

        fake = RecordingPipe(USER)
        srv = WindowsControlServer("C:\\run", handler, pipe=fake)
        self.addCleanup(srv.close)
        srv.start()
        creates = [call for call in fake.snapshot() if call[0] == "CreateNamedPipeW"]
        self.assertEqual(len(creates), 1)
        _op, name, access, mode, flags, sddl = creates[0]
        self.assertEqual(name, pipe_name(USER))
        self.assertEqual(name, srv.path)
        self.assertEqual(access, PIPE_ACCESS_DUPLEX)
        self.assertEqual(mode, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT)
        self.assertTrue(flags & FILE_FLAG_FIRST_PIPE_INSTANCE)
        self.assertTrue(flags & PIPE_REJECT_REMOTE_CLIENTS)
        dacl_flags, _body = _dacl(sddl)
        self.assertEqual(dacl_flags.replace("AI", "").replace("AR", ""), "P")
        aces = _aces(sddl)
        self.assertEqual(len(aces), 1)
        ace_type, ace_flags, _rights, account = aces[0]
        self.assertEqual(ace_type, "A")
        self.assertNotIn("ID", ace_flags)
        self.assertEqual(account.upper(), USER.upper())
        self.assertNotIn(account.upper(), _BANNED)
        self.assertNotIn("SY", sddl.upper())
        with WindowsControlClient(srv.path, pipe=fake) as client:
            self.assertEqual(client.request({"op": "ping", "id": 7}), {"ok": True, "op": "ping", "id": 7})
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0].pid)
        self.assertIsNone(seen[0].uid)
        self.assertEqual(seen[0].sid, USER)
        self.assertIsNone(seen[0].gid)

    def test_a_bad_sid_refuses_the_dacl_and_creates_nothing(self) -> None:
        fake = RecordingPipe("Everyone")
        with self.assertRaises(ct.InsecureSocketPath) as caught:
            WindowsControlServer("C:\\run", _ok, pipe=fake)
        self.assertIn("pipe_dacl_unprotected", str(caught.exception))
        self.assertFalse(any(call[0] == "CreateNamedPipeW" for call in fake.snapshot()))

    def test_another_sid_is_shut_unread_and_revert_is_recorded_on_an_exception_too(self) -> None:
        seen: list = []
        fake = RecordingPipe(USER)
        srv = WindowsControlServer("C:\\run", lambda message, conn: seen.append(message) or {"ok": True}, pipe=fake)
        self.addCleanup(srv.close)
        srv.start()

        fake.client_sid = OTHER
        fake.open_client(srv.path)
        self.assertTrue(_wait(lambda: any("peer sid" in item for item in srv.refused)), srv.refused)
        self.assertEqual(seen, [])
        names = [call[0] for call in fake.snapshot()]
        self.assertIn("RevertToSelf", names)
        self.assertNotIn("ReadFile", names)
        revert_at = names.index("RevertToSelf")
        self.assertLess(names.index("ImpersonateNamedPipeClient"), revert_at)
        self.assertLess(names.index("GetNamedPipeClientProcessId"), names.index("ImpersonateNamedPipeClient"))

        fake.fail_token = True
        fake.client_sid = USER
        before = len(fake.snapshot())
        fake.open_client(srv.path)
        self.assertTrue(_wait(lambda: len(srv.refused) >= 2), srv.refused)
        self.assertEqual(seen, [])
        later = [call[0] for call in fake.snapshot()[before:]]
        self.assertIn("RevertToSelf", later)
        self.assertNotIn("ReadFile", later)

    def test_failed_impersonation_is_refused_and_never_served(self) -> None:
        seen: list = []
        fake = RecordingPipe(USER)
        srv = WindowsControlServer("C:\\run", lambda message, conn: seen.append(message) or {"ok": True}, pipe=fake)
        self.addCleanup(srv.close)
        srv.start()
        fake.fail_impersonate = True
        fake.open_client(srv.path)
        self.assertTrue(_wait(lambda: len(srv.refused) >= 1), srv.refused)
        self.assertEqual(seen, [])
        self.assertEqual(srv.accepted, 0)
        self.assertNotIn("ReadFile", [call[0] for call in fake.snapshot()])

    def test_an_engine_member_pid_is_scope_refused_without_a_slot(self) -> None:
        seen: list = []

        def admit(peer: PeerCred):
            if peer.pid == ENGINE_PID:
                return False
            if peer.pid == ENGINE_PID + 1:
                raise RuntimeError("membership unknown")
            return True

        fake = RecordingPipe(USER)
        srv = WindowsControlServer(
            "C:\\run",
            lambda message, conn: seen.append(message) or {"ok": True, "op": message.get("op")},
            pipe=fake,
            max_connections=1,
            admit=admit,
        )
        self.addCleanup(srv.close)
        srv.start()
        for pid in (ENGINE_PID, ENGINE_PID + 1):
            fake.client_pid = pid
            with self.assertRaises(ct.PeerNotAdmitted):
                WindowsControlClient(srv.path, pipe=fake, timeout_s=2.0).request({"op": "ping"})
        self.assertEqual(seen, [])
        self.assertEqual(srv.accepted, 0)
        self.assertTrue(_wait(lambda: srv.refused == ["not_admitted", "not_admitted"]), srv.refused)
        fake.client_pid = 5100
        with WindowsControlClient(srv.path, pipe=fake, timeout_s=2.0) as client:
            self.assertEqual(client.request({"op": "ping"})["op"], "ping")
        self.assertEqual(srv.accepted, 1)

    def test_a_taken_pipe_name_refuses_and_does_not_serve(self) -> None:
        seen: list = []
        fake = RecordingPipe(USER)
        fake.take_name(pipe_name(USER))
        srv = WindowsControlServer("C:\\run", lambda message, conn: seen.append(message) or {"ok": True}, pipe=fake)
        self.addCleanup(srv.close)
        with self.assertRaises(ct.InsecureSocketPath) as caught:
            srv.start()
        self.assertIn("pipe_name_taken", str(caught.exception))
        self.assertEqual(seen, [])
        self.assertEqual(srv.accepted, 0)
        self.assertIsNone(srv._thread)
        with self.assertRaises(ct.PeerRefused):
            WindowsControlClient(pipe_name(USER), pipe=fake)

    def test_the_client_refuses_a_foreign_sid_server_before_any_write(self) -> None:
        fake = RecordingPipe(USER)
        srv = WindowsControlServer("C:\\run", _ok, pipe=fake)
        self.addCleanup(srv.close)
        srv.start()
        fake.set_process_sid(SERVER_PID, OTHER)
        before = fake.snapshot()
        with self.assertRaises(ct.PeerRefused):
            WindowsControlClient(srv.path, pipe=fake)
        after = fake.snapshot()[len(before) :]
        self.assertNotIn("WriteFile", [call[0] for call in after])
        self.assertIn("GetNamedPipeServerProcessId", [call[0] for call in after])
        self.assertIn("OpenProcessToken", [call[0] for call in after])

        fake.deny_process(SERVER_PID)
        before = fake.snapshot()
        with self.assertRaises(ct.PeerRefused):
            WindowsControlClient(srv.path, pipe=fake)
        after = fake.snapshot()[len(before) :]
        self.assertNotIn("WriteFile", [call[0] for call in after])

    def test_busy_and_the_reserved_stop_slot(self) -> None:
        seen: list[str] = []
        release = threading.Event()
        started = threading.Event()

        def handler(message, _conn):
            op = message.get("op")
            seen.append(op)
            if op == "hold":
                started.set()
                self.assertTrue(release.wait(5.0))
            return {"ok": True, "op": op, "id": message.get("id")}

        fake = RecordingPipe(USER)
        srv = WindowsControlServer(
            "C:\\run",
            handler,
            pipe=fake,
            max_connections=1,
            idle_timeout_s=30.0,
            priority=lambda request: request.get("op") == "stop",
            reserved_connections=1,
            reserved_timeout_s=30.0,
        )
        self.addCleanup(srv.close)
        srv.start()
        box: dict = {}

        def hold() -> None:
            client = WindowsControlClient(srv.path, pipe=fake, timeout_s=5.0)
            box["client"] = client
            try:
                box["reply"] = client.request({"op": "hold", "id": 1})
            except BaseException as exc:  # pragma: no cover - the assertion below reports it
                box["error"] = exc

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5.0)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(5.0))
        with self.assertRaises(ct.TransportBusy):
            WindowsControlClient(srv.path, pipe=fake, timeout_s=2.0).request({"op": "ping", "id": 2})
        def _workers() -> int:
            with srv._lock:
                return len(srv._workers)

        self.assertTrue(_wait(lambda: _workers() == 1), _workers())
        self.assertNotIn("ping", seen)
        with WindowsControlClient(srv.path, pipe=fake, timeout_s=2.0) as stopper:
            reply = stopper.request({"op": "stop", "id": 3})
        self.assertEqual(reply["op"], "stop")
        self.assertIn("stop", seen)
        release.set()
        worker.join(5.0)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", box)
        self.assertEqual(box["reply"]["op"], "hold")
        box["client"].close()

    def test_reserved_connections_need_a_priority_test(self) -> None:
        fake = RecordingPipe(USER)
        with self.assertRaises(ValueError):
            WindowsControlServer("C:\\run", _ok, pipe=fake, reserved_connections=1)

    def test_a_remote_pipe_name_is_refused(self) -> None:
        for name in (r"\\host\pipe\x", "//host/pipe/x", r"\\?\UNC\host\pipe\x"):
            with self.subTest(name=name):
                with self.assertRaises(ct.RemoteBindRefused):
                    WindowsControlServer(name, _ok)
                with self.assertRaises(ct.RemoteBindRefused):
                    WindowsControlClient(name)
        # The factory on win32, with no pipe backend, is still the labelled refusal.
        with self.assertRaises(ct.PlatformNotSupported) as caught:
            ct.create_server(r"C:\run", _ok, platform="win32")
        self.assertIn("F40", str(caught.exception))
        self.assertIn("not implemented", str(caught.exception))

    def test_peer_cred_gains_sid_and_posix_construction_stays(self) -> None:
        posix = PeerCred(pid=1, uid=2, gid=3)
        self.assertIsNone(posix.sid)
        self.assertEqual((posix.pid, posix.uid, posix.gid), (1, 2, 3))
        windows_peer = PeerCred(pid=9, uid=None, gid=None, sid=USER)
        self.assertEqual(windows_peer.sid, USER)
        self.assertIsNone(windows_peer.uid)


class PageExclusiveBindTests(unittest.TestCase):
    def test_nt_sets_exclusive_addr_use_before_bind(self) -> None:
        events: list[tuple] = []

        class _RecordingSocket:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def setsockopt(self, level, opt, value) -> None:
                events.append(("setsockopt", level, opt, value))

            def bind(self, address) -> None:
                events.append(("bind", address))

            def listen(self, backlog) -> None:
                events.append(("listen", backlog))

            def getsockname(self):
                return ("127.0.0.1", 9)

            def close(self) -> None:
                events.append(("close",))

        option = getattr(socket, "SO_EXCLUSIVEADDRUSE", ~socket.SO_REUSEADDR)
        with mock.patch("socket.socket", _RecordingSocket), mock.patch("os.name", "nt"):
            server = _Server(("127.0.0.1", 0), _Handler)
        try:
            kinds = [event[0] for event in events]
            self.assertIn("setsockopt", kinds)
            self.assertIn("bind", kinds)
            self.assertLess(kinds.index("setsockopt"), kinds.index("bind"))
            recorded = events[kinds.index("setsockopt")]
            self.assertEqual(recorded[1], socket.SOL_SOCKET)
            self.assertEqual(recorded[2], option)
            self.assertEqual(recorded[3], 1)
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
