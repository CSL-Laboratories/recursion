"""The registry: input checks before a tool runs, and the one output cap with its spill."""

from __future__ import annotations

import os
import stat
import unittest

from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import FAKE_KEY, POSIX, context, workspace
from recursion_core.loop.tools import registry as r
from recursion_core.loop.tools.workspace import ToolError

SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}, "s": {"type": "string", "minLength": 1},
                   "flag": {"type": "boolean"}, "mode": {"type": "string", "enum": ["a", "b"]},
                   "items": {"type": "array", "items": {"type": "string"}}},
    "required": ["n"],
    "additionalProperties": False,
}


def tool(name: str, fn, needs_approval: bool = False) -> r.Tool:
    return r.Tool(b.ToolSpec(name, f"{name} tool", SCHEMA), fn, needs_approval)


def call(name: str, args: dict | None, raw: str = "") -> b.ToolCall:
    return b.ToolCall("toolu_1", name, args, raw, None if args is not None else "not JSON")


class Validation(unittest.TestCase):
    def test_bad_inputs_are_refused_and_the_tool_does_not_run(self) -> None:
        ran: list[dict] = []
        reg = r.ToolRegistry([tool("t", lambda a, c: ran.append(a) or r.ToolOutput("ran"))])
        ctx = context(workspace(self))
        cases = [({}, "n is required"), ({"n": "1"}, "must be an integer"), ({"n": True}, "must be an integer"),
                 ({"n": 1.5}, "must be an integer"), ({"n": 0}, "at least 1"), ({"n": 9}, "at most 5"),
                 ({"n": 1, "x": 1}, "unknown field"), ({"n": 1, "s": ""}, "too short"),
                 ({"n": 1, "flag": 1}, "true or false"), ({"n": 1, "mode": "c"}, "one of"),
                 ({"n": 1, "items": ["a", 2]}, "items[1] must be a string")]
        for args, words in cases:
            with self.subTest(args=args):
                out = reg.dispatch(call("t", args), ctx)
                self.assertEqual(out.status, r.ERROR)
                self.assertIn(words, out.model_text)
        self.assertEqual(ran, [])
        self.assertEqual(reg.dispatch(call("t", {"n": 2, "mode": "a"}), ctx).model_text, "ran")

    def test_input_that_was_not_json_comes_back_raw_and_nothing_runs(self) -> None:
        ran: list = []
        reg = r.ToolRegistry([tool("t", lambda a, c: ran.append(a) or r.ToolOutput("ran"))])
        out = reg.dispatch(call("t", None, raw='{"n": "1'), context(workspace(self)))
        self.assertEqual(out.status, r.ERROR)
        self.assertEqual(out.model_text, '{"INVALID_JSON": "{\\"n\\": \\"1"}')
        self.assertEqual(ran, [])

    def test_unknown_tool_lists_the_tools(self) -> None:
        empty = lambda a, c: r.ToolOutput("")  # noqa: E731
        reg = r.ToolRegistry([tool("alpha", empty), tool("beta", empty)])
        out = reg.dispatch(call("gamma", {"n": 1}), context(workspace(self)))
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("alpha, beta", out.model_text)

    def test_a_tool_that_needs_approval_is_refused_until_approvals_exist(self) -> None:
        ran: list = []
        reg = r.ToolRegistry([tool("t", lambda a, c: ran.append(a) or r.ToolOutput("ran"), needs_approval=True)])
        out = reg.dispatch(call("t", {"n": 1}), context(workspace(self)))
        self.assertEqual(out.status, r.DENIED)
        self.assertEqual(ran, [])

    def test_a_failing_tool_is_an_error_result(self) -> None:
        def boom(a, c):
            raise RuntimeError("disk on fire")

        def refuse(a, c):
            raise ToolError("not here")

        reg = r.ToolRegistry([tool("boom", boom), tool("refuse", refuse)])
        ctx = context(workspace(self))
        self.assertEqual(reg.dispatch(call("boom", {"n": 1}), ctx).model_text,
                         "the tool failed: RuntimeError: disk on fire")
        self.assertEqual(reg.dispatch(call("refuse", {"n": 1}), ctx).model_text, "not here")

    def test_an_interrupted_turn_runs_no_more_tools(self) -> None:
        ran: list = []
        reg = r.ToolRegistry([tool("t", lambda a, c: ran.append(a) or r.ToolOutput("ran"))])
        cancel = b.CancelToken()
        cancel.cancel()
        out = reg.dispatch(call("t", {"n": 1}), context(workspace(self), cancel=cancel))
        self.assertEqual(out.status, r.CANCELLED)
        self.assertEqual(ran, [])

    def test_registry_refuses_duplicate_and_odd_names(self) -> None:
        fn = lambda a, c: r.ToolOutput("")  # noqa: E731
        with self.assertRaises(ValueError):
            r.ToolRegistry([tool("t", fn), tool("t", fn)])
        with self.assertRaises(ValueError):
            r.ToolRegistry([tool("has space", fn)])
        with self.assertRaises(ValueError):
            r.ToolRegistry([tool("t", fn)], max_bytes=100)


class Cap(unittest.TestCase):
    def emit(self, text: str, **kw) -> tuple[r.Outcome, object]:
        ws = workspace(self)
        reg = r.ToolRegistry([tool("t", lambda a, c: r.ToolOutput(text))], **kw)
        return reg.dispatch(call("t", {"n": 1}), context(ws, scrub=lambda s: s.replace(FAKE_KEY, "[redacted]"))), ws

    def test_an_output_within_the_cap_is_untouched(self) -> None:
        text = "x" * (r.DEFAULT_MAX_BYTES - 1) + "\n"
        out, ws = self.emit(text)
        self.assertFalse(out.truncated)
        self.assertEqual(out.model_text, text)
        self.assertIsNone(out.spill_ref)
        self.assertFalse((ws.spill.root).exists(), "nothing is spilled when nothing is cut")

    def test_an_oversized_output_is_capped_and_spilled_whole(self) -> None:
        lines = [f"line {i:05d} " + "y" * 60 + "\n" for i in range(5000)]
        text = "".join(lines)
        out, ws = self.emit(text)
        self.assertTrue(out.truncated)
        self.assertLessEqual(len(out.model_text.encode("utf-8")), r.DEFAULT_MAX_BYTES)
        self.assertEqual(out.model_text.count("[output truncated:"), 1, "one marker, never two")
        self.assertTrue(out.model_text.startswith(lines[0]))
        self.assertTrue(out.model_text.endswith(lines[-1]))
        self.assertRegex(out.spill_ref, r"^spill:[0-9a-f]{64}$")
        self.assertIn(out.spill_ref, out.model_text)
        self.assertEqual(out.output_bytes, len(text.encode()))
        saved = ws.spill.path_for(out.spill_ref)
        self.assertEqual(saved.read_text(encoding="utf-8"), text)
        if POSIX:
            self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(ws.spill.root).st_mode), 0o700)

    def test_the_line_cap_holds_for_short_lines(self) -> None:
        text = "".join(f"{i}\n" for i in range(3000))
        out, _ = self.emit(text)
        self.assertTrue(out.truncated)
        self.assertLessEqual(out.model_text.count("\n"), r.DEFAULT_MAX_LINES)
        self.assertIn("of 3000", out.model_text)

    def test_one_huge_line_shows_its_start_and_end(self) -> None:
        text = "\u2713" * 40000  # 120,000 bytes, no newline, three bytes a character
        out, _ = self.emit(text)
        self.assertTrue(out.truncated)
        self.assertLessEqual(len(out.model_text.encode("utf-8")), r.DEFAULT_MAX_BYTES)
        self.assertTrue(out.model_text.startswith("\u2713\u2713"))
        self.assertTrue(out.model_text.endswith("\u2713\u2713"))
        self.assertIn("lines are cut", out.model_text)
        self.assertEqual(out.model_text.count("[output truncated:"), 1)

    def test_the_same_output_is_stored_once(self) -> None:
        text = "z\n" * 40000
        ws = workspace(self)
        reg = r.ToolRegistry([tool("t", lambda a, c: r.ToolOutput(text))])
        ctx = context(ws)
        first = reg.dispatch(call("t", {"n": 1}), ctx)
        second = reg.dispatch(call("t", {"n": 1}), ctx)
        self.assertEqual(first.spill_ref, second.spill_ref)
        files = [f for _, _, fs in os.walk(ws.spill.root) for f in fs]
        self.assertEqual(len(files), 1, files)

    def test_a_spill_that_cannot_be_written_is_said_so(self) -> None:
        ws = workspace(self)
        ws.spill.root.write_text("in the way", encoding="utf-8")  # a file where the folder should be
        reg = r.ToolRegistry([tool("t", lambda a, c: r.ToolOutput("w\n" * 40000))])
        out = reg.dispatch(call("t", {"n": 1}), context(ws))
        self.assertTrue(out.truncated)
        self.assertIsNone(out.spill_ref)
        self.assertIn("could not be saved", out.model_text)

    def test_secrets_are_removed_before_the_model_or_the_spill_sees_them(self) -> None:
        text = f"token={FAKE_KEY}\n" + "filler\n" * 20000 + f"again {FAKE_KEY}\n"
        out, ws = self.emit(text)
        self.assertNotIn(FAKE_KEY, out.model_text)
        self.assertIn("[redacted]", out.model_text)
        self.assertNotIn(FAKE_KEY, ws.spill.path_for(out.spill_ref).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
