"""Control MCP descriptors (O41, Q48 as receipted into PRODUCT.md 055cc7a5) and the Codex snippet."""

from __future__ import annotations

import json
import os
import tomllib
import unittest

from recursion_core.control import mcp_tools
from recursion_core.control.codex_config import codex_operator_snippet
from recursion_core.control.registry import EMPTY, POPULATED
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


class IsErrorTest(unittest.TestCase):
    """The Liaison's ruling on lane MCP's Q2 (Nov 15, 4:22 PM; DeepSeek F4 of #416): a Stop that truthfully answers
    pending or ended, and an interrupt that answers pending, read isError true. One mapping, per method, a test per
    stop and interrupt tool."""

    class Answering:
        """A control API that answers every call with one code."""

        def __init__(self, code):
            self.code, self.calls = code, []

        def call(self, method, params):
            self.calls.append(method)
            return {"code": self.code, "run_id": params.get("run_id")}

    def test_the_run_stop_tool_reads_pending_and_ended_as_answers_not_errors(self) -> None:
        for code, error in (("stopped", False), ("pending", False), ("ended", False), ("run_unknown", True),
                            ("request_invalid", True), ("registry_unavailable", True), ("unavailable", True),
                            ("scope_refused", True)):
            with self.subTest(code=code):
                api = self.Answering(code)
                res = mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": "r1"})
                self.assertEqual((res["structuredContent"]["code"], res["isError"]), (code, error))
                self.assertEqual(api.calls, ["run.stop"])

    def test_a_pending_stop_through_the_real_api_is_not_an_error(self) -> None:
        api, _, _, _ = make_api(FakeKillTree([POPULATED] * 20))
        rid = started(api)
        res = mcp_tools.call_tool(api, "recursion_run_stop", {"run_id": rid})
        self.assertEqual((res["structuredContent"]["code"], res["isError"]), ("pending", False))

    def test_the_session_stop_and_interrupt_tools(self) -> None:
        # Lane MCP's tools recursion_session_stop and recursion_session_interrupt answer through is_error.
        cases = {"session.stop": (("stopped", False), ("pending", False), ("ended", False), ("ok", True),
                                  ("session_unknown", True), ("conflict", True), ("unsupported", True)),
                 "session.interrupt": (("ok", False), ("pending", False), ("ended", True), ("stopped", True),
                                       ("session_unknown", True), ("conflict", True), ("unsupported", True))}
        for method, answers in cases.items():
            for code, error in answers:
                with self.subTest(method=method, code=code):
                    self.assertIs(mcp_tools.is_error(method, {"code": code}), error)

    def test_every_other_method_keeps_the_api_success_codes(self) -> None:
        for method in ("run.start", "run.status", "run.list", "session.turn", "approval.answer"):
            for code in ("started", "ok", "stopped"):
                self.assertIs(mcp_tools.is_error(method, {"code": code}), False, (method, code))
            for code in ("pending", "ended", "run_unknown", None):
                self.assertIs(mcp_tools.is_error(method, {"code": code}), True, (method, code))
        for answer in (None, [], "pending", {"detail": "no code"}):
            for method in ("run.stop", "run.status", "session.interrupt"):
                self.assertIs(mcp_tools.is_error(method, answer), True, (method, repr(answer)))

    def test_the_mapping_names_only_codes_the_published_table_gives_each_method(self) -> None:
        # Skipped where the published table is not in the tree (it arrives with SPINE's #368).
        path = os.path.join(os.path.dirname(mcp_tools.__file__), *[os.pardir] * 3, "schemas", "control", "v1",
                            "methods.table.v1.json")
        if not os.path.exists(path):
            self.skipTest("schemas/control/v1/methods.table.v1.json is not in this checkout yet")
        with open(path, encoding="utf-8") as handle:
            rows = {row["name"]: row for row in json.load(handle)["methods"]}
        for method, codes in mcp_tools.NOT_ERRORS.items():
            self.assertLessEqual(codes, set(rows[method]["codes"]), method)


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


if __name__ == "__main__":
    unittest.main()
