"""SocketAPI: a request the transport cannot frame is refused before anything is connected (finding 8), and
Windows is refused before any path or connector is looked at (finding 13, D9)."""

from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from recursion_core.control import client as client_mod
from recursion_core.control.client import SocketAPI
from recursion_core.control_transport.framing import MAX_FRAME_BYTES


class _Client:
    def __init__(self, reply: dict) -> None:
        self.reply, self.requests, self.closed = reply, [], False

    def request(self, message: dict) -> dict:
        self.requests.append(message)
        return self.reply

    def close(self) -> None:
        self.closed = True


class _Connect:
    """Stands in for UnixControlClient: records every connection, answers one canned reply."""

    def __init__(self, reply: dict) -> None:
        self.reply, self.calls = reply, []

    def __call__(self, path: str, *, timeout_s: float) -> _Client:
        client = _Client(self.reply)
        self.calls.append((path, timeout_s, client))
        return client


class NotSentTest(unittest.TestCase):
    PATH = "/nonexistent/recursion/control.sock"

    def setUp(self) -> None:
        # The client as on a POSIX host, on every host: on native Windows SocketAPI refuses before framing
        # (finding 13, D9), which the Windows test below checks by setting this stand-in's name to "nt".
        # (The read of #385 by Codex GPT-6.1 Sol, R6: these tests failed on native Windows 3.12.)
        patcher = mock.patch.object(client_mod, "os", types.SimpleNamespace(name="posix", getuid=lambda: 1000,
                                                                            environ={}, getcwd=os.getcwd))
        patcher.start()
        self.addCleanup(patcher.stop)

    def api(self) -> tuple[SocketAPI, _Connect]:
        connect = _Connect({"ok": True, "result": {"code": "ok", "runs": []}})
        return SocketAPI(self.PATH, timeout_s=3.0, connect=connect), connect

    def test_a_request_over_the_frame_cap_is_not_delivered_and_connects_nothing(self) -> None:
        api, connect = self.api()
        reply = api.call("run.start", {"tenant": "t", "engine": "e", "prompt": "x" * MAX_FRAME_BYTES})
        self.assertEqual(reply, {"code": "control_api_unavailable", "delivered": False,
                                 "detail": "not_sent:frame_too_large"})
        self.assertEqual(connect.calls, [])

    def test_params_that_are_not_json_are_not_delivered_and_connect_nothing(self) -> None:
        api, connect = self.api()
        for params in ({"x": float("nan")}, {"x": float("inf")}, {"x": object()}, {"x": {1, 2}}):
            with self.subTest(params=repr(params)):
                reply = api.call("run.list", params)
                self.assertEqual(reply, {"code": "control_api_unavailable", "delivered": False,
                                         "detail": "not_sent:frame_not_json"})
        self.assertEqual(connect.calls, [])

    def test_windows_refuses_before_any_path_frame_or_connector_is_looked_at(self) -> None:
        # Finding 13 under D9: an explicit POSIX path on Windows reached the connector and answered
        # connect:InsecureSocketPath. R2's own regression is in control/tests/test_tui_entry.py, lane TUI's file.
        from recursion_core.control_transport import InsecureSocketPath

        def windows_connect(path, *, timeout_s):
            raise InsecureSocketPath("socket path must be absolute")   # what ntpath makes of "/run/..."

        refusal = {"code": "control_api_unavailable", "delivered": False, "detail": "platform_unsupported"}
        api = SocketAPI("/run/user/1000/recursion/control.sock", connect=windows_connect)
        with mock.patch.object(client_mod.os, "name", "nt"):
            self.assertEqual(api.call("run.stop", {"run_id": "r1"}), refusal)
            self.assertEqual(api.call("run.start", {"prompt": "x" * MAX_FRAME_BYTES}), refusal)
        no_uids = types.SimpleNamespace(name="posix", environ={}, getcwd=os.getcwd)
        recorded = _Connect({"ok": True, "result": {"code": "ok"}})
        with mock.patch.object(client_mod, "os", no_uids):
            self.assertEqual(SocketAPI(self.PATH, connect=recorded).call("run.list", {}), refusal)
        self.assertEqual(recorded.calls, [])

    def test_params_that_are_not_an_object_are_refused_typed_and_connect_nothing(self) -> None:
        # The reads of #377 and #385 by DeepSeek V4.1 Flash (F9, F6): these raised TypeError or ValueError,
        # or (a list of pairs, a list of two-letter strings) were framed as an object nobody sent.
        api, connect = self.api()
        for params in (5, [1, 2], [("a", 1)], ["ab"], "ab", b"ab", [], ()):
            with self.subTest(params=repr(params)):
                self.assertEqual(api.call("run.list", params), {"code": "control_api_unavailable",
                                                                "delivered": False,
                                                                "detail": "not_sent:params_not_object"})
        self.assertEqual(connect.calls, [])
        self.assertEqual(api.call("run.list", None), {"code": "ok", "runs": []})
        self.assertEqual(connect.calls[0][2].requests[0]["params"], {})

    def test_a_request_that_frames_is_sent_as_before(self) -> None:
        api, connect = self.api()
        self.assertEqual(api.call("run.list", {"limit": 5}), {"code": "ok", "runs": []})
        (path, timeout_s, client), = connect.calls
        self.assertEqual((path, timeout_s, client.closed), (self.PATH, 3.0, True))
        self.assertEqual(client.requests, [{"op": "control", "method": "run.list", "params": {"limit": 5}}])


if __name__ == "__main__":
    unittest.main()
