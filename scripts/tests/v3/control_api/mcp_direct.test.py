#!/usr/bin/env python3
"""Control MCP face and the labeled direct-CLI fallback. Fake driver only."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.v3.control_api import mcp_server  # noqa: E402
from scripts.v3.control_api.auth import SCOPE_CONTROL, SCOPE_OBSERVE  # noqa: E402
from scripts.v3.control_api.direct_cli import run_direct  # noqa: E402
from scripts.v3.control_api.fake_engines import build_fake_stack  # noqa: E402


def _server(scopes: set[str]) -> mcp_server.ControlMcpServer:
    stack = build_fake_stack()
    token = stack.issue("agent", "tenant-mcp", scopes)

    def dispatch(method: str, params: dict) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        return stack.service.handle(body, f"Bearer {token}")

    return mcp_server.ControlMcpServer(dispatch)


def _call(server: mcp_server.ControlMcpServer, name: str, arguments: dict) -> dict:
    response = server.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    assert response is not None
    return response["result"]


class McpTests(unittest.TestCase):
    def test_initialize_and_tools_list_cover_every_method(self) -> None:
        server = _server({SCOPE_CONTROL, SCOPE_OBSERVE})
        init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], mcp_server.SERVER_NAME)
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(names, set(mcp_server.TOOLS))
        methods = {method for method, _, _ in mcp_server.TOOLS.values()}
        self.assertEqual(
            methods,
            {"service.info", "route.list", "session.open", "session.get", "session.close", "session.run", "events.poll"},
        )

    def test_notification_gets_no_response(self) -> None:
        server = _server({SCOPE_OBSERVE})
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_both_engines_run_through_mcp(self) -> None:
        server = _server({SCOPE_CONTROL, SCOPE_OBSERVE})
        for engine in ("claude", "codex"):
            self.assertFalse(_call(server, "recursion_session_open", {"engine": engine, "session": "s1"})["isError"])
            run = _call(server, "recursion_session_run", {"engine": engine, "session": "s1", "prompt": "review"})
            self.assertFalse(run["isError"])
            payload = json.loads(run["content"][0]["text"])
            self.assertEqual(payload["result"]["status"], "ok")
            self.assertEqual(payload["request"]["workload_id"], "control-api")

    def test_scope_is_enforced_by_the_control_api(self) -> None:
        server = _server({SCOPE_OBSERVE})
        result = _call(server, "recursion_session_open", {"engine": "codex", "session": "s1"})
        self.assertTrue(result["isError"])
        self.assertTrue(_call(server, "recursion_service_info", {}) ["isError"] is False)

    def test_bad_requests_are_protocol_errors(self) -> None:
        server = _server({SCOPE_OBSERVE})
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"], -32601)
        bad = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}})
        self.assertEqual(bad["error"]["code"], -32602)
        self.assertEqual(server.handle([])["error"]["code"], -32600)

    def test_stdio_loop_survives_garbage(self) -> None:
        server = _server({SCOPE_OBSERVE})
        sink = io.StringIO()
        server.serve(io.StringIO('not json\n\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), sink)
        lines = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["result"], {})


class DirectCliTests(unittest.TestCase):
    def test_both_engines_labeled_not_x2(self) -> None:
        for engine in ("claude", "codex"):
            out = run_direct(engine, "review this")
            self.assertEqual(out["path"], "direct-cli-fallback")
            self.assertIs(out["satisfies_x2"], False)
            self.assertIs(out["live_calls"], False)
            self.assertIn("Not X2", out["notice"])
            self.assertEqual(out["result"]["status"], "ok")

    def test_neutral_workload_ids(self) -> None:
        request = run_direct("codex", "x")["request"]
        self.assertEqual(request["workload_id"], "direct-cli")
        self.assertEqual(request["process_requirement_id"], "proc-req-direct-cli")

    def test_unknown_engine_refused(self) -> None:
        with self.assertRaises(ValueError):
            run_direct("gemini", "x")

    def test_failure_scripts_surface_without_a_provider(self) -> None:
        out = run_direct("codex", "x", script=("limit",))
        self.assertNotEqual(out["result"]["status"], "ok")
        self.assertIs(out["satisfies_x2"], False)


if __name__ == "__main__":
    unittest.main()
