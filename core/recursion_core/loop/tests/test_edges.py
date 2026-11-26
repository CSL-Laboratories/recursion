"""What the round-3 reads of draft #379 found (the GPT-6 Astra confirming read and the DeepSeek break-it read,
review-3-379), kept as tests: the texts a failure or a reply carries that the vendor sent; the process's exit
after ``close`` with standard input left open; a stream or a tool input nested too deep; the result settle
gives a call that ran; a reply part a reply cannot carry; what scrub_across takes; the order of an interrupt's
answer and its turn's end; and four rules the Astra read showed the suite did not reach."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from recursion_core.loop import protocol as p
from recursion_core.loop.engine import NOT_RUN, Config, Engine, Session
from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider
from recursion_core.loop.tests.support import POSIX, Canned, Lines, Loopback, context, private_dir, registry, workspace
from recursion_core.loop.tools import registry as r

SECRET = "synthetic-canary-3333-not-a-real-key-3333"
CORE = Path(__file__).resolve().parents[3]


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read1(self, n: int) -> bytes:
        return self._buf.read(n)


def _wire(*docs) -> bytes:
    return "".join("data: " + (d if isinstance(d, str) else json.dumps(d)) + "\n\n" for d in docs).encode()


def _start(model: str = "synthetic-model") -> dict:
    return {"type": "message_start",
            "message": {"model": model, "usage": {"input_tokens": 7, "cache_read_input_tokens": 3}}}


def _stop(reason: str = "end_turn") -> tuple[dict, ...]:
    return ({"type": "message_delta", "delta": {"stop_reason": reason}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"})


def _text_block(index: int, text: str) -> tuple[dict, ...]:
    return ({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": index})


def _tool_block(index: int, call_id: str, partial_json: str, start_input: dict | None = None) -> tuple[dict, ...]:
    begin = {"type": "content_block_start", "index": index,
             "content_block": {"type": "tool_use", "id": call_id, "name": "read", "input": start_input or {}}}
    delta = {"type": "content_block_delta", "index": index,
             "delta": {"type": "input_json_delta", "partial_json": partial_json}}
    return (begin, delta, {"type": "content_block_stop", "index": index}) if partial_json else (
        begin, {"type": "content_block_stop", "index": index})


def _read(data: bytes, on_event=None, cancel: b.CancelToken | None = None) -> b.TurnResponse:
    return a.AnthropicProvider(SECRET)._read_stream(_Bytes(data), "req-test", on_event, cancel or b.CancelToken())


def _session(case, steps) -> tuple[Session, FakeProvider, Lines]:
    provider = FakeProvider(steps)
    lines = Lines([SECRET])
    session = Session(provider, registry(), workspace(case, {"a.txt": "x\n"}), lines.writer, lines.redactor,
                      Config("fake-model"))
    return session, provider, lines


def _message(n: int, delivery: str | None = None) -> bytes:
    return json.dumps({"kind": "user_message", "turn_id": f"turn-{n}", "delivery_id": delivery or f"dlv-{n}",
                       "text": f"message {n}"}).encode()


def _interrupt(n: int) -> bytes:
    return json.dumps({"kind": "interrupt", "turn_id": f"turn-{n}"}).encode()


def _nested(depth: int) -> dict:
    """An object nested ``depth`` levels deep (a flat object is 1)."""

    value: dict = {}
    for _ in range(depth - 1):
        value = {"k": value}
    return value


class VendorTexts(unittest.TestCase):
    """Astra, round 3, change 1: the texts a failure carries (its error type, the request id, the model the
    vendor named) and those a reply carries (its model, the vendor's stop reason, the request id) hold no key,
    however the call ended."""

    def _call(self, canned: Canned) -> b.TurnResponse:
        local = Loopback(self, [canned])
        return a.AnthropicProvider(SECRET, base_url=local.url).stream_turn(b.TurnRequest("synthetic-model", "s", ()))

    def _assert_clean(self, exc: BaseException) -> None:
        for name in ("detail", "vendor_type", "request_id", "served_model"):
            self.assertNotIn(SECRET, getattr(exc, name, None) or "", name)
        self.assertNotIn(SECRET, repr(exc.args))

    def test_a_stream_error_carries_no_key_in_any_field(self) -> None:
        for field in ("vendor_type", "request_id", "served_model"):
            with self.subTest(field=field):
                start = _start(SECRET if field == "served_model" else "synthetic-model")
                error = {"type": "error", "error": {"type": SECRET if field == "vendor_type" else "api_error",
                                                    "message": "synthetic failure"}}
                headers = {"content-type": "text/event-stream",
                           "request-id": SECRET if field == "request_id" else "req-1"}
                with self.assertRaises(b.ProviderError) as caught:
                    self._call(Canned(200, _wire(start, error), headers))
                self._assert_clean(caught.exception)
                self.assertEqual(getattr(caught.exception, field), "[redacted]", "scrubbed, not dropped")

    def test_an_http_error_carries_no_key_in_any_field(self) -> None:
        body = json.dumps({"type": "error", "error": {"type": SECRET, "message": "about " + SECRET}}).encode()
        with self.assertRaises(b.ProviderError) as caught:
            self._call(Canned(400, body, {"content-type": "application/json", "request-id": SECRET}))
        self._assert_clean(caught.exception)

    def test_a_reply_carries_no_key_in_its_model_stop_reason_or_request_id(self) -> None:
        docs = (_start(SECRET), *_text_block(0, "fine"), *_stop(SECRET))
        response = self._call(Canned(200, _wire(*docs), {"content-type": "text/event-stream", "request-id": SECRET}))
        self.assertEqual((response.served_model, response.vendor_stop_reason, response.request_id),
                         ("[redacted]", "[redacted]", "[redacted]"))
        self.assertEqual(response.text, "fine")

    def test_an_interrupted_call_carries_no_key_in_its_model(self) -> None:
        cancel = b.CancelToken()
        with self.assertRaises(b.Cancelled) as caught:
            _read(_wire(_start(SECRET), *_text_block(0, "partial"), *_stop()), lambda event: cancel.cancel(), cancel)
        self.assertEqual(caught.exception.served_model, "[redacted]")


@unittest.skipUnless(POSIX, "the engine runs in a Linux run unit")
class ProcessClose(unittest.TestCase):
    """DeepSeek, round 3, F1: after ``close``, with standard input still open (a driver that waits for the
    process before it closes the pipe), the process exits 0 with nothing on standard error. It used to abort
    (SIGABRT, exit -6) with its input thread blocked inside sys.stdin's buffered reader."""

    def test_close_with_standard_input_left_open_exits_cleanly(self) -> None:
        ws = private_dir(self, "loop-ws-")
        home = private_dir(self, "loop-home-")
        script = home / "steps.json"
        script.write_text(json.dumps([{"text": "the answer"}]), encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(CORE), "LC_ALL": "C.UTF-8"}
        for round_no in range(3):
            with self.subTest(round=round_no):
                proc = subprocess.Popen(
                    [sys.executable, "-B", "-m", "recursion_core.loop", "--provider", "fake", "--fake-script",
                     str(script), "--home", str(home / f"engine-{round_no}")],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=ws)
                try:
                    proc.stdin.write(_message(1) + b"\n" + b'{"kind":"close"}\n')
                    proc.stdin.flush()
                    out = proc.stdout.read()  # to its end: the process has exited, its standard input still open
                    err = proc.stderr.read()
                    code = proc.wait(timeout=60)
                finally:
                    proc.stdin.close()
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
                    proc.stdout.close()
                    proc.stderr.close()
                types = [json.loads(line)["type"] for line in out.decode("utf-8").splitlines()]
                self.assertEqual(types[-2:], ["turn_ended", "closed"])
                self.assertEqual((code, err.decode("utf-8", "replace")), (0, ""))


class InputLines(unittest.TestCase):
    """The input thread's reader: lines split anywhere across reads come out the same, and ``stop`` ends a
    waiting read without taking a begun line as a line."""

    def test_lines_split_anywhere_across_reads_come_out_the_same(self) -> None:
        data = b'{"a":1}\r\n\n  \n{"b":2}\n' + b"y" * 40 + b"\n" + b'{"c":3}'
        old = p.MAX_INPUT_LINE
        p.MAX_INPUT_LINE = 16
        try:
            for size in (1, 2, 3, 7, 64):
                with self.subTest(size=size):
                    stream = io.BytesIO(data)
                    got: list[bytes] = []
                    long_lines: list[int] = []
                    p.lines_from(lambda n, s=stream, k=size: s.read(min(n, k)), got.append,
                                 lambda: long_lines.append(1))
                    self.assertEqual(got, [b'{"a":1}', b'{"b":2}', b'{"c":3}'])
                    self.assertEqual(long_lines, [1])
        finally:
            p.MAX_INPUT_LINE = old

    def test_a_line_at_the_limit_is_taken_and_one_byte_more_is_not(self) -> None:
        old = p.MAX_INPUT_LINE
        p.MAX_INPUT_LINE = 8
        try:
            got: list[bytes] = []
            long_lines: list[int] = []
            p.read_lines(io.BytesIO(b"x" * 8 + b"\n" + b"y" * 9 + b"\n" + b"z" * 9), got.append,
                         lambda: long_lines.append(1))
        finally:
            p.MAX_INPUT_LINE = old
        self.assertEqual((got, long_lines), ([b"x" * 8], [1, 1]))

    @unittest.skipUnless(POSIX, "stop waits on a POSIX pipe")
    def test_the_input_thread_has_ended_when_the_engine_returns(self) -> None:
        from recursion_core.loop import __main__ as entry
        session, provider, lines = _session(self, [{"text": "answer"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        r_fd, w_fd = os.pipe()
        self.addCleanup(os.close, r_fd)
        self.addCleanup(os.close, w_fd)
        os.write(w_fd, _message(1) + b"\n" + b'{"kind":"close"}\n')  # and the pipe stays open
        self.assertEqual(entry.serve_input(engine, r_fd), 0)
        self.assertEqual([t for t in threading.enumerate() if t.name == entry.INPUT_THREAD], [])
        self.assertEqual([d["type"] for d in lines.all()][-1], "closed")

    @unittest.skipUnless(POSIX, "stop waits on a POSIX pipe")
    def test_stop_ends_a_waiting_read_and_a_begun_line_is_not_a_line(self) -> None:
        r_fd, w_fd = os.pipe()
        self.addCleanup(os.close, r_fd)
        self.addCleanup(os.close, w_fd)
        reader = p.InputReader(r_fd)
        self.addCleanup(reader.close)
        got: list[bytes] = []
        first = threading.Event()
        ended: list[str] = []

        def on_line(line: bytes) -> None:
            got.append(line)
            first.set()

        def pump() -> None:
            try:
                p.lines_from(reader.read, on_line, lambda: None)
            except p.Stopped:
                ended.append("stopped")

        os.write(w_fd, b'{"kind":"close"}\n{"kind":"user_mes')
        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        self.assertTrue(first.wait(10))
        reader.stop()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual((got, ended), ([b'{"kind":"close"}'], ["stopped"]))


class DeepNesting(unittest.TestCase):
    """DeepSeek, round 3, F2: a stream nested deeper than the client reads is a ``ProtocolError`` that keeps
    what the stream had measured, never an untyped ``RecursionError``; a tool input nested past the bound is
    input that did not parse, in the client and in the engine."""

    def test_a_stream_event_nested_too_deep_is_a_protocol_error_that_keeps_what_was_measured(self) -> None:
        deep = "[" * 200_000 + "]" * 200_000
        with self.assertRaises(b.ProtocolError) as caught:
            _read(_wire(_start(), deep))
        usage = caught.exception.usage
        self.assertEqual((usage.input_tokens.value, usage.cache_read_tokens.value), (7, 3))
        self.assertEqual(caught.exception.served_model, "synthetic-model")

    def test_a_recursion_error_anywhere_in_reading_the_stream_is_a_protocol_error(self) -> None:
        data = _wire(_start(), *_tool_block(0, "toolu_1", '{"path": "a.txt"}'), *_stop("tool_use"))
        with patch.object(a.AnthropicProvider, "_scrub_obj", side_effect=RecursionError), \
                self.assertRaises(b.ProtocolError) as caught:
            _read(data)
        self.assertEqual(caught.exception.usage.input_tokens.value, 7)

    def test_an_error_body_nested_too_deep_is_still_a_typed_error(self) -> None:
        class Response:
            status = 400

            def read(self, n: int) -> bytes:
                return (b"[" * 300_000 + b"]" * 300_000)[:n]

            def getheader(self, name: str, default=None):
                return default

        exc = a.AnthropicProvider(SECRET)._http_error(Response(), "req-1")
        self.assertIsInstance(exc, b.ProviderError)
        self.assertEqual((exc.status, exc.vendor_type), (400, None))

    def test_a_tool_input_nested_past_the_bound_is_input_that_did_not_parse(self) -> None:
        bound = b.MAX_INPUT_DEPTH
        huge = '{"k":' * 200_000 + "{}" + "}" * 200_000
        data = _wire(_start(), *_tool_block(0, "toolu_1", json.dumps(_nested(bound))),
                     *_tool_block(1, "toolu_2", json.dumps(_nested(bound + 1))),
                     *_tool_block(2, "toolu_3", huge),
                     *_tool_block(3, "toolu_4", "", start_input=_nested(bound + 1)), *_stop("tool_use"))
        calls = _read(data).tool_calls
        self.assertEqual((calls[0].input, calls[0].input_error), (_nested(bound), None))
        for call in calls[1:]:
            self.assertEqual((call.call_id, call.input, call.input_error), (call.call_id, None, b.INPUT_TOO_DEEP))

    def test_nests_deeper_counts_without_recursion(self) -> None:
        self.assertFalse(b.nests_deeper({"a": 1, "b": [1, {"c": []}]}, 4))  # object, array, object, array
        self.assertTrue(b.nests_deeper({"a": 1, "b": [1, {"c": []}]}, 3))
        self.assertFalse(b.nests_deeper("text", 0))
        deep: list = []
        for _ in range(100_000):
            deep = [deep]
        self.assertTrue(b.nests_deeper(deep, b.MAX_INPUT_DEPTH))

    def test_the_engine_keeps_a_too_deep_input_as_input_that_did_not_parse(self) -> None:
        deep = b.TurnResponse((b.ToolCall("toolu_1", "read", _nested(b.MAX_INPUT_DEPTH + 1), "{}"),), b.TOOL_USE,
                              b.Usage())
        session, provider, lines = _session(self, [deep, {"text": "done"}])
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "candidate_done")
        self.assertIsNone(lines.of("tool_call")[0]["input"])
        self.assertEqual(lines.of("tool_result")[0]["status"], r.ERROR)
        self.assertIsNone(session.messages[1].parts[0].input)

    def test_a_reply_too_deep_for_the_engine_to_walk_is_refused_not_a_loop_error(self) -> None:
        reply = b.TurnResponse((b.OpaquePart("anthropic", {"type": "redacted_thinking"}),), b.END_TURN, b.Usage())
        session, provider, lines = _session(self, [reply])
        with patch.object(session.redactor, "holds", side_effect=RecursionError):
            self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "failed")
        ended = lines.of("turn_ended")[0]
        self.assertEqual(ended["result"]["code"], "provider_protocol")
        self.assertIn("deeper than the loop can walk", ended["result"]["detail"])


class SettleKeepsWhatRan(unittest.TestCase):
    """DeepSeek, round 3, F3: after a loop error between two tool calls of one reply, the call that ran keeps
    its own result in the conversation; only the one that did not run says so."""

    def test_a_call_that_ran_keeps_its_own_result_after_a_loop_error(self) -> None:
        reply = {"tool_calls": [{"id": "toolu_1", "name": "read", "input": {"path": "a.txt"}},
                                {"id": "toolu_2", "name": "read", "input": {"path": "a.txt"}}]}
        session, provider, lines = _session(self, [reply])
        engine = Engine(session, lines.writer, log=lambda m: None)
        real = session.registry.dispatch

        def dispatch(call, ctx):
            if call.call_id == "toolu_2":
                raise RuntimeError("a bug between two tools")
            return real(call, ctx)

        session.registry.dispatch = dispatch
        engine.feed_line(_message(1))
        engine.input_ended()
        engine.serve()
        written = lines.of("tool_result")
        self.assertEqual([(d["call_id"], d["status"]) for d in written], [("toolu_1", r.OK)])
        self.assertEqual([m.role for m in session.messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual([(x.call_id, x.text, x.is_error) for x in session.messages[2].parts],
                         [("toolu_1", written[0]["text"], False), ("toolu_2", NOT_RUN, True)])
        self.assertEqual(lines.of("turn_ended")[0]["result"]["code"], "loop_error")


class ReplyParts(unittest.TestCase):
    """DeepSeek, round 3, F5: a reply holding a part a reply cannot carry (a tool result) is refused, never
    kept and sent back as the assistant's."""

    def test_a_reply_holding_a_tool_result_is_refused(self) -> None:
        reply = b.TurnResponse((b.TextPart("hi"), b.ToolResult("toolu_1", "vendor says", False)), b.END_TURN,
                               b.Usage())
        session, provider, lines = _session(self, [reply, {"text": "ok"}])
        self.assertEqual(session.run_turn("turn-1", "one", b.CancelToken()), "failed")
        ended = lines.of("turn_ended")[0]
        self.assertEqual(ended["result"]["code"], "provider_protocol")
        self.assertIn("ToolResult", ended["result"]["detail"])
        session.run_turn("turn-2", "two", b.CancelToken())
        sent = provider.requests[1].messages
        self.assertFalse([part for m in sent if m.role == "assistant" for part in m.parts
                          if not isinstance(part, b.TextPart)])
        self.assertEqual([d["text"] for d in lines.of("text")], ["ok"])


class ScrubAcrossArguments(unittest.TestCase):
    """DeepSeek, round 3, F6: scrub_across takes sequences of strings and refuses anything else cleanly."""

    def test_one_string_is_refused_not_read_as_its_characters(self) -> None:
        with self.assertRaises(TypeError):
            b.scrub_across("ab", ["xxxxxxxx"])
        with self.assertRaises(TypeError):
            b.scrub_across(["ab"], "xxxxxxxx")

    def test_anything_but_strings_is_refused(self) -> None:
        for texts, secrets in ((["a", 3], ["xxxxxxxx"]), (["a"], [None]), (None, ["xxxxxxxx"])):
            with self.subTest(texts=texts, secrets=secrets), self.assertRaises(TypeError):
                b.scrub_across(texts, secrets)

    def test_a_generator_is_read_once_and_kept(self) -> None:
        texts = (t for t in ["the key: sk-ab", "cdef done"])
        self.assertEqual(b.scrub_across(texts, (s for s in ["sk-abcdef"])), ["the key: [redacted]", " done"])


class InterruptAnswerOrder(unittest.TestCase):
    """DeepSeek, round 3, F4: an interrupt's answer and its turn's end are written under one lock, with the turn
    no longer running once its end is written: delivered comes before the end; after it, not delivered."""

    def test_an_interrupt_read_once_the_turn_has_ended_is_not_delivered(self) -> None:
        session, provider, lines = _session(self, [{"text": "answer"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        real = lines.writer.write

        def write(kind, /, **fields):
            real(kind, **fields)
            if kind == "turn_ended":  # the driver answers the end at once
                engine.feed_line(_interrupt(1))

        lines.writer.write = write
        engine.feed_line(_message(1))
        engine.input_ended()
        engine.serve()
        self.assertEqual([d["delivered"] for d in lines.of("interrupt")], [False])
        self.assertEqual(lines.of("turn_ended")[0]["outcome"], "candidate_done")

    def test_an_interrupt_answered_delivered_comes_before_its_turns_end(self) -> None:
        def reply(request):
            engine.feed_line(_interrupt(1))  # read while the model call runs
            return b.TurnResponse((b.TextPart("answer"),), b.END_TURN, b.Usage())

        session, provider, lines = _session(self, [reply])
        engine = Engine(session, lines.writer, log=lambda m: None)
        engine.feed_line(_message(1))
        engine.input_ended()
        engine.serve()
        types = [d["type"] for d in lines.all()]
        self.assertEqual([d["delivered"] for d in lines.of("interrupt")], [True])
        self.assertLess(types.index("interrupt"), types.index("turn_ended"))
        self.assertEqual(lines.of("turn_ended")[0]["outcome"], "interrupted")


class AstraLaterRules(unittest.TestCase):
    """Four rules the Astra read of round 3 removed without the lane suite noticing (its F16, F22, F35, F38)."""

    def test_an_interrupt_for_a_waiting_turn_is_used_once(self) -> None:
        # F16: turn-2 waits and is interrupted; a second message with the same id, read before the first
        # started, runs as usual once the first has used the interrupt.
        session, provider, lines = _session(self, [{"text": "one"}, {"text": "again"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        engine.feed_line(_message(1))
        engine.feed_line(_message(2))
        engine.feed_line(_interrupt(2))
        engine.feed_line(_message(2, "dlv-3"))
        engine.input_ended()
        engine.serve()
        self.assertEqual([d["outcome"] for d in lines.of("turn_ended")], ["candidate_done", "interrupted",
                                                                          "candidate_done"])
        self.assertEqual(len(provider.requests), 2)

    def test_a_tool_exit_while_its_output_is_processed_is_one_error_result(self) -> None:
        # F22: SystemExit raised after the tool ran (in the registry's output processing) is caught by the
        # outer dispatch: one error result, never the turn.
        ws = workspace(self, {"a.txt": "x\n"})
        reg = registry()
        with patch.object(reg, "_finish", side_effect=SystemExit(5)):
            out = reg.dispatch(b.ToolCall("c1", "read", {"path": "a.txt"}), context(ws))
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("SystemExit", out.model_text)

    def test_the_last_held_fragment_carries_its_own_blocks_index(self) -> None:
        # F35: text held back at the end of the message (it could have begun the key) is sent with the index of
        # the last text block, where it was written.
        seen: list[b.TextDelta] = []
        _read(_wire(_start(), *_text_block(0, "ordinary text"), *_text_block(1, "tail " + SECRET[:6]), *_stop()),
              seen.append)
        self.assertEqual((seen[-1].index, seen[-1].text), (1, SECRET[:6]))

    def test_a_turn_ends_and_the_next_runs_even_when_settling_a_loop_error_fails(self) -> None:
        # F38: a loop error whose settling also fails still writes the turn's end, and the next turn runs.
        session, provider, lines = _session(self, [RuntimeError("provider bug"), {"text": "ok"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        real, calls = session.settle, []

        def settle(reason: str) -> None:
            calls.append(reason)
            if len(calls) == 1:
                raise RuntimeError("settlement bug")
            real(reason)

        engine.feed_line(_message(1))
        engine.feed_line(_message(2))
        engine.input_ended()
        with patch.object(session, "settle", side_effect=settle):
            self.assertEqual(engine.serve(), 0)
        self.assertEqual([d["outcome"] for d in lines.of("turn_ended")], ["failed", "candidate_done"])
        self.assertEqual(len(lines.of("input_accepted")), 2)


if __name__ == "__main__":
    unittest.main()
