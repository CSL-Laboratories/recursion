"""The loop with the fake provider, in process: turns, tools, interrupts, limits and how each turn ends."""

from __future__ import annotations

import json
import threading
import unittest

from recursion_core.loop.engine import Config, Engine, Session
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import Lines, registry, workspace
from recursion_core.loop.tools import read, search
from recursion_core.loop.tools import registry as r


def msg(turn: str, text: str, delivery: str) -> bytes:
    return json.dumps({"kind": "user_message", "turn_id": turn, "text": text, "delivery_id": delivery}).encode()


def interrupt(turn: str) -> bytes:
    return json.dumps({"kind": "interrupt", "turn_id": turn}).encode()


CLOSE = b'{"kind":"close"}'


class Harness:
    def __init__(self, case, script, files=None, tools=None, max_steps: int = 50) -> None:
        self.ws = workspace(case, files or {"src/a.py": "print(1)\n"})
        self.provider = FakeProvider(script)
        self.lines = Lines()
        reg = r.ToolRegistry(tools) if tools is not None else registry()
        self.session = Session(self.provider, reg, self.ws, self.lines.writer, self.lines.redactor,
                               Config(model="fake-model", max_steps=max_steps))
        self.engine = Engine(self.session, self.lines.writer, log=lambda m: None)
        self.thread = threading.Thread(target=self.engine.serve)
        self.thread.start()
        case.addCleanup(self.stop)

    def feed(self, *lines: bytes) -> None:
        for line in lines:
            self.engine.feed_line(line)

    def stop(self) -> None:
        if self.thread.is_alive():
            self.engine.input_ended()
            self.thread.join()

    def finish(self) -> list[dict]:
        self.feed(CLOSE)
        self.thread.join()
        return self.lines.all()


def kinds(docs: list[dict]) -> list[str]:
    return [d["type"] for d in docs]


class Turns(unittest.TestCase):
    def test_a_read_then_an_answer(self) -> None:
        h = Harness(self, [
            {"text": "Reading.", "tool_calls": [{"id": "toolu_1", "name": "read", "input": {"path": "src/a.py"}}],
             "usage": {"input_tokens": 100, "output_tokens": 20}},
            {"text": "It prints 1.", "usage": {"input_tokens": 140, "output_tokens": 5, "cache_read_tokens": 90}},
        ])
        h.feed(msg("turn-1", "What does src/a.py do?", "dlv-1"))
        docs = h.finish()
        self.assertEqual(kinds(docs), ["input_accepted", "usage", "text", "tool_call", "tool_result", "usage", "text",
                                       "turn_ended", "closed"])
        self.assertEqual(docs[0], {"type": "input_accepted", "delivery_id": "dlv-1", "turn_id": "turn-1",
                                   "how": "new_turn"})
        self.assertEqual(docs[3], {"type": "tool_call", "turn_id": "turn-1", "call_id": "toolu_1", "name": "read",
                                   "input": {"path": "src/a.py"}})
        self.assertEqual(docs[4], {"type": "tool_result", "turn_id": "turn-1", "call_id": "toolu_1", "status": "ok",
                                   "text": "     1\tprint(1)\n", "output_ref": None, "truncated": False,
                                   "spill": None})
        self.assertEqual(docs[7], {"type": "turn_ended", "turn_id": "turn-1", "outcome": "candidate_done",
                                   "result": {"code": None, "detail": "", "result_ref": None}, "reason": "end_turn"})
        usage = docs[1]
        self.assertEqual(usage["span"], "call")
        self.assertEqual(usage["input_tokens"], {"value": 100, "quality": "known", "provenance": "observed"})
        self.assertEqual(usage["cost_usd"], {"value": None, "quality": "unknown", "provenance": "unavailable"})
        second = h.provider.requests[1].messages
        self.assertEqual([m.role for m in second], ["user", "assistant", "user"])
        self.assertEqual(second[2].parts, (b.ToolResult("toolu_1", "     1\tprint(1)\n", False),))

    def test_input_accepted_comes_before_the_model_is_called(self) -> None:
        seen: list[list[str]] = []
        h = Harness(self, [lambda req: (seen.append(kinds(h.lines.all())),
                                        b.TurnResponse((b.TextPart("ok"),), b.END_TURN, b.Usage()))[1]])
        h.feed(msg("turn-1", "hi", "dlv-1"))
        h.finish()
        self.assertEqual(seen, [["input_accepted"]])

    def test_input_accepted_names_each_message_taken_and_none_never_read(self) -> None:
        h = Harness(self, [{"text": "one"}, {"text": "two"}, {"text": "three"}])
        h.feed(msg("turn-1", "first", "dlv-1"), b'{"kind":"user_message","turn_id":"turn-2"}',
               msg("turn-3", "second", "dlv-3"), CLOSE, msg("turn-4", "after close", "dlv-4"))
        h.thread.join()
        docs = h.lines.all()
        accepted = [(d["delivery_id"], d["turn_id"]) for d in docs if d["type"] == "input_accepted"]
        self.assertEqual(accepted, [("dlv-1", "turn-1"), ("dlv-3", "turn-3")])
        errors = [d["code"] for d in docs if d["type"] == "protocol_error"]
        self.assertEqual(errors, ["missing_field"])
        self.assertEqual(len(h.provider.requests), 2, "the message after close never reached the model")

    def test_turns_share_one_conversation(self) -> None:
        h = Harness(self, [{"text": "one"}, {"text": "two"}])
        h.feed(msg("turn-1", "first", "dlv-1"), msg("turn-2", "second", "dlv-2"))
        h.finish()
        texts = [[getattr(p, "text", None) for p in m.parts] for m in h.provider.requests[1].messages]
        self.assertEqual(texts, [["first"], ["one"], ["second"]])


class Interrupts(unittest.TestCase):
    def test_an_interrupt_during_the_model_call(self) -> None:
        h = Harness(self, [{"block_until_cancelled": True}, {"text": "after"}])
        h.feed(msg("turn-1", "long", "dlv-1"))
        h.provider.calls_started.wait()
        h.feed(interrupt("turn-9"), interrupt("turn-1"))
        h.feed(msg("turn-2", "next", "dlv-2"))
        docs = h.finish()
        self.assertIn({"type": "interrupt", "turn_id": "turn-9", "delivered": False}, docs)
        self.assertIn({"type": "interrupt", "turn_id": "turn-1", "delivered": True}, docs)
        ended = [d for d in docs if d["type"] == "turn_ended"]
        self.assertEqual(ended[0], {"type": "turn_ended", "turn_id": "turn-1", "outcome": "interrupted",
                                    "result": None, "reason": "interrupted"})
        self.assertEqual(ended[1]["outcome"], "candidate_done")

    def test_an_interrupt_during_tools_leaves_the_conversation_valid(self) -> None:
        started = threading.Event()

        def block(args, ctx):
            started.set()
            ctx.cancel.wait()
            raise b.Cancelled()

        blocker = r.Tool(b.ToolSpec("block", "waits", {"type": "object", "properties": {}}), block)
        h = Harness(self, [
            {"tool_calls": [{"id": "toolu_a", "name": "block", "input": {}},
                            {"id": "toolu_b", "name": "read", "input": {"path": "src/a.py"}}]},
            {"text": "fresh start"},
        ], tools=[blocker, read.TOOL, search.TOOL])
        h.feed(msg("turn-1", "go", "dlv-1"))
        started.wait()
        h.feed(interrupt("turn-1"), msg("turn-2", "again", "dlv-2"))
        docs = h.finish()
        results = [(d["call_id"], d["status"]) for d in docs if d["type"] == "tool_result"]
        self.assertEqual(results, [("toolu_a", "cancelled"), ("toolu_b", "cancelled")])
        self.assertEqual([d["outcome"] for d in docs if d["type"] == "turn_ended"], ["interrupted", "candidate_done"])
        convo = h.provider.requests[1].messages
        answered = {p.call_id for m in convo for p in m.parts if isinstance(p, b.ToolResult)}
        asked = {p.call_id for m in convo for p in m.parts if isinstance(p, b.ToolCall)}
        self.assertEqual(asked, answered, "every tool call has a result before the next call")
        self.assertTrue(all(p.is_error for m in convo for p in m.parts if isinstance(p, b.ToolResult)),
                        "the model is told those calls did not complete")


class Endings(unittest.TestCase):
    def ended(self, script, **kw) -> tuple[dict, list[dict], Harness]:
        h = Harness(self, script, **kw)
        h.feed(msg("turn-1", "go", "dlv-1"))
        docs = h.finish()
        return [d for d in docs if d["type"] == "turn_ended"][0], docs, h

    def test_a_limit_is_a_limit_line_and_a_paused_turn(self) -> None:
        end, docs, _ = self.ended([{"error": {"kind": "limit", "detail": "HTTP 429 rate_limit_error",
                                              "retry_after_s": 30, "reset_at": "2026-11-14T20:00:30.000Z"}}])
        limit = [d for d in docs if d["type"] == "limit"][0]
        self.assertEqual(limit, {"type": "limit", "turn_id": "turn-1", "scope": "connection", "kind": "plan_limit",
                                 "model": None, "bucket": None, "reset_at": "2026-11-14T20:00:30.000Z",
                                 "retry_after_s": 30})
        self.assertEqual((end["outcome"], end["reason"]), ("paused", "limit"))
        self.assertIsNone(end["result"]["code"])
        self.assertNotIn("usage", kinds(docs), "a call that failed reports no usage, rather than a made-up 0")

    def test_a_provider_failure_fails_the_turn_with_its_code(self) -> None:
        end, _, _ = self.ended([{"error": {"kind": "auth", "detail": "HTTP 401 authentication_error"}}])
        self.assertEqual((end["outcome"], end["result"]["code"]), ("failed", "provider_auth"))

    def test_budget_exhaustion_is_never_done(self) -> None:
        loop = {"tool_calls": [{"id": "toolu_x", "name": "read", "input": {"path": "src/a.py"}}]}
        end, docs, h = self.ended([loop, loop, loop], max_steps=2)
        self.assertEqual((end["outcome"], end["reason"]), ("paused", "step_budget"))
        self.assertEqual(len(h.provider.requests), 2)

    def test_a_tool_call_cut_off_at_the_output_limit_is_not_run(self) -> None:
        end, docs, h = self.ended([
            {"tool_calls": [{"id": "toolu_cut", "name": "read", "input": {"path": "src/a.py"}}],
             "stop_reason": "max_tokens"},
            {"text": "ok"},
        ])
        self.assertNotIn("tool_call", kinds(docs))
        self.assertEqual((end["outcome"], end["reason"]), ("paused", "max_tokens"))
        h2 = h.session.messages
        self.assertEqual(h2[-1].parts, (b.ToolResult("toolu_cut", "not run: the reply ended before this call was "
                                                                  "complete", True),))

    def test_a_refusal_fails_the_turn(self) -> None:
        end, _, _ = self.ended([{"text": "", "stop_reason": "refusal"}])
        self.assertEqual((end["outcome"], end["result"]["code"]), ("failed", "model_refusal"))

    def test_a_loop_bug_fails_one_turn_and_the_loop_goes_on(self) -> None:
        def broken(req):
            raise RuntimeError("bug")

        h = Harness(self, [broken, {"text": "fine"}])
        h.feed(msg("turn-1", "a", "dlv-1"), msg("turn-2", "b", "dlv-2"))
        docs = h.finish()
        ends = [(d["turn_id"], d["outcome"], (d["result"] or {}).get("code")) for d in docs
                if d["type"] == "turn_ended"]
        self.assertEqual(ends, [("turn-1", "failed", "loop_error"), ("turn-2", "candidate_done", None)])

    def test_an_approval_answer_with_nothing_waiting_is_refused(self) -> None:
        h = Harness(self, [])
        h.feed(b'{"kind":"approval_answer","approval_id":"apr-1","decision":"approve"}')
        docs = h.finish()
        self.assertEqual(docs[0]["code"], "no_pending_request")


if __name__ == "__main__":
    unittest.main()
