"""Control MCP descriptors (O41, Q48 as receipted into PRODUCT.md 055cc7a5) and the Codex snippet."""

from __future__ import annotations

import json
import tomllib
import unittest

from recursion_core.control import mcp_tools
from recursion_core.control.codex_config import codex_operator_snippet
from recursion_core.control.registry import EMPTY
from recursion_core.control.tests.fakes import FakeKillTree, make_api, started

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
EXPECTED = {
    "recursion_run_list": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "recursion_run_status": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    "recursion_run_stop": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    "recursion_run_start": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
}


class DescriptorTest(unittest.TestCase):
    def test_every_hint_on_every_tool(self) -> None:
        tools = {t["name"]: t for t in mcp_tools.descriptors()}
        self.assertEqual(set(tools), set(EXPECTED))
        for name, want in EXPECTED.items():
            ann = tools[name]["annotations"]
            for hint in HINTS:
                self.assertIn(hint, ann, f"{name} {hint} not set explicitly")
                self.assertIs(ann[hint], want[hint], f"{name} {hint}")
            self.assertIsInstance(ann["title"], str)
            json.dumps(tools[name])

    def test_stop_is_destructive_closed_world_start_is_open_world(self) -> None:
        tools = {t["name"]: t["annotations"] for t in mcp_tools.descriptors()}
        self.assertIs(tools["recursion_run_stop"]["destructiveHint"], True)  # 055cc7a5 O41 wins over older F60 wording
        self.assertIs(tools["recursion_run_stop"]["openWorldHint"], False)
        self.assertIs(tools["recursion_run_start"]["openWorldHint"], True)

    def test_tools_forward_to_the_control_api(self) -> None:
        api, _, kill, _ = make_api(FakeKillTree([EMPTY]))
        rid = started(api)
        res = mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": rid})
        self.assertEqual((res["structuredContent"]["code"], res["isError"]), ("stopped", False))
        self.assertEqual(json.loads(res["content"][0]["text"]), res["structuredContent"])
        self.assertEqual(len(kill.requests), 1)
        self.assertTrue(mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": "nope"})["isError"])
        self.assertTrue(mcp_tools.call_tool(api, "recursion_run_kill", {})["isError"])
        self.assertFalse(mcp_tools.call_tool(api, "recursion_run_list", {})["isError"])


class CodexSnippetTest(unittest.TestCase):
    def test_stop_gets_per_tool_approve_and_start_does_not(self) -> None:
        text = codex_operator_snippet("recursion-control", "recursion-control-mcp", ["--stdio"])
        doc = tomllib.loads(text)
        server = doc["mcp_servers"]["recursion-control"]
        self.assertEqual((server["command"], server["args"]), ("recursion-control-mcp", ["--stdio"]))
        self.assertEqual(server["tools"], {"recursion_run_stop": {"approval_mode": "approve"}})
        self.assertEqual(text.count("approval_mode"), 2)  # one setting, one mention in the comment
        self.assertIn('[mcp_servers.recursion-control.tools.recursion_run_stop]\napproval_mode = "approve"', text)

    def test_snippet_documents_the_exceptions(self) -> None:
        text = codex_operator_snippet("rc", "x")
        for phrase in ("strict_auto_review", "managed approval requirement", "prompts in Auto mode",
                       "refused under", "codex exec"):
            self.assertIn(phrase, text)

    def test_rejects_non_bare_server_id(self) -> None:
        with self.assertRaises(ValueError):
            codex_operator_snippet("a.b", "x")

    def test_args_are_not_built_by_iterating_a_string_or_a_mapping(self) -> None:
        # A string is a sequence of characters; a mapping iterates its keys. Either
        # would write an argv that is not the launch the caller named.
        with self.assertRaises(ValueError):
            codex_operator_snippet("rc", "recursion-control-mcp", "--stdio")
        with self.assertRaises(ValueError):
            codex_operator_snippet("rc", "recursion-control-mcp", {"--stdio": "1"})
        with self.assertRaises(ValueError):
            codex_operator_snippet("rc", "recursion-control-mcp", ["--stdio", 1])
        with self.assertRaises(ValueError):
            codex_operator_snippet("rc", None, ["--stdio"])


if __name__ == "__main__":
    unittest.main()
