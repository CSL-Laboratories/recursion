"""SocketAPI: a request the transport cannot frame is refused before anything is connected (finding 8)."""

from __future__ import annotations

import unittest

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

    def test_a_request_that_frames_is_sent_as_before(self) -> None:
        api, connect = self.api()
        self.assertEqual(api.call("run.list", {"limit": 5}), {"code": "ok", "runs": []})
        (path, timeout_s, client), = connect.calls
        self.assertEqual((path, timeout_s, client.closed), (self.PATH, 3.0, True))
        self.assertEqual(client.requests, [{"op": "control", "method": "run.list", "params": {"limit": 5}}])


if __name__ == "__main__":
    unittest.main()
