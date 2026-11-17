"""The stand-in daemon keeps the socket's rules, and each way a request can end is answered as fixtures/delivery.json says.

The answers are the CLI's socket client's, made to happen on a real owner-only socket: the desktop shows the
same code and the same ``delivered`` for each.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from fake_socket import FakeDaemon, SilentDaemon
from recursion_core.control.client import SocketAPI
from recursion_core.control_transport.errors import InsecureSocketPath

DELIVERY = json.loads((Path(__file__).resolve().parent / "fixtures" / "delivery.json").read_text(encoding="utf-8"))
CASES = {case["case"]: case for case in DELIVERY["cases"]}


def is_stop(message: dict) -> bool:
    # The daemon's own test (daemon/control_adapter.py is_stop_request), restated so these checks need no daemon.
    return message.get("op") == "control" and message.get("method") == "run.stop"


class Held:
    """An answer that holds its connection's slot until released, so a test knows the slot is taken."""

    def __init__(self, answer: dict) -> None:
        self.entered, self.release, self._answer = threading.Event(), threading.Event(), answer

    def __call__(self, params: dict) -> dict:
        self.entered.set()
        self.release.wait()
        return self._answer


class DeliveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desktop-delivery-")

    def call(self, path: str, case: str) -> dict:
        request = CASES[case]["request"]
        return SocketAPI(path).call(request["method"], request["params"])

    def test_an_answer_is_passed_on_as_it_came(self) -> None:
        for name in ("answered", "stop_pending"):
            with self.subTest(name):
                case = CASES[name]
                with FakeDaemon(tempfile.mkdtemp(dir=self.dir), {case["request"]["method"]: case["answer"]}) as d:
                    self.assertEqual(self.call(d.path, name), case["answer"])

    def test_no_daemon(self) -> None:
        self.assertEqual(self.call(os.path.join(self.dir, "control.sock"), "no_daemon"), CASES["no_daemon"]["answer"])

    def test_busy(self) -> None:
        held = Held({"code": "ok"})
        with FakeDaemon(self.dir, {"run.list": held, "run.status": {"code": "ok"}}, max_connections=1) as daemon:
            first = threading.Thread(target=SocketAPI(daemon.path).call, args=("run.list", {}))
            first.start()
            self.addCleanup(first.join)
            self.addCleanup(held.release.set)
            held.entered.wait()
            self.assertEqual(self.call(daemon.path, "busy"), CASES["busy"]["answer"])
            held.release.set()
            first.join()

    def test_a_stop_still_gets_a_reserved_slot_when_the_others_are_taken(self) -> None:
        held = Held({"code": "ok"})
        answers = {"run.list": held, "run.status": {"code": "ok"}, "run.stop": {"code": "stopped"}}
        with FakeDaemon(self.dir, answers, max_connections=1, priority=is_stop, reserved_connections=1) as daemon:
            first = threading.Thread(target=SocketAPI(daemon.path).call, args=("run.list", {}))
            first.start()
            self.addCleanup(first.join)
            self.addCleanup(held.release.set)
            held.entered.wait()
            self.assertEqual(SocketAPI(daemon.path).call("run.stop", {"run_id": "run-1"}), {"code": "stopped"})
            self.assertEqual(self.call(daemon.path, "busy"), CASES["busy"]["answer"])
            held.release.set()
            first.join()

    def test_not_admitted(self) -> None:
        with FakeDaemon(self.dir, {"run.list": {"code": "ok"}}, admit=lambda cred: False) as daemon:
            self.assertEqual(self.call(daemon.path, "not_admitted"), CASES["not_admitted"]["answer"])
            self.assertEqual(daemon.frames, [])

    def test_refused_by_the_handler(self) -> None:
        with FakeDaemon(self.dir, refuse="scope_refused") as daemon:
            self.assertEqual(self.call(daemon.path, "refused_by_handler"), CASES["refused_by_handler"]["answer"])
            self.assertEqual(len(daemon.frames), 1)

    def test_written_and_never_answered(self) -> None:
        with SilentDaemon(self.dir) as daemon:
            answer = self.call(daemon.path, "never_answered")
            daemon.done.wait()
        self.assertEqual(answer, CASES["never_answered"]["answer"])
        request = CASES["never_answered"]["request"]
        self.assertEqual(daemon.received, [{"op": "control", "method": request["method"], "params": request["params"]}])

    def test_every_case_is_checked(self) -> None:
        checked = {"answered", "stop_pending", "no_daemon", "busy", "not_admitted", "refused_by_handler", "never_answered"}
        self.assertEqual(set(CASES), checked)


class FakeDaemonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desktop-fake-")

    def test_it_records_every_frame_in_order_and_answers_ping_as_the_daemon_does(self) -> None:
        from recursion_core.control_transport import UnixControlClient

        with FakeDaemon(self.dir, {"run.list": {"code": "ok", "runs": []}}) as daemon:
            client = UnixControlClient(daemon.path)
            try:
                self.assertEqual(client.request({"op": "ping"}), {"ok": True, "op": "pong"})
                self.assertEqual(client.request({"op": "control", "method": "run.list", "params": {}}),
                                 {"ok": True, "result": {"code": "ok", "runs": []}})
                self.assertEqual(client.request({"op": "control", "method": "run.kill", "params": {}}),
                                 {"ok": True, "result": {"code": "method_unknown", "detail": "run.kill"}})
                self.assertEqual(client.request({"op": "nope"}), {"ok": False, "error": {"code": "unknown_op", "op": "nope"}})
            finally:
                client.close()
        self.assertEqual([f.get("op") for f in daemon.frames], ["ping", "control", "control", "nope"])
        self.assertEqual(daemon.requests(), [("run.list", {}), ("run.kill", {})])

    def test_a_control_frame_of_the_wrong_shape_is_refused_before_any_answer_is_made(self) -> None:
        from recursion_core.control_transport import UnixControlClient

        asked = []
        answers = {"run.list": lambda params: asked.append(params) or {"code": "ok", "runs": []}}
        invalid = {"ok": False, "error": {"code": "request_invalid"}}
        with FakeDaemon(self.dir, answers) as daemon:
            client = UnixControlClient(daemon.path)
            try:
                for frame in ({"op": "control", "method": 5, "params": {}},
                              {"op": "control", "params": {}},
                              {"op": "control", "method": "run.list", "params": []},
                              {"op": "control", "method": "run.list", "params": "all"}):
                    with self.subTest(frame):
                        self.assertEqual(client.request(frame), invalid)
                self.assertEqual(asked, [])
                # No params at all is the CLI's empty request, as the daemon reads it.
                self.assertEqual(client.request({"op": "control", "method": "run.list", "params": None}),
                                 {"ok": True, "result": {"code": "ok", "runs": []}})
            finally:
                client.close()
        self.assertEqual(asked, [{}])
        self.assertEqual(len(daemon.frames), 5)

    def test_it_refuses_a_directory_others_can_enter(self) -> None:
        open_dir = os.path.join(self.dir, "open")
        os.mkdir(open_dir, 0o755)
        os.chmod(open_dir, 0o755)
        with self.assertRaises(InsecureSocketPath):
            FakeDaemon(open_dir).__enter__()

    def test_the_silent_daemon_keeps_the_same_directory_rule(self) -> None:
        open_dir = os.path.join(self.dir, "open")
        os.mkdir(open_dir, 0o755)
        os.chmod(open_dir, 0o755)
        private = os.path.join(self.dir, "private")
        os.mkdir(private, 0o700)
        link = os.path.join(self.dir, "link")
        os.symlink(private, link)
        not_dir = os.path.join(self.dir, "file")
        Path(not_dir).write_text("", encoding="utf-8")
        for place in (open_dir, link, not_dir):
            with self.subTest(os.path.basename(place)), self.assertRaises(PermissionError):
                SilentDaemon(place)
        with SilentDaemon(private) as daemon:
            self.assertEqual(os.lstat(daemon.path).st_mode & 0o777, 0o600)

    def test_its_socket_is_owner_only(self) -> None:
        with FakeDaemon(self.dir) as daemon:
            mode = os.lstat(daemon.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
