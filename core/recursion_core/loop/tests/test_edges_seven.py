"""What the round-7 DeepSeek read of draft #379 found (review-7-379; the two Codex reads of the round stopped on
their usage limit), kept as tests: a user message whose text is not valid Unicode is refused and never joins the
conversation; a thing passed to dispatch that is not a tool call cannot raise out of it; the input checked is the
input run; the approval rule fails closed where it is read; the specs the registry hands out are copies."""

from __future__ import annotations

import copy
import json
import unittest
from collections.abc import Mapping
from dataclasses import dataclass

from recursion_core.loop import protocol as p
from recursion_core.loop.engine import Engine
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.test_edges import _message, _session
from recursion_core.loop.tools import registry as r

LONE_ESCAPE = bytes([92]) + b"ud800"  # JSON's escape for a lone surrogate
SCHEMA = {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"],
          "additionalProperties": False}


def _surrogate_message(n: int) -> bytes:
    return (b'{"kind": "user_message", "turn_id": "turn-%d", "delivery_id": "dlv-%d", "text": "before '
            % (n, n)) + LONE_ESCAPE + b'"}'


def _tool(needs_approval: bool = False, cls=r.Tool):
    ran: list = []
    tool = cls(b.ToolSpec("t", "an inert test tool", copy.deepcopy(SCHEMA)),
               lambda args, ctx: ran.append(dict(args)) or r.ToolOutput("ran"), needs_approval)
    return tool, ran


def _ctx(case):
    session, provider, lines = _session(case, [])
    return r.ToolContext(session.workspace, b.CancelToken(), lambda s: s)


class UserTextThatIsNotUnicode(unittest.TestCase):
    """DeepSeek, round 7, finding 1: a user_message holding a lone surrogate was taken, failed its turn with a
    loop error, and stayed in the conversation, so every later turn failed the same way."""

    def test_parse_input_refuses_it(self) -> None:
        with self.assertRaises(p.InputError) as caught:
            p.parse_input(_surrogate_message(1))
        self.assertEqual(caught.exception.code, "bad_text")

    def test_the_line_is_refused_and_the_next_message_runs(self) -> None:
        session, provider, lines = _session(self, [{"text": "fine"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        engine.feed_line(_surrogate_message(1))
        engine.feed_line(_message(2))
        engine.input_ended()
        self.assertEqual(engine.serve(), 0)
        self.assertEqual(lines.of("protocol_error")[0]["code"], "bad_text")
        self.assertEqual([(x["turn_id"], x["outcome"]) for x in lines.of("turn_ended")],
                         [("turn-2", "candidate_done")])
        lines.raw().encode("utf-8")

    def test_an_in_process_caller_cannot_put_it_in_the_conversation(self) -> None:
        session, provider, lines = _session(self, [{"text": "fine"}])
        with self.assertRaises(ValueError):
            session.run_turn("turn-1", "before " + chr(0xD800), b.CancelToken())
        self.assertEqual(session.messages, [])
        self.assertEqual(session.run_turn("turn-2", "a clean message", b.CancelToken()), "candidate_done")


class DispatchGuard(unittest.TestCase):
    """DeepSeek, round 7, finding 2: something that is not a tool call, whose call_id or name raises, still gets
    an error result."""

    def test_a_raising_property_does_not_escape(self) -> None:
        class Raising:
            @property
            def call_id(self):
                raise RuntimeError("synthetic")

            @property
            def name(self):
                raise RuntimeError("synthetic")

        tool, ran = _tool()
        outcome = r.ToolRegistry([tool]).dispatch(Raising(), _ctx(self))
        self.assertEqual((outcome.status, outcome.call_id, outcome.name, ran), (r.ERROR, "", "", []))


class CheckedIsRun(unittest.TestCase):
    """DeepSeek, round 7, finding 3: the input is copied once, so what the schema check saw is what the tool
    gets, even from a Mapping that answers differently each time it is read."""

    def test_a_mapping_that_changes_between_reads_runs_as_checked(self) -> None:
        class Twice(Mapping):
            def __init__(self) -> None:
                self.reads = 0

            def __iter__(self):
                self.reads += 1
                return iter(["p"] if self.reads == 1 else ["p", "evil"])

            def __len__(self) -> int:
                return 1 if self.reads <= 1 else 2

            def __getitem__(self, key):
                return "x"

        tool, ran = _tool()
        outcome = r.ToolRegistry([tool]).dispatch(b.ToolCall("toolu_1", "t", Twice()), _ctx(self))
        self.assertEqual(outcome.status, r.OK)
        self.assertEqual(ran, [{"p": "x"}])


class ApprovalFailsClosed(unittest.TestCase):
    """DeepSeek, round 7, finding 4: a subclass that skips the flag's check cannot make 0 or None mean "runs
    without approval": the rule reads anything but False as needing approval."""

    def test_a_subclass_that_skips_the_check_is_still_refused(self) -> None:
        @dataclass(frozen=True)
        class NoCheck(r.Tool):
            def __post_init__(self) -> None:
                pass

        for flag in (0, None, "", []):
            with self.subTest(flag=repr(flag)):
                tool, ran = _tool(flag, NoCheck)
                outcome = r.ToolRegistry([tool]).dispatch(b.ToolCall("toolu_1", "t", {"p": "x"}), _ctx(self))
                self.assertEqual((outcome.status, ran), (r.DENIED, []))
        tool, ran = _tool(False, NoCheck)
        self.assertEqual(r.ToolRegistry([tool]).dispatch(b.ToolCall("toolu_1", "t", {"p": "x"}), _ctx(self)).status,
                         r.OK)


class SpecsAreCopies(unittest.TestCase):
    """DeepSeek, round 7, finding 5: the spec get() and specs() hand out is a copy; changing it changes neither
    what the model is sent nor what dispatch checks."""

    def test_changing_a_handed_out_schema_changes_nothing(self) -> None:
        tool, ran = _tool()
        registry = r.ToolRegistry([tool])
        for spec in (registry.get("t"), registry.specs()[0]):
            spec.input_schema.pop("additionalProperties")
            spec.input_schema["required"].clear()
            spec.input_schema["properties"]["p"]["type"] = "integer"
        self.assertEqual(json.dumps(registry.specs()[0].input_schema, sort_keys=True),
                         json.dumps(SCHEMA, sort_keys=True))
        outcome = registry.dispatch(b.ToolCall("toolu_1", "t", {"p": "x", "extra": 1}), _ctx(self))
        self.assertEqual((outcome.status, ran), (r.ERROR, []))


if __name__ == "__main__":
    unittest.main()
