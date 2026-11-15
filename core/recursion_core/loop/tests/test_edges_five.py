"""What the round-5 reads of draft #379 found (the GPT-6 Astra and GPT-6.1 Sol confirming reads and the DeepSeek
break-it read, review-5-379), kept as tests: a block's type must be text; what a block starts with counts toward
the reply's bound; nothing follows ``closed`` from the process; a delivered interrupt keeps the result it ends;
NaN and infinities never reach a line; a block-start input that is not an object; two calls of one id; parts
whose text is not text; and the rules the reads showed the suite did not reach."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from recursion_core.loop import protocol as p
from recursion_core.loop.engine import Engine
from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import POSIX, Lines, private_dir
from recursion_core.loop.tests.test_edges import (CORE, SECRET, _interrupt, _message, _nested, _read, _session,
                                                  _start, _stop, _tool_block, _wire)


def _strict(line: str) -> dict:
    def refuse(name: str):
        raise ValueError(name)

    return json.loads(line, parse_constant=refuse)


def _state() -> tuple[a._StreamState, a.AnthropicProvider]:
    state, provider = a._StreamState("req-test"), a.AnthropicProvider(SECRET)
    state.feed(_start(), None, provider)
    return state, provider


def _start_block(state, provider, index: int, block: dict) -> None:
    state.feed({"type": "content_block_start", "index": index, "content_block": block}, None, provider)
    state.feed({"type": "content_block_stop", "index": index}, None, provider)


class BlockTypes(unittest.TestCase):
    """Astra and Sol, round 5, change 1 (and DeepSeek's INFO 6): a block's type is looked at only once it is known
    to be text; any other type, or none, is a ProtocolError that keeps what the stream had measured."""

    def test_a_block_whose_type_is_not_text_is_a_protocol_error(self) -> None:
        for value in ([], {}, 5, "", None):
            with self.subTest(value=value):
                entry = {"block": {} if value is None else {"type": value}, "text": [], "json": [], "thinking": [],
                         "signature": []}
                with self.assertRaises(b.ProtocolError):
                    a._StreamState("req-test")._finish_block(entry, a.AnthropicProvider(SECRET))
                block = {} if value is None else {"type": value}
                with self.assertRaises(b.ProtocolError) as caught:
                    _read(_wire(_start(), {"type": "content_block_start", "index": 0, "content_block": block},
                                {"type": "content_block_stop", "index": 0}, *_stop()))
                self.assertEqual(caught.exception.usage.input_tokens.value, 7)


class StartContentCounts(unittest.TestCase):
    """Astra and Sol, round 5, change 2 (and DeepSeek's F4): what a block starts with counts toward
    MAX_REPLY_CHARS with its deltas, and a reply exactly at the bound is taken."""

    def test_what_a_block_starts_with_counts_toward_the_bound(self) -> None:
        cases = {
            "text": [{"type": "text", "text": "x" * 11}, {"type": "text", "text": "y" * 11}],
            "thinking": [{"type": "thinking", "thinking": "t" * 21, "signature": ""}],
            "signature": [{"type": "thinking", "thinking": "", "signature": "s" * 21}],
            "opaque data": [{"type": "redacted_thinking", "data": "d" * 21}],
            "tool input": [{"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "p" * 21}}],
        }
        with patch.object(a, "MAX_REPLY_CHARS", 20):
            for name, blocks in cases.items():
                with self.subTest(name=name), self.assertRaises(b.ProtocolError):
                    state, provider = _state()
                    for index, block in enumerate(blocks):
                        _start_block(state, provider, index, block)

    def test_start_and_deltas_count_together_and_the_bound_itself_is_taken(self) -> None:
        with patch.object(a, "MAX_REPLY_CHARS", 20):
            start = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "x" * 10}}
            delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "y" * 10}}
            stop = {"type": "content_block_stop", "index": 0}
            self.assertEqual(_read(_wire(_start(), start, delta, stop, *_stop())).text, "x" * 10 + "y" * 10)
            more = dict(delta, delta={"type": "text_delta", "text": "z"})
            with self.assertRaises(b.ProtocolError):
                _read(_wire(_start(), start, delta, more, stop, *_stop()))


@unittest.skipUnless(POSIX, "the engine runs in a Linux run unit")
class NothingFollowsClosed(unittest.TestCase):
    """DeepSeek, round 5, F1: the process writes nothing after ``closed``, however many lines follow ``close`` on
    its standard input; an Engine driven in-process still answers a line fed after it (both confirming reads
    test that an interrupt read after its turn's end is answered ``delivered: false``)."""

    def test_the_process_writes_nothing_after_closed(self) -> None:
        ws = private_dir(self, "loop-ws-")
        home = private_dir(self, "loop-home-")
        script = home / "steps.json"
        script.write_text(json.dumps([{"text": "the answer"}]), encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(CORE), "LC_ALL": "C.UTF-8"}
        tail = (_interrupt(1) + b"\n") * 2000 + b"not json\n" * 200
        for round_no in range(3):
            with self.subTest(round=round_no):
                proc = subprocess.Popen(
                    [sys.executable, "-B", "-m", "recursion_core.loop", "--provider", "fake", "--fake-script",
                     str(script), "--home", str(home / f"engine-{round_no}")],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=ws)
                try:
                    proc.stdin.write(_message(1) + b"\n" + b'{"kind":"close"}\n' + tail)
                    proc.stdin.flush()
                    out = proc.stdout.read()
                    err = proc.stderr.read()
                    code = proc.wait(timeout=60)
                finally:
                    proc.stdin.close()
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    proc.stdout.close()
                    proc.stderr.close()
                types = [_strict(line)["type"] for line in out.decode("utf-8").splitlines()]
                self.assertEqual((types[-1], types.count("closed")), ("closed", 1))
                self.assertEqual((code, err.decode("utf-8", "replace")), (0, ""))

    def test_a_sealed_writer_writes_nothing_after_its_last_line(self) -> None:
        lines = Lines()
        sealed = p.Writer(lines.buffer, lines.redactor, seal_after="closed")
        sealed.write("text", turn_id="turn-1", text="a")
        sealed.write("closed")
        sealed.write("interrupt", turn_id="turn-1", delivered=False)
        self.assertEqual([d["type"] for d in lines.all()], ["text", "closed"])
        plain = Lines()
        plain.writer.write("closed")
        plain.writer.write("interrupt", turn_id="turn-1", delivered=False)
        self.assertEqual([d["type"] for d in plain.all()], ["closed", "interrupt"])


class InterruptKeepsTheResult(unittest.TestCase):
    """DeepSeek, round 5, F2 (and its INFO 9): a delivered interrupt that ends a turn which failed or paused keeps
    that turn's result and reason, so a failure's code is not lost; run_turn returns what the line says."""

    def test_a_paused_turn_ended_by_an_interrupt_keeps_its_result(self) -> None:
        session, provider, lines = _session(self, [{"text": "cut", "stop_reason": "max_tokens"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        real = lines.writer.write

        def write(kind, /, **fields):
            real(kind, **fields)
            if kind == "text":
                engine.feed_line(_interrupt(1))

        lines.writer.write = write
        engine.feed_line(_message(1))
        engine.input_ended()
        engine.serve()
        ended = lines.of("turn_ended")[0]
        self.assertEqual((ended["outcome"], ended["reason"], ended["result"]["detail"]),
                         ("interrupted", "max_tokens", "the reply reached the output limit"))

    def test_run_turn_returns_the_outcome_its_line_says(self) -> None:
        cancel = b.CancelToken()

        def reply(request):
            cancel.cancel()
            raise b.LimitError("rate limited", retry_after_s=30)

        session, provider, lines = _session(self, [reply])
        self.assertEqual(session.run_turn("turn-1", "go", cancel), "interrupted")
        self.assertEqual(lines.of("turn_ended")[0]["outcome"], "interrupted")

    def test_a_cancel_read_where_the_end_is_written(self) -> None:
        # Astra's I04: the interrupt comes as the end is being written, after the outcome was chosen.
        session, provider, lines = _session(self, [b.LimitError("synthetic limit", retry_after_s=1), {"text": "next"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        actual = engine._ending

        def end(write):
            if not lines.of("turn_ended"):
                engine.feed_line(_interrupt(1))
            actual(write)

        session.ending = end
        engine.feed_line(_message(1))
        engine.feed_line(_message(2))
        engine.input_ended()
        engine.serve()
        self.assertEqual([d["outcome"] for d in lines.of("turn_ended")], ["interrupted", "candidate_done"])
        self.assertEqual(len(lines.of("limit")), 1)


class NonFiniteNumbers(unittest.TestCase):
    """DeepSeek, round 5, F3: NaN and the infinities are not JSON. The client refuses them where the vendor's JSON
    is read, the engine keeps a tool input holding one as input that did not parse, and no line carries one."""

    def test_the_client_never_takes_a_number_json_does_not_have(self) -> None:
        call = _read(_wire(_start(), *_tool_block(0, "toolu_1", '{"path": "a.txt", "limit": NaN}'),
                           *_stop("tool_use"))).tool_calls[0]
        self.assertEqual((call.input, call.input_error), (None, "input is not valid JSON: NaN is not a JSON number"))
        with self.assertRaises(b.ProtocolError):
            _read(_wire(_start(), '{"type": "ping", "x": Infinity}', *_stop()))

    def test_the_engine_keeps_such_an_input_as_input_that_did_not_parse(self) -> None:
        reply = b.TurnResponse((b.ToolCall("toolu_1", "read", {"path": "a.txt", "limit": float("nan")}, "{}"),),
                               b.TOOL_USE, b.Usage())
        session, provider, lines = _session(self, [reply, {"text": "done"}])
        session.run_turn("turn-1", "go", b.CancelToken())
        for line in lines.raw().splitlines():
            _strict(line)
        kept = session.messages[1].parts[0]
        self.assertEqual((kept.input, kept.raw_input, kept.input_error), (None, "", b.INPUT_NOT_FINITE))
        self.assertEqual(json.loads(session.messages[2].parts[0].text),
                         {"INVALID_JSON": "", "error": f"{b.INPUT_NOT_FINITE}; the tool did not run"})

    def test_the_writer_refuses_such_a_number(self) -> None:
        lines = Lines()
        with self.assertRaises(ValueError):
            lines.writer.write("tool_call", turn_id="turn-1", input={"x": float("inf")})
        self.assertEqual(lines.raw(), "")

    def test_holds_nonfinite(self) -> None:
        self.assertTrue(b.holds_nonfinite({"a": [1, {"b": float("-inf")}]}))
        self.assertFalse(b.holds_nonfinite({"a": [1, 2.5, "nan"]}))


class ReplyShape(unittest.TestCase):
    """DeepSeek, round 5, INFO 5, 7 and 8: a block-start input that is not an object, two calls of one id, and a
    text part or tool result whose text is not text."""

    def test_a_block_start_input_that_is_not_an_object_is_input_that_did_not_parse(self) -> None:
        state, provider = _state()
        _start_block(state, provider, 0, {"type": "tool_use", "id": "toolu_1", "name": "read", "input": [1]})
        call = state.done[0]
        self.assertEqual((call.input, call.raw_input, call.input_error), (None, "", "input is not a JSON object"))

    def test_two_calls_of_one_id_are_refused_by_the_client_and_the_engine(self) -> None:
        with self.assertRaises(b.ProtocolError):
            _read(_wire(_start(), *_tool_block(0, "toolu_1", '{"path": "a.txt"}'),
                        *_tool_block(1, "toolu_1", '{"path": "a.txt"}'), *_stop("tool_use")))
        twice = b.TurnResponse((b.ToolCall("toolu_1", "read", {"path": "a.txt"}),
                                b.ToolCall("toolu_1", "read", {"path": "a.txt"})), b.TOOL_USE, b.Usage())
        session, provider, lines = _session(self, [twice])
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "failed")
        self.assertEqual(lines.of("turn_ended")[0]["result"]["code"], "provider_protocol")
        self.assertEqual(lines.of("tool_call"), [])

    def test_a_part_whose_text_is_not_text_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            b.TextPart(5)
        with self.assertRaises(ValueError):
            b.ToolResult("toolu_1", ["x"])
        session, provider, lines = _session(self, [{"text": 5}])
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "failed")
        self.assertEqual(lines.of("turn_ended")[0]["result"]["code"], "provider_protocol")


class AstraLaterRound5(unittest.TestCase):
    """Astra's D01 (and Sol's later point): a deep block-start input refused with only blank partial JSON keeps no
    raw text, not even the blank."""

    def test_a_depth_refusal_with_blank_partial_json_keeps_no_raw_text(self) -> None:
        state, provider = _state()
        block = {"type": "tool_use", "id": "toolu_1", "name": "read", "input": _nested(b.MAX_INPUT_DEPTH + 1)}
        state.feed({"type": "content_block_start", "index": 0, "content_block": block}, None, provider)
        state.feed({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta",
                                                                         "partial_json": "   "}}, None, provider)
        state.feed({"type": "content_block_stop", "index": 0}, None, provider)
        call = state.done[0]
        self.assertEqual((call.input, call.raw_input, call.input_error), (None, "", b.INPUT_TOO_DEEP))


if __name__ == "__main__":
    unittest.main()
