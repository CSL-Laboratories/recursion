"""Control MCP descriptors (O41, Q48 as receipted into PRODUCT.md 055cc7a5) and the Codex snippet."""

from __future__ import annotations

import json
import tomllib
import unittest

from recursion_core.control import codex_config, mcp_tools
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


    def test_rejects_trailing_newline_server_id_and_encodes_non_bmp_paths_as_valid_toml(self) -> None:
        with self.assertRaises(ValueError):
            codex_operator_snippet("rc\n", "x")
        text = codex_operator_snippet("rc", "/opt/\U0001f680/mcp", ["--label=\U0001f680", "line\nbreak"])
        doc = tomllib.loads(text)
        self.assertEqual(doc["mcp_servers"]["rc"]["command"], "/opt/\U0001f680/mcp")
        self.assertEqual(doc["mcp_servers"]["rc"]["args"], ["--label=\U0001f680", "line\nbreak"])

    def test_every_value_round_trips_through_toml_and_the_text_is_ascii(self) -> None:
        values = ["plain", "with \"quotes\" and \\ backslash", "tab\tcr\rff\fbs\b", "del\x7fnul\x00us\x1f",
                  "caf\u00e9 \u2028 \uffff", "\U0001f680\U0010ffff", ""]
        text = codex_operator_snippet("rc", values[0], values)
        self.assertTrue(text.isascii())
        doc = tomllib.loads(text)
        self.assertEqual(doc["mcp_servers"]["rc"]["args"], values)
        self.assertIn("\\U0001f680", text)  # a scalar escape, not JSON's surrogate pair
        self.assertNotIn("\\ud83d", text)

    def test_below_u_ffff_the_text_is_what_json_wrote_except_del(self) -> None:
        sample = "a\"b\\c\nd\te\u00e9\u0001\uffff/"
        self.assertEqual(codex_config.toml_string(sample), json.dumps(sample))
        self.assertEqual(codex_config.toml_string("\x7f"), '"\\u007f"')

    def test_a_lone_surrogate_or_a_non_string_has_no_toml_form(self) -> None:
        with self.assertRaises(ValueError):
            codex_operator_snippet("rc", "/opt/\udc80/mcp")
        with self.assertRaises(ValueError):
            codex_operator_snippet("rc", "x", ["\ud83d"])
        for bad in (5, None, b"x", ["x"]):   # a list of strings would otherwise be written as one string
            with self.assertRaises(TypeError):
                codex_operator_snippet("rc", bad)  # type: ignore[arg-type]
        for bad in (5, None, b"rc", "", "rc\n", "\nrc", "r c"):
            with self.assertRaises(ValueError):
                codex_operator_snippet(bad, "x")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
