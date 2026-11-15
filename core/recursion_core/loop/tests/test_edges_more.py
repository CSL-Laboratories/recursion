"""What the round-4 reads of draft #379 found (the GPT-6 Astra and GPT-6.1 Sol confirming reads and the DeepSeek
break-it read, review-4-379), kept as tests: a tool input refused for its depth keeps no raw text; a block whose
text is not text, an absurd retry time and an over-long reply are typed failures; a delivered interrupt is not
lost to a limit; consumer edges of parse_input and the registry; the read tool's O_NOFOLLOW; and two rules the
Astra read showed the suite did not reach (its R29 and R39)."""

from __future__ import annotations

import json
import os
import threading
import unittest
from unittest.mock import patch

from recursion_core.loop import protocol as p
from recursion_core.loop.engine import Engine
from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import POSIX, context, workspace
from recursion_core.loop.tests.test_boundaries import ESCAPED, SECRET as BOUNDARY_SECRET
from recursion_core.loop.tests.test_edges import (SECRET, _Bytes, _interrupt, _message, _read, _session, _start,
                                                  _stop, _text_block, _tool_block, _wire)
from recursion_core.loop.tools import read
from recursion_core.loop.tools import registry as r
from recursion_core.loop.tools.workspace import ToolError


def _read_with(key: str, data: bytes) -> b.TurnResponse:
    """The Anthropic client with ``key`` as its key, reading ``data`` as one stream."""

    return a.AnthropicProvider(key)._read_stream(_Bytes(data), "req-test", None, b.CancelToken())


def _thinking_block(thinking: str, signature: str) -> tuple[dict, ...]:
    return ({"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": thinking}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": signature}},
            {"type": "content_block_stop", "index": 0})


def _deep_with(leaf: str, depth: int) -> dict:
    value: dict = {"path": leaf}
    for _ in range(depth - 1):
        value = {"k": value}
    return value


class DepthRefusalDropsRawText(unittest.TestCase):
    """Astra and Sol, round 4, change 1: a tool input refused for its depth keeps no raw text, since text never
    decoded and scrubbed could spell a secret with JSON escapes (the model would be shown it as INVALID_JSON)."""

    def _provider_read(self, *blocks) -> list[b.ToolCall]:
        data = _wire(_start(), *[event for block in blocks for event in block], *_stop("tool_use"))
        return list(_read_with(BOUNDARY_SECRET, data).tool_calls)

    def test_the_client_drops_the_raw_text_of_every_depth_refusal(self) -> None:
        deep = json.dumps(_deep_with(ESCAPED, b.MAX_INPUT_DEPTH + 1)).replace("\\\\", "\\")
        huge = '{"k":' * 200_000 + '{"path": "' + ESCAPED + '"}' + "}" * 200_000
        calls = self._provider_read(_tool_block(0, "toolu_1", deep), _tool_block(1, "toolu_2", huge),
                                    _tool_block(2, "toolu_3", "", start_input=_deep_with("x", b.MAX_INPUT_DEPTH + 1)))
        for call in calls:
            self.assertEqual((call.input, call.raw_input, call.input_error), (None, "", b.INPUT_TOO_DEEP))

    def test_a_valid_input_that_is_not_an_object_keeps_only_scrubbed_raw_text(self) -> None:
        calls = self._provider_read(_tool_block(0, "toolu_1", '["' + ESCAPED + '"]'))
        self.assertEqual((calls[0].input, calls[0].input_error), (None, "input is not a JSON object"))
        self.assertEqual(json.loads(calls[0].raw_input), ["[redacted]"])

    def test_the_engine_drops_the_raw_text_and_the_model_is_told_why(self) -> None:
        raw = json.dumps(_deep_with("x", b.MAX_INPUT_DEPTH + 1))[:-1] + ', "s": "' + ESCAPED + '"}'
        deep = b.TurnResponse((b.ToolCall("toolu_1", "read", _deep_with("x", b.MAX_INPUT_DEPTH + 1), raw),),
                              b.TOOL_USE, b.Usage())
        session, provider, lines = _session(self, [deep, {"text": "done"}])
        session.redactor.add(BOUNDARY_SECRET)
        session.run_turn("turn-1", "go", b.CancelToken())
        kept = session.messages[1].parts[0]
        self.assertEqual((kept.input, kept.raw_input, kept.input_error), (None, "", b.INPUT_TOO_DEEP))
        result = session.messages[2].parts[0]
        self.assertEqual(json.loads(result.text),
                         {"INVALID_JSON": "", "error": f"{b.INPUT_TOO_DEEP}; the tool did not run"})
        self.assertTrue(result.is_error)

    def test_the_engine_drops_the_raw_text_of_an_input_that_is_not_an_object(self) -> None:
        call = b.ToolCall("toolu_1", "read", {}, '["' + ESCAPED + '"]')
        object.__setattr__(call, "input", [BOUNDARY_SECRET])  # a provider bug the dataclass would refuse
        session, provider, lines = _session(self, [b.TurnResponse((call,), b.TOOL_USE, b.Usage()), {"text": "ok"}])
        session.run_turn("turn-1", "go", b.CancelToken())
        kept = session.messages[1].parts[0]
        self.assertEqual((kept.input, kept.raw_input), (None, ""))


class TypedVendorFaults(unittest.TestCase):
    """DeepSeek, round 4, F1 and F2, and its INFO 8: a block whose starting text is not text, a retry time the
    platform cannot write and a reply longer than the bound are typed failures that keep what was measured."""

    def test_a_block_whose_text_is_not_text_is_a_protocol_error(self) -> None:
        cases = {"text 5": {"type": "text", "text": 5}, "text list": {"type": "text", "text": ["a"]},
                 "thinking 5": {"type": "thinking", "thinking": 5, "signature": ""},
                 "signature 5": {"type": "thinking", "thinking": "", "signature": 5}}
        for name, block in cases.items():
            with self.subTest(name=name), self.assertRaises(b.ProtocolError) as caught:
                _read(_wire(_start(), {"type": "content_block_start", "index": 0, "content_block": block},
                            {"type": "content_block_stop", "index": 0}, *_stop()))
            self.assertEqual(caught.exception.usage.input_tokens.value, 7)

    def test_a_retry_time_too_far_away_still_gives_a_typed_limit(self) -> None:
        class Response:
            status = 429

            def read(self, n: int) -> bytes:
                return b'{"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}'

            def getheader(self, name: str, default=None):
                return "100000000000000000000" if name.lower() == "retry-after" else default

        exc = a.AnthropicProvider(SECRET)._http_error(Response(), "req-1")
        self.assertIsInstance(exc, b.LimitError)
        self.assertEqual((exc.retry_after_s, exc.reset_at), (1e20, None))

    def test_a_reply_longer_than_the_bound_is_a_protocol_error(self) -> None:
        with patch.object(a, "MAX_REPLY_CHARS", 20):
            for name, blocks in (("text", (*_text_block(0, "x" * 15), *_text_block(1, "y" * 15))),
                                 ("tool input", _tool_block(0, "toolu_1", '{"path": "' + "z" * 30 + '"}')),
                                 ("thinking", _thinking_block("t" * 30, "s")),
                                 ("signature", _thinking_block("t", "s" * 30))):
                with self.subTest(name=name), self.assertRaises(b.ProtocolError) as caught:
                    _read(_wire(_start(), *blocks, *_stop()))
                self.assertEqual(caught.exception.usage.input_tokens.value, 7)
            self.assertEqual(_read(_wire(_start(), *_text_block(0, "x" * 20), *_stop())).text, "x" * 20)


class DeliveredInterrupts(unittest.TestCase):
    """DeepSeek, round 4, F3: a turn whose interrupt was delivered ends interrupted, even when a limit or a
    failure came first (its own line still written); a turn the model had already finished stays done."""

    def _run(self, error: BaseException | None):
        def reply(request):
            engine.feed_line(_interrupt(1))  # read while the call runs: delivered
            if error is not None:
                raise error
            return b.TurnResponse((b.TextPart("answer"),), b.END_TURN, b.Usage())

        session, provider, lines = _session(self, [reply])
        engine = Engine(session, lines.writer, log=lambda m: None)
        engine.feed_line(_message(1))
        engine.input_ended()
        engine.serve()
        self.assertEqual([d["delivered"] for d in lines.of("interrupt")], [True])
        return lines

    def test_a_limit_after_a_delivered_interrupt_still_ends_interrupted(self) -> None:
        lines = self._run(b.LimitError("rate limited", retry_after_s=30, reset_at="2026-11-14T12:00:00.000Z"))
        self.assertEqual(len(lines.of("limit")), 1)
        self.assertEqual(len(lines.of("usage")), 1)
        self.assertEqual(lines.of("turn_ended")[0]["outcome"], "interrupted")

    def test_a_failure_after_a_delivered_interrupt_still_ends_interrupted(self) -> None:
        lines = self._run(b.ServerError("the vendor failed"))
        # Round 5 (DeepSeek F2): the failure's result stays on the interrupted line, so its code is not lost.
        self.assertEqual((lines.of("turn_ended")[0]["outcome"], lines.of("turn_ended")[0]["result"]["code"]),
                         ("interrupted", b.ServerError.code))

    def test_a_turn_the_model_had_finished_stays_done(self) -> None:
        session, provider, lines = _session(self, [{"text": "answer"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        real = lines.writer.write

        def write(kind, /, **fields):
            real(kind, **fields)
            if kind == "text":  # the reply is in; the interrupt comes before the turn's end
                engine.feed_line(_interrupt(1))

        lines.writer.write = write
        engine.feed_line(_message(1))
        engine.input_ended()
        engine.serve()
        self.assertEqual([d["delivered"] for d in lines.of("interrupt")], [True])
        self.assertEqual(lines.of("turn_ended")[0]["outcome"], "candidate_done")


class ConsumerEdges(unittest.TestCase):
    """DeepSeek, round 4, INFO 5 to 7: parse_input refuses what is not a line, the registry a name that is not
    text, and the read tool opens with O_NOFOLLOW (a path swapped for a link after it was resolved)."""

    def test_parse_input_refuses_what_is_not_a_line(self) -> None:
        for value in (None, memoryview(b"{}"), 3):
            with self.subTest(value=type(value).__name__), self.assertRaises(p.InputError) as caught:
                p.parse_input(value)
            self.assertEqual(caught.exception.code, "bad_input")

    def test_a_tool_name_that_is_not_text_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            r.ToolRegistry([r.Tool(b.ToolSpec(3, "", {"type": "object"}), lambda args, ctx: r.ToolOutput("x"))])

    @unittest.skipUnless(POSIX and hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is POSIX")
    def test_the_read_tool_does_not_follow_a_link_swapped_in_after_resolving(self) -> None:
        ws = workspace(self, {"real.txt": "inside\n"})
        link = ws.root / "swapped.txt"
        os.symlink(ws.root / "real.txt", link)
        with patch.object(ws, "resolve", return_value=link), self.assertRaises(ToolError) as caught:
            read.run({"path": "swapped.txt"}, context(ws))
        self.assertIn("cannot open", str(caught.exception))


class AstraLaterRulesRound4(unittest.TestCase):
    """Two rules the Astra read of round 4 removed without the lane suite noticing (its R29 and R39)."""

    def test_a_turns_end_waits_for_an_interrupt_answer_being_written(self) -> None:
        # R29: the answer and the turn's end are written under one lock, so the end waits for the answer.
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()

        def reply(request):
            returned.wait(10)
            return b.TurnResponse((b.TextPart("ok"),), b.END_TURN, b.Usage())

        session, provider, lines = _session(self, [reply])
        engine = Engine(session, lines.writer, log=lambda m: None)
        real = lines.writer.write

        def write(kind, /, **fields):
            if kind == "interrupt":
                entered.set()
                release.wait(10)
            real(kind, **fields)

        lines.writer.write = write
        engine.feed_line(_message(1))
        engine.input_ended()
        worker = threading.Thread(target=engine.serve, daemon=True)
        worker.start()
        self.assertTrue(provider.calls_started.wait(10))
        stopper = threading.Thread(target=engine.feed_line, args=(_interrupt(1),), daemon=True)
        stopper.start()
        try:
            self.assertTrue(entered.wait(10))
            returned.set()
            worker.join(0.3)
            self.assertTrue(worker.is_alive(), "the turn's end did not wait for the interrupt's answer")
            self.assertEqual(lines.of("turn_ended"), [])
        finally:
            release.set()
            returned.set()
            stopper.join(10)
            worker.join(10)
        types = [d["type"] for d in lines.all()]
        self.assertLess(types.index("interrupt"), types.index("turn_ended"))

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "counts descriptors through /proc")
    def test_serving_input_leaves_no_descriptor_behind(self) -> None:
        # R39: the wake pipe is closed once the input thread has ended.
        from recursion_core.loop import __main__ as entry
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(5):
            session, provider, lines = _session(self, [{"text": "ok"}])
            engine = Engine(session, lines.writer, log=lambda m: None)
            r_fd, w_fd = os.pipe()
            try:
                os.write(w_fd, _message(1) + b"\n" + b'{"kind":"close"}\n')
                self.assertEqual(entry.serve_input(engine, r_fd), 0)
            finally:
                os.close(r_fd)
                os.close(w_fd)
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)


if __name__ == "__main__":
    unittest.main()
