"""Our client and our server talk to each other: the MCP client against the Control MCP server's ``serve``,
in the test's own process, over two pipes. The control API is a stand-in that records each call; nothing
reaches a daemon and no process is started."""

from __future__ import annotations

import os
import threading
import unittest

from recursion_core.control import mcp_tools
from recursion_core.control.mcp import server
from recursion_core.mcp.client import (
    CLIENT_SHUT,
    LATEST_PROTOCOL,
    LEGACY_PROTOCOL,
    McpClient,
    McpServerError,
)
from recursion_core.mcp.client.tests.support import HANG_GUARD_S


class RecordingAPI:
    def __init__(self) -> None:
        self.calls: list = []
        self._lock = threading.Lock()

    def call(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self.calls.append((method, params))
        return {"code": "ok", "runs": []}


class ClientAgainstControlServer(unittest.TestCase):
    def setUp(self) -> None:
        c2s_r, c2s_w = os.pipe()
        s2c_r, s2c_w = os.pipe()
        self.server_in, self.server_out = os.fdopen(c2s_r, "rb"), os.fdopen(s2c_w, "wb")
        self.client_in, self.client_out = os.fdopen(s2c_r, "rb"), os.fdopen(c2s_w, "wb")
        self.api = RecordingAPI()
        self.served = threading.Event()
        self.status: list = []

        def serve() -> None:
            try:
                self.status.append(server.serve(self.api, self.server_in, self.server_out))
            finally:
                self.served.set()

        threading.Thread(target=serve, daemon=True).start()
        self.records: list = []
        self.client = McpClient(self.client_in, self.client_out, on_record=self.records.append)
        self.addCleanup(self._shut)

    def _shut(self) -> None:
        self.client.close()  # the server reads end of input and returns
        self.served.wait(HANG_GUARD_S)
        for stream in (self.server_out, self.server_in, self.client_out, self.client_in):
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - already shut
                pass

    def test_the_session_continues_on_the_revision_the_server_answers(self):
        result = self.client.open()
        self.assertEqual(LEGACY_PROTOCOL, result["protocolVersion"])
        self.assertEqual(LEGACY_PROTOCOL, self.client.protocol_version)
        self.assertNotEqual(LATEST_PROTOCOL, self.client.protocol_version)
        self.assertEqual(server.SERVER_NAME, self.client.server_info["name"])

    def test_tools_are_listed_and_called(self):
        self.client.open()
        names = [tool["name"] for tool in self.client.list_tools()]
        self.assertEqual([entry["name"] for entry in mcp_tools.TOOLS], names)
        answer = self.client.call_tool("recursion_run_list")
        self.assertEqual({"code": "ok", "runs": []}, answer["structuredContent"])
        self.assertEqual("run.list", self.api.calls[0][0])
        self.assertEqual([], self.records)

    def test_the_servers_refusal_is_typed_and_the_session_goes_on(self):
        self.client.open()
        with self.assertRaises(McpServerError) as caught:
            self.client.call_tool("no_such_tool")
        self.assertEqual(-32602, caught.exception.error["code"])
        self.assertEqual(len(mcp_tools.TOOLS), len(self.client.list_tools()))
        self.assertEqual([], self.api.calls)

    def test_close_lets_the_server_end(self):
        self.client.open()
        self.client.close()
        self.assertTrue(self.served.wait(HANG_GUARD_S), "serve returned after end of input")
        self.assertEqual([0], self.status)
        self.assertEqual(CLIENT_SHUT, self.client.ending)


if __name__ == "__main__":
    unittest.main()
