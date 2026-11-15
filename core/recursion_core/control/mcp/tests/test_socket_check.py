"""The socket check: every refusal by its reason, the live probe through the real owner-only transport, and
the per-call recheck. Each socket and directory is created under a fresh temporary folder; the real runtime
folder is never touched."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import unittest
from unittest import mock

from recursion_core.control.client import UNAVAILABLE
from recursion_core.control.mcp import socket_check
from recursion_core.control.mcp.socket_check import CheckedSocketAPI, SocketCheckFailed, check, check_path, locate
from recursion_core.control_transport import UnixControlServer
from recursion_core.control_transport.framing import MAX_FRAME_BYTES

CAN_SERVE = hasattr(socket, "AF_UNIX") and hasattr(socket, "SO_PEERCRED") and hasattr(os, "geteuid")
NEEDS_SERVER = unittest.skipUnless(
    CAN_SERVE, "the owner-only transport needs AF_UNIX, SO_PEERCRED and os.geteuid (Linux)")


class Daemon:
    """The daemon's socket face (daemon.control_adapter.DaemonControl.handle) over a scripted answer."""

    def __init__(self, answer=None) -> None:
        self.requests: list[dict] = []
        self._answer = answer or (lambda method, params: {"code": "ok", "runs": []})

    def handle(self, message, conn):
        self.requests.append(message)
        return {"ok": True, "result": self._answer(message.get("method"), message.get("params"))}


@NEEDS_SERVER
class SocketCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = tempfile.mkdtemp(prefix="mcp-sock-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.environ = {"XDG_RUNTIME_DIR": self.base}
        self.directory = os.path.join(self.base, "recursion")
        self.path = os.path.join(self.directory, "control.sock")

    def serve(self, daemon: Daemon, **kwargs) -> UnixControlServer:
        server = UnixControlServer(self.directory, daemon.handle, **kwargs)
        server.start()
        self.addCleanup(server.close)
        self.assertEqual(self.path, server.path)
        return server

    def bare_socket(self, mode: int = 0o600) -> None:
        """A socket file with nothing listening on it (bound, then closed; the file stays)."""

        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(self.path)
        os.chmod(self.path, mode)

    def refused(self, reason: str, environ=None) -> SocketCheckFailed:
        with self.assertRaises(SocketCheckFailed) as caught:
            check(self.environ if environ is None else environ)
        self.assertEqual(reason, caught.exception.reason, str(caught.exception))
        return caught.exception

    # -- the path ------------------------------------------------------------------------------------
    def test_the_socket_is_the_one_control_client_names(self) -> None:
        self.assertEqual(self.path, locate(self.environ))

    def test_a_relative_runtime_folder_is_refused(self) -> None:
        self.refused("socket_path_not_absolute", {"XDG_RUNTIME_DIR": "relative/run"})

    def test_a_missing_directory_is_refused_and_not_created(self) -> None:
        self.refused("socket_dir_missing")
        self.assertFalse(os.path.exists(self.directory))

    def test_a_directory_that_is_a_symlink_is_refused(self) -> None:
        real = os.path.join(self.base, "real")
        os.mkdir(real, 0o700)
        os.symlink(real, self.directory)
        self.refused("socket_dir_not_a_directory")

    def test_a_file_where_the_directory_should_be_is_refused(self) -> None:
        with open(self.directory, "w"):
            pass
        self.refused("socket_dir_not_a_directory")

    def test_a_directory_open_to_group_or_others_is_refused_and_not_repaired(self) -> None:
        for mode in (0o750, 0o705, 0o777):
            with self.subTest(mode=oct(mode)):
                self.bare_socket()
                os.chmod(self.directory, mode)
                self.refused("socket_dir_mode")
                self.assertEqual(mode, os.stat(self.directory).st_mode & 0o777)
                shutil.rmtree(self.directory)

    def test_a_directory_of_another_user_is_refused(self) -> None:
        self.bare_socket()
        with mock.patch.object(socket_check.os, "geteuid", return_value=os.geteuid() + 1):
            self.refused("socket_dir_foreign_owner")

    def test_a_missing_socket_is_refused(self) -> None:
        os.mkdir(self.directory, 0o700)
        self.refused("socket_missing")

    def test_a_regular_file_or_a_symlink_where_the_socket_should_be_is_refused(self) -> None:
        os.mkdir(self.directory, 0o700)
        with open(self.path, "w"):
            pass
        self.refused("socket_not_a_socket")
        os.unlink(self.path)
        other = os.path.join(self.base, "other.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(other)
        os.chmod(other, 0o600)
        os.symlink(other, self.path)
        self.refused("socket_not_a_socket")

    def test_a_socket_open_to_group_or_others_is_refused(self) -> None:
        self.bare_socket(0o660)
        self.refused("socket_mode")

    def test_a_socket_of_another_user_is_refused(self) -> None:
        self.bare_socket()
        real_uid = os.geteuid()
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            st = real_lstat(path, *args, **kwargs)
            if os.fspath(path) != self.path:
                return st
            fields = list(st)
            fields[4] = real_uid + 1  # st_uid
            return os.stat_result(fields)

        with mock.patch.object(socket_check.os, "lstat", side_effect=lstat):
            self.refused("socket_foreign_owner")

    # -- the live probe ------------------------------------------------------------------------------
    def test_a_stale_socket_with_nothing_listening_is_refused(self) -> None:
        self.bare_socket()
        failed = self.refused("daemon_unreachable")
        self.assertEqual("connect:ConnectionRefusedError", failed.detail)

    def test_a_live_daemon_passes_after_one_read(self) -> None:
        daemon = Daemon()
        self.serve(daemon)
        api = check(self.environ)
        self.assertIsInstance(api, CheckedSocketAPI)
        self.assertEqual([{"op": "control", "method": "run.list", "params": {"limit": 1}}], daemon.requests)
        self.assertEqual({"code": "ok", "runs": []}, api.call("run.list", {}))

    def test_a_control_answer_that_is_not_a_success_still_passes(self) -> None:
        self.serve(Daemon(lambda method, params: {"code": "registry_unavailable"}))
        self.assertIsInstance(check(self.environ), CheckedSocketAPI)

    def test_a_daemon_that_does_not_admit_this_peer_is_refused(self) -> None:
        daemon = Daemon()
        self.serve(daemon, admit=lambda cred: False)
        failed = self.refused("daemon_unreachable")
        self.assertEqual("daemon:scope_refused", failed.detail)
        self.assertEqual([], daemon.requests)

    def test_a_daemon_that_refuses_the_request_is_refused(self) -> None:
        class Refusing(Daemon):
            def handle(self, message, conn):
                self.requests.append(message)
                return {"ok": False, "error": {"code": "scope_refused", "detail": "peer is inside a run unit"}}

        self.serve(Refusing())
        self.assertEqual("daemon:scope_refused", self.refused("daemon_unreachable").detail)

    # -- every call ----------------------------------------------------------------------------------
    def test_a_call_the_control_frame_cannot_carry_is_not_delivered_and_nothing_is_written(self) -> None:
        daemon = Daemon()
        self.serve(daemon)
        api = check(self.environ)
        envelope = len(json.dumps({"op": "control", "method": "run.stop", "params": {"run_id": ""}},
                                  separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        fits = "r" * (MAX_FRAME_BYTES - envelope)  # the frame is exactly the transport's bound
        self.assertEqual({"code": UNAVAILABLE, "delivered": False, "detail": "request:frame_too_large"},
                         api.call("run.stop", {"run_id": fits + "r"}))
        self.assertEqual(1, len(daemon.requests))  # only the check's probe: nothing was written for the stop
        self.assertEqual({"code": "ok", "runs": []}, api.call("run.stop", {"run_id": fits}))
        self.assertEqual("run.stop", daemon.requests[-1]["method"])

    def test_every_call_rechecks_the_path_and_sends_nothing_when_it_fails(self) -> None:
        daemon = Daemon()
        self.serve(daemon)
        api = check(self.environ)
        os.chmod(self.directory, 0o755)
        self.assertEqual({"code": UNAVAILABLE, "delivered": False, "detail": "socket_check:socket_dir_mode"},
                         api.call("run.stop", {"run_id": "run-1"}))
        self.assertEqual(1, len(daemon.requests))  # only the probe
        os.chmod(self.directory, 0o700)
        self.assertEqual("ok", api.call("run.list", {})["code"])
        self.assertEqual(2, len(daemon.requests))

    def test_check_path_alone_passes_for_the_daemon_socket(self) -> None:
        self.serve(Daemon())
        self.assertIsNone(check_path(self.path))


class ProbeTest(unittest.TestCase):
    """The probe's verdict, with the path checks set aside (platform-neutral)."""

    def run_check(self, answer):
        class Fake:
            def __init__(self, path):
                self.path = path

            def call(self, method, params=None):
                return answer

        with mock.patch.object(socket_check, "locate", return_value="/run/x/recursion/control.sock"), \
                mock.patch.object(socket_check, "check_path", return_value=None):
            return check({}, make_api=Fake)

    def test_unavailable_or_shapeless_answers_are_refused(self) -> None:
        for answer, detail in (({"code": UNAVAILABLE, "delivered": False, "detail": "busy"}, "busy"),
                               ({"code": UNAVAILABLE, "delivered": "unknown"}, "no control answer"),
                               ({"code": 5}, "no control answer"), ({}, "no control answer"),
                               ("ok", "no control answer"), (None, "no control answer")):
            with self.subTest(answer=answer), self.assertRaises(SocketCheckFailed) as caught:
                self.run_check(answer)
            self.assertEqual(("daemon_unreachable", detail), (caught.exception.reason, caught.exception.detail))

    def test_any_control_code_passes(self) -> None:
        for code in ("ok", "registry_unavailable", "request_invalid"):
            self.assertEqual("/run/x/recursion/control.sock", self.run_check({"code": code}).path)


@unittest.skipUnless(os.name == "nt", "Windows-only: the Unix transport is missing there")
class WindowsTest(unittest.TestCase):
    def test_windows_is_refused_before_anything_is_touched(self) -> None:
        for environ in ({}, {"XDG_RUNTIME_DIR": "C:\\runtime"}):
            with self.assertRaises(SocketCheckFailed) as caught:
                check(environ)
            self.assertEqual("platform_unsupported", caught.exception.reason)


if __name__ == "__main__":
    unittest.main()
