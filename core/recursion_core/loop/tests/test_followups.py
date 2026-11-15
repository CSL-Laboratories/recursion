"""What the round-2 reads of draft #379 found (the GPT-6 Astra confirming read and the DeepSeek break-it read,
review-2-379), kept as tests: a secret split across text parts, or held in a tool call's id or name, or quoted in
a stream diagnostic or a spill note; an interrupt for a turn that waits in the queue; a tool that exits; a
deeply nested input line; the per-event size bound; the conversation after a loop error; and four guards the
Astra read showed the suite did not reach."""

from __future__ import annotations

import io
import json
import threading
import unittest
from unittest.mock import patch

from recursion_core.loop import protocol as p
from recursion_core.loop.engine import Config, Engine, Session
from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers import sse
from recursion_core.loop.providers.fake import FakeProvider, request_to_json
from recursion_core.loop.tests.support import Lines, context, registry, workspace
from recursion_core.loop.tools import registry as r
from recursion_core.loop.tools.workspace import ToolError

SECRET = "synthetic-canary-1111-not-a-real-key-1111"


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read1(self, n: int) -> bytes:
        return self._buf.read(n)


def _wire(*docs: dict) -> bytes:
    return "".join("data: " + json.dumps(d) + "\n\n" for d in docs).encode()


START = {"type": "message_start", "message": {"model": "synthetic-model", "usage": {"input_tokens": 3}}}
END = ({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
       {"type": "message_stop"})


def _text_block(index: int, text: str) -> tuple[dict, ...]:
    return ({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": index})


def _read(data: bytes, on_event=None) -> b.TurnResponse:
    return a.AnthropicProvider(SECRET)._read_stream(_Bytes(data), "req-test", on_event, b.CancelToken())


def _session(case, steps) -> tuple[Session, FakeProvider, Lines]:
    provider = FakeProvider(steps)
    lines = Lines([SECRET])
    session = Session(provider, registry(), workspace(case, {"a.txt": "x\n"}), lines.writer, lines.redactor,
                      Config("fake-model"))
    return session, provider, lines


class ScrubAcross(unittest.TestCase):
    def test_a_secret_split_across_texts_becomes_one_mark_where_it_begins(self) -> None:
        self.assertEqual(b.scrub_across(["the key: sk-ab", "cdef done"], ["sk-abcdef"]),
                         ["the key: [redacted]", " done"])
        self.assertEqual(b.scrub_across(["x sk-a", "bcd", "ef y"], ["sk-abcdef"]), ["x [redacted]", "", " y"])

    def test_overlapping_and_repeated_secrets(self) -> None:
        self.assertEqual(b.scrub_across(["aaaa-bbbb-cccc"], ["aaaa-bbbb", "bbbb-cccc"]), ["[redacted]"])
        self.assertEqual(b.scrub_across(["k1k1k1k1 and k1k1k1k1"], ["k1k1k1k1"]), ["[redacted] and [redacted]"])

    def test_texts_without_a_secret_come_back_unchanged(self) -> None:
        texts = ["one", "", "two"]
        self.assertEqual(b.scrub_across(texts, ["absent-secret"]), texts)


class SplitTextBlocks(unittest.TestCase):
    def test_a_key_split_across_two_text_blocks_is_caught_in_the_reply_and_the_callbacks(self) -> None:
        cut = 17
        for between in ((), ({"type": "content_block_start", "index": 1,
                              "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}},
                             {"type": "content_block_stop", "index": 1})):
            with self.subTest(tool_between=bool(between)):
                last = 2 if between else 1
                chunks: list[str] = []
                response = _read(_wire(START, *_text_block(0, "the key: " + SECRET[:cut]), *between,
                                       *_text_block(last, SECRET[cut:] + " done"), *END),
                                 lambda e: chunks.append(e.text))
                self.assertNotIn(SECRET, "".join(chunks))
                self.assertNotIn(SECRET, response.text)
                self.assertEqual(response.text, "the key: [redacted] done")

    def test_a_held_fragment_that_is_not_the_key_still_goes_out_at_the_end(self) -> None:
        chunks: list[str] = []
        _read(_wire(START, *_text_block(0, "tail " + SECRET[:6]), *END), lambda e: chunks.append(e.text))
        self.assertEqual("".join(chunks), "tail " + SECRET[:6])

    def test_the_engine_scrubs_the_text_parts_of_a_reply_as_one_text(self) -> None:
        reply = b.TurnResponse((b.TextPart("the key: " + SECRET[:9]), b.TextPart(SECRET[9:] + " done")), b.END_TURN,
                               b.Usage())
        session, provider, lines = _session(self, [reply, {"text": "ok"}])
        session.run_turn("turn-1", "first", b.CancelToken())
        session.run_turn("turn-2", "second", b.CancelToken())
        self.assertNotIn(SECRET, json.dumps(request_to_json(provider.requests[1])))
        self.assertEqual("".join(d["text"] for d in lines.of("text")[:2]), "the key: [redacted] done")


class ToolIdentifiers(unittest.TestCase):
    def test_the_client_refuses_a_key_in_a_call_id_or_a_tool_name(self) -> None:
        for call_id, name in ((SECRET, "read"), ("toolu_1", SECRET)):
            with self.subTest(in_name=name == SECRET), self.assertRaises(b.ProtocolError) as caught:
                _read(_wire(START, {"type": "content_block_start", "index": 0, "content_block": {
                    "type": "tool_use", "id": call_id, "name": name, "input": {}}},
                    {"type": "content_block_stop", "index": 0}, *END))
            self.assertNotIn(SECRET, str(caught.exception))

    def test_the_engine_refuses_a_reply_whose_call_id_or_tool_name_holds_a_secret(self) -> None:
        for call in (b.ToolCall(SECRET, "read", {"path": "a.txt"}), b.ToolCall("toolu_1", SECRET, {})):
            with self.subTest(call_id=call.call_id[:8]):
                session, provider, lines = _session(self, [b.TurnResponse((call,), b.TOOL_USE, b.Usage()),
                                                           {"text": "ok"}])
                session.run_turn("turn-1", "go", b.CancelToken())
                self.assertEqual(lines.of("turn_ended")[0]["result"]["code"], "provider_protocol")
                self.assertEqual(lines.of("tool_call"), [])
                self.assertEqual([m.role for m in session.messages], ["user", "assistant"])
                self.assertNotIn(SECRET, repr(session.messages))


class Diagnostics(unittest.TestCase):
    def test_a_stream_diagnostic_never_quotes_the_key(self) -> None:
        streams = (("event: " + SECRET + "\ndata: not json\n\n").encode(),
                   _wire({"type": SECRET}),
                   _wire(START, {"type": "content_block_start", "index": 0, "content_block": {"type": SECRET}},
                         {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": SECRET}},
                         {"type": "content_block_stop", "index": 0}, *END))
        for data in streams:
            with self.subTest(data=data[:20]), self.assertRaises(b.ProtocolError) as caught:
                _read(data)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn(SECRET, caught.exception.detail)

    def test_a_marker_in_a_spill_failure_note_is_quoted(self) -> None:
        ws = workspace(self)
        reg = r.ToolRegistry([r.Tool(b.ToolSpec("synthetic", "", {"type": "object"}),
                                     lambda args, ctx: r.ToolOutput("row\n" * 5000))])
        with patch.object(ws.spill, "put", side_effect=ToolError("could not save " + r.MARKER_OPEN + " here")):
            out = reg.dispatch(b.ToolCall("c1", "synthetic", {}), context(ws))
        self.assertEqual(out.model_text.count(r.MARKER_OPEN), 1)
        self.assertIn("could not save " + r.QUOTED_OPEN, out.model_text)


class QueuedInterrupt(unittest.TestCase):
    def test_an_interrupt_for_a_waiting_turn_stops_it_before_any_model_call(self) -> None:
        gate = threading.Event()

        def slow(request):
            gate.wait(10)
            return b.TurnResponse((b.TextPart("first answer"),), b.END_TURN, b.Usage())

        session, provider, lines = _session(self, [slow, {"text": "never asked"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        thread = threading.Thread(target=engine.serve)
        thread.start()
        self.addCleanup(lambda: (gate.set(), thread.is_alive() and engine.input_ended(), thread.join(10)))
        engine.feed_line(b'{"kind":"user_message","turn_id":"turn-1","delivery_id":"dlv-1","text":"one"}')
        provider.calls_started.wait(10)
        engine.feed_line(b'{"kind":"user_message","turn_id":"turn-2","delivery_id":"dlv-2","text":"two"}')
        engine.feed_line(b'{"kind":"interrupt","turn_id":"turn-2"}')
        engine.feed_line(b'{"kind":"interrupt","turn_id":"turn-9"}')
        gate.set()
        engine.feed_line(b'{"kind":"close"}')
        thread.join(10)
        self.assertEqual([(d["turn_id"], d["delivered"]) for d in lines.of("interrupt")],
                         [("turn-2", True), ("turn-9", False)])
        self.assertEqual([(d["turn_id"], d["outcome"]) for d in lines.of("turn_ended")],
                         [("turn-1", "candidate_done"), ("turn-2", "interrupted")])
        self.assertEqual([d["turn_id"] for d in lines.of("input_accepted")], ["turn-1", "turn-2"])
        self.assertEqual(len(provider.requests), 1, "the interrupted turn made no model call")

    def test_an_interrupt_for_a_turn_that_already_ended_is_not_delivered(self) -> None:
        session, _, lines = _session(self, [{"text": "ok"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        engine.feed_line(b'{"kind":"user_message","turn_id":"turn-1","delivery_id":"dlv-1","text":"one"}')
        engine.input_ended()
        engine.serve()
        engine.feed_line(b'{"kind":"interrupt","turn_id":"turn-1"}')
        self.assertEqual(lines.of("interrupt")[-1]["delivered"], False)


class ExitingTools(unittest.TestCase):
    def _registry(self, error: BaseException) -> r.ToolRegistry:
        def run(args, ctx):
            raise error

        return r.ToolRegistry([r.Tool(b.ToolSpec("synthetic", "", {"type": "object"}), run)])

    def test_a_tool_that_exits_costs_one_error_result(self) -> None:
        out = self._registry(SystemExit(3)).dispatch(b.ToolCall("c1", "synthetic", {}), context(workspace(self)))
        self.assertEqual(out.status, r.ERROR)
        self.assertIn("the tool failed: SystemExit", out.model_text)

    def test_a_keyboard_interrupt_still_stops_the_process(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._registry(KeyboardInterrupt()).dispatch(b.ToolCall("c1", "synthetic", {}), context(workspace(self)))


class Inputs(unittest.TestCase):
    def test_a_line_nested_deeper_than_the_decoder_goes_is_not_json(self) -> None:
        line = '{"kind":"user_message","turn_id":"turn-1","text":' + "[" * 200000 + "]" * 200000 + "}"
        with self.assertRaises(p.InputError) as caught:
            p.parse_input(line)
        self.assertEqual(caught.exception.code, "not_json")

    def test_one_event_built_from_many_data_lines_has_a_size_bound(self) -> None:
        body = b"".join(b"data: " + b"x" * 30 + b"\n" for _ in range(10))
        with patch.object(sse, "MAX_EVENT_CHARS", 100), self.assertRaises(sse.SSEError) as caught:
            list(sse.iter_events(io.BytesIO(body).read))
        self.assertIn("one event is larger", str(caught.exception))


class AfterALoopError(unittest.TestCase):
    def test_a_loop_error_leaves_a_harness_note(self) -> None:
        session, _, lines = _session(self, [RuntimeError("a bug")])
        engine = Engine(session, lines.writer, log=lambda m: None)
        engine.feed_line(b'{"kind":"user_message","turn_id":"turn-1","delivery_id":"dlv-1","text":"one"}')
        engine.input_ended()
        engine.serve()
        self.assertEqual([m.role for m in session.messages], ["user", "assistant"])
        self.assertEqual(session.messages[1].parts[0].text,
                         "[harness] The turn ended before a reply was kept: the loop failed.")

    def test_a_loop_error_between_a_reply_and_its_tools_leaves_every_call_answered(self) -> None:
        reply = {"tool_calls": [{"id": "toolu_1", "name": "read", "input": {"path": "a.txt"}}]}
        session, provider, lines = _session(self, [reply, {"text": "ok"}])
        engine = Engine(session, lines.writer, log=lambda m: None)
        with patch.object(session.registry, "dispatch", side_effect=RuntimeError("a bug")):
            engine.feed_line(b'{"kind":"user_message","turn_id":"turn-1","delivery_id":"dlv-1","text":"one"}')
            engine.feed_line(b'{"kind":"user_message","turn_id":"turn-2","delivery_id":"dlv-2","text":"two"}')
            engine.input_ended()
            engine.serve()
        last = provider.requests[-1]
        self.assertEqual([m.role for m in last.messages], ["user", "assistant", "user", "assistant", "user"])
        (result,) = last.messages[2].parts
        self.assertEqual((result.call_id, result.is_error), ("toolu_1", True))
        self.assertEqual([d["reason"] for d in lines.of("turn_ended")], ["loop_error", "end_turn"])


class GuardsTheAstraReadAdded(unittest.TestCase):
    def test_a_usage_field_must_be_a_measurement(self) -> None:
        with self.assertRaises(ValueError):
            b.Usage(input_tokens=object())

    def test_a_call_id_and_a_tool_name_must_be_text(self) -> None:
        for call_id, name in ((["x"], "read"), ("c1", ["read"]), (7, "read"), ("c1", None)):
            with self.subTest(call_id=call_id, name=name), self.assertRaises(ValueError):
                b.ToolCall(call_id, name, {})

    def test_a_tool_input_given_whole_in_the_block_start_is_scrubbed(self) -> None:
        response = _read(_wire(START, {"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": SECRET}}},
            {"type": "content_block_stop", "index": 0}, *END))
        self.assertEqual(response.tool_calls[0].input, {"path": "[redacted]"})


if __name__ == "__main__":
    unittest.main()
