"""What the round-6 reads of draft #379 found (the GPT-6 Astra and GPT-6.1 Sol confirming reads and the DeepSeek
break-it read, review-6-379), kept as tests: a tool input refused for a number JSON cannot write keeps no raw
text; an explicit null input is not an empty argument list; everything a block starts with counts toward the
reply's bound; a number too large for a float is refused where the vendor's JSON is read; a limit line is written
whatever retry time a provider gives; text that is not valid Unicode never reaches a line or a request; and the
registry's and the writer's refusals."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from recursion_core.loop import protocol as p
from recursion_core.loop.engine import Engine
from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import Canned, Lines, Loopback
from recursion_core.loop.tests.test_edges import (SECRET, _message, _read, _session, _start, _stop, _text_block,
                                                  _tool_block, _wire)
from recursion_core.loop.tools import registry as r

BACKSLASH = chr(92)
LONE = chr(0xD800)  # a lone surrogate, as Python's decoder gives it for the JSON escape below
LONE_ESCAPE = BACKSLASH + "ud800"


def _escaped(text: str) -> str:
    """``text`` spelt entirely with JSON escapes, so a text scrub cannot see it."""

    return "".join(BACKSLASH + "u%04x" % ord(ch) for ch in text)


def _strict(line: str):
    def refuse(name: str):
        raise ValueError(name)

    return json.loads(line, parse_constant=refuse)


def _initial(index: int, block: dict) -> tuple[dict, ...]:
    return ({"type": "content_block_start", "index": index, "content_block": block},
            {"type": "content_block_stop", "index": index})


def _state() -> tuple[a._StreamState, a.AnthropicProvider]:
    state, provider = a._StreamState("req-test"), a.AnthropicProvider(SECRET)
    state.feed(_start(), None, provider)
    return state, provider


def _inert(name: str = "inspect", needs_approval: bool = False):
    ran: list = []
    tool = r.Tool(b.ToolSpec(name, "an inert test tool", {"type": "object", "properties": {},
                                                            "additionalProperties": False}),
                  lambda args, ctx: ran.append(args) or r.ToolOutput("ran"), needs_approval)
    return tool, ran


class NonFiniteToolInput(unittest.TestCase):
    """Astra, round 6, change 1, and Sol, round 6, change 2: a tool input the client refuses for NaN, an infinity
    or a number too large for a float keeps no raw text (its strings were never decoded and scrubbed) and gives
    the engine's reason, INPUT_NOT_FINITE; so does input the decoder refuses for anything but its syntax."""

    def test_each_such_number_is_refused_with_no_raw_text_and_the_shared_reason(self) -> None:
        for number in ("NaN", "Infinity", "-Infinity", "1e400", "-1e400"):
            with self.subTest(number=number):
                call = _read(_wire(_start(), *_tool_block(0, "toolu_1", '{"path": "a.txt", "n": ' + number + '}'),
                                   *_stop("tool_use"))).tool_calls[0]
                self.assertEqual((call.input, call.raw_input, call.input_error), (None, "", b.INPUT_NOT_FINITE))

    def test_an_integer_too_long_for_python_is_refused_the_same_way(self) -> None:
        # The decoder's one other refusal that is not about syntax (DeepSeek, round 6, finding 3's last case).
        call = _read(_wire(_start(), *_tool_block(0, "toolu_1", '{"n": ' + "9" * 5000 + '}'),
                           *_stop("tool_use"))).tool_calls[0]
        self.assertEqual((call.input, call.raw_input, call.input_error), (None, "", b.INPUT_NOT_FINITE))

    def test_a_syntax_error_still_hands_its_text_back(self) -> None:
        call = _read(_wire(_start(), *_tool_block(0, "toolu_1", '{"path": '), *_stop("tool_use"))).tool_calls[0]
        self.assertEqual((call.input, call.raw_input), (None, '{"path": '))
        self.assertTrue(call.input_error.startswith("input is not valid JSON: "))

    def test_the_model_sees_the_reason_and_no_escaped_secret_in_the_next_request(self) -> None:
        for number in ("NaN", "Infinity", "-Infinity", "1e400"):
            with self.subTest(number=number):
                raw = '{"payload": {"value": "' + _escaped(SECRET) + '"}, "n": ' + number + '}'
                data = _wire(_start(), *_tool_block(0, "toolu_1", raw), *_stop("tool_use"))
                done = _wire(_start(), *_text_block(0, "done"), *_stop())
                headers = {"content-type": "text/event-stream"}
                local = Loopback(self, [Canned(200, data, headers), Canned(200, done, headers)])
                session, unused, lines = _session(self, [])
                session.provider = a.AnthropicProvider(SECRET, base_url=local.url)
                tool, ran = _inert("read", needs_approval=True)
                session.registry = r.ToolRegistry([tool])
                session.run_turn("turn-1", "go", b.CancelToken())
                self.assertEqual(ran, [])
                answer = json.loads(local.requests[1]["body"]["messages"][2]["content"][0]["content"])
                self.assertEqual(answer, {"INVALID_JSON": "", "error": f"{b.INPUT_NOT_FINITE}; the tool did not run"})
                self.assertEqual(session.messages[1].parts[0].raw_input, "")


class ExplicitNullInput(unittest.TestCase):
    """Astra, round 6, change 2, and DeepSeek, round 6, finding 4: ``"input": null`` on a tool_use start is input
    that is not an object, never an empty argument list; an absent input is still the empty object."""

    def test_null_is_refused_and_absent_is_the_empty_object(self) -> None:
        state, provider = _state()
        for index, block in enumerate(({"type": "tool_use", "id": "toolu_1", "name": "read", "input": None},
                                       {"type": "tool_use", "id": "toolu_2", "name": "read"})):
            state.feed({"type": "content_block_start", "index": index, "content_block": block}, None, provider)
            state.feed({"type": "content_block_stop", "index": index}, None, provider)
        null, absent = state.done[0], state.done[1]
        self.assertEqual((null.input, null.raw_input, null.input_error), (None, "", "input is not a JSON object"))
        self.assertEqual((absent.input, absent.input_error), ({}, None))

    def test_a_tool_that_takes_no_arguments_does_not_run_on_null(self) -> None:
        call = _read(_wire(_start(), *_initial(0, {"type": "tool_use", "id": "toolu_1", "name": "inspect",
                                                   "input": None}), *_stop("tool_use"))).tool_calls[0]
        tool, ran = _inert()
        session, provider, lines = _session(self, [b.TurnResponse((call,), b.TOOL_USE, b.Usage()), {"text": "done"}])
        session.registry = r.ToolRegistry([tool])
        session.run_turn("turn-1", "go", b.CancelToken())
        self.assertEqual(ran, [])
        self.assertEqual(lines.of("tool_result")[0]["status"], r.ERROR)


class EverythingAStartCarriesCounts(unittest.TestCase):
    """Sol, round 6, change 1 (and Astra's B07 and Sol's B04, later points): every value a kept block starts with
    and every field name it is sent back with count toward MAX_REPLY_CHARS, the type, id and name of a vendor
    block included; only the type and field names of the four block kinds the client reads, and a tool call's
    id and name (checked against their own bounds first), are left out. A reply at the bound is still taken."""

    def test_a_vendor_blocks_type_id_name_and_field_names_count(self) -> None:
        cases = {
            "unknown type": {"type": "vendor-" + "x" * 11, "data": ""},
            "id": {"type": "redacted_thinking", "data": "", "id": "x" * 11},
            "name": {"type": "redacted_thinking", "data": "", "name": "x" * 11},
            "field name": {"type": "redacted_thinking", "data": "", "k" * 11: ""},
            "number": {"type": "redacted_thinking", "data": "", "n": 10 ** 11},
        }
        with patch.object(a, "MAX_REPLY_CHARS", 20):
            for name, block in cases.items():
                with self.subTest(name=name), self.assertRaises(b.ProtocolError):
                    state, provider = _state()
                    for index in range(2):
                        state.feed({"type": "content_block_start", "index": index, "content_block": dict(block)},
                                   None, provider)
                with self.subTest(name=name, one="field name of 21"), self.assertRaises(b.ProtocolError):
                    _read(_wire(_start(), *_initial(0, {"type": "redacted_thinking", "data": "", "k" * 21: ""}),
                                *_stop()))

    def test_list_and_object_values_count(self) -> None:
        with patch.object(a, "MAX_REPLY_CHARS", 20):
            for data in (["x" * 21], {"items": ["x" * 21]}):
                with self.subTest(kind=type(data).__name__), self.assertRaises(b.ProtocolError):
                    state, provider = _state()
                    state.feed({"type": "content_block_start", "index": 0,
                                "content_block": {"type": "redacted_thinking", "data": data}}, None, provider)
                self.assertEqual(state.blocks, {})  # refused before it was kept

    def test_the_kinds_the_client_reads_count_only_their_content_and_the_bound_is_taken(self) -> None:
        blocks = [{"type": "text", "text": "abc"}, {"type": "thinking", "thinking": "de", "signature": "fg"},
                  {"type": "redacted_thinking", "data": "hij"},
                  {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "k"}}]
        size = 10 + len(json.dumps({"path": "k"}, ensure_ascii=False))
        events = [event for index, block in enumerate(blocks) for event in _initial(index, block)]
        with patch.object(a, "MAX_REPLY_CHARS", size):
            self.assertEqual(len(_read(_wire(_start(), *events, *_stop("tool_use"))).parts), 4)
        with patch.object(a, "MAX_REPLY_CHARS", size - 1), self.assertRaises(b.ProtocolError):
            _read(_wire(_start(), *events, *_stop("tool_use")))

    def test_a_tool_calls_id_and_name_are_checked_before_the_block_is_kept(self) -> None:
        for block in ({"type": "tool_use", "id": "x" * (b.MAX_CALL_ID + 1), "name": "read", "input": {}},
                      {"type": "tool_use", "id": "toolu_1", "name": "n" * (b.MAX_TOOL_NAME + 1), "input": {}},
                      {"type": "tool_use", "name": "read", "input": {}}):
            with self.subTest(block=sorted(block)), self.assertRaises(b.ProtocolError):
                state, provider = _state()
                state.feed({"type": "content_block_start", "index": 0, "content_block": block}, None, provider)
            self.assertEqual(state.blocks, {})


class NumbersTooLargeForAFloat(unittest.TestCase):
    """DeepSeek, round 6, finding 1 (and Sol's later point on 1e400): a number too large for a float is an
    infinity Python's decoder makes without asking parse_constant; it is refused where the vendor's JSON is read,
    so a kept vendor block never sends ``Infinity`` back. The HTTP error body is read the same strict way
    (Astra's N03 and Sol's N04, later points)."""

    def test_a_vendor_block_holding_one_is_a_protocol_error_that_keeps_the_usage(self) -> None:
        for number in ("1e400", "-1e400", "1e309"):
            with self.subTest(number=number), self.assertRaises(b.ProtocolError) as caught:
                _read(_wire(_start(), '{"type": "content_block_start", "index": 0, "content_block": '
                                      '{"type": "redacted_thinking", "data": "abc", "n": ' + number + '}}',
                            {"type": "content_block_stop", "index": 0}, *_stop()))
            self.assertEqual(caught.exception.usage.input_tokens.value, 7)

    def test_an_ordinary_number_is_kept_as_it_was(self) -> None:
        reply = _read(_wire(_start(), *_initial(0, {"type": "redacted_thinking", "data": "abc", "n": 1.5e300}),
                            *_stop()))
        self.assertEqual(reply.parts[0].data["n"], 1.5e300)

    def test_an_http_error_body_holding_such_a_number_gives_no_vendor_text(self) -> None:
        class Response:
            status = 400

            def __init__(self, body: bytes) -> None:
                self.body = body

            def read(self, n: int) -> bytes:
                return self.body

            def getheader(self, name: str, default=None):
                return default

        for number in (b"NaN", b"Infinity", b"1e400"):
            with self.subTest(number=number):
                body = b'{"error": {"type": "rate_limit_error", "message": "synthetic"}, "n": ' + number + b'}'
                error = a.AnthropicProvider(SECRET)._http_error(Response(body), "req-test")
                self.assertEqual((type(error), error.detail, error.vendor_type), (b.RequestError, "HTTP 400", None))


class LimitLineIsAlwaysWritten(unittest.TestCase):
    """DeepSeek, round 6, finding 2: a provider's LimitError whose retry time is NaN or an infinity (or whose
    reset time is not text) still gives a ``limit`` line, with that field null, and the turn pauses; it used to
    end ``loop_error`` with no limit line once the writer refused NaN."""

    def test_a_retry_time_json_cannot_write_is_written_as_null(self) -> None:
        for retry in (float("inf"), float("nan"), float("-inf"), "30", True):
            with self.subTest(retry=repr(retry)):
                session, provider, lines = _session(self, [b.LimitError("synthetic limit", retry_after_s=retry,
                                                                        reset_at=float("inf"))])
                self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "paused")
                for line in lines.raw().splitlines():
                    _strict(line)
                limit = lines.of("limit")[0]
                self.assertEqual((limit["retry_after_s"], limit["reset_at"]), (None, None))
                self.assertEqual(lines.of("turn_ended")[0]["reason"], "limit")

    def test_a_finite_retry_time_is_kept(self) -> None:
        session, provider, lines = _session(self, [b.LimitError("synthetic limit", retry_after_s=30.5,
                                                                reset_at="2026-11-15T09:00:00.000Z")])
        session.run_turn("turn-1", "go", b.CancelToken())
        limit = lines.of("limit")[0]
        self.assertEqual((limit["retry_after_s"], limit["reset_at"]), (30.5, "2026-11-15T09:00:00.000Z"))


class TextThatIsNotUnicode(unittest.TestCase):
    """DeepSeek, round 6, finding 3: a lone surrogate (JSON's ``\\ud800`` escape) in a reply's text, a tool
    call's input or a vendor block can be neither written on a line nor sent in the next request. The reply is
    refused (``provider_protocol``, the turn fails typed), never a loop error, and no tool runs."""

    def test_a_reply_holding_one_anywhere_is_refused_typed(self) -> None:
        tool, ran = _inert("read")
        replies = {
            "text": b.TurnResponse((b.TextPart("before " + LONE),), b.END_TURN, b.Usage()),
            "tool input": b.TurnResponse((b.ToolCall("toolu_1", "read", {"path": LONE}),), b.TOOL_USE, b.Usage()),
            "tool input key": b.TurnResponse((b.ToolCall("toolu_1", "read", {LONE: "a"}),), b.TOOL_USE, b.Usage()),
            "raw input": b.TurnResponse((b.ToolCall("toolu_1", "read", None, "{" + LONE, "bad"),), b.TOOL_USE,
                                        b.Usage()),
            "vendor block": b.TurnResponse((b.OpaquePart("anthropic", {"type": "redacted_thinking", "data": LONE}),),
                                           b.END_TURN, b.Usage()),
        }
        for name, reply in replies.items():
            with self.subTest(part=name):
                session, provider, lines = _session(self, [reply])
                session.registry = r.ToolRegistry([tool])
                self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "failed")
                ended = lines.of("turn_ended")[0]
                self.assertEqual((ended["result"]["code"], ended["reason"]), ("provider_protocol", "provider_error"))
                self.assertEqual((lines.of("text"), lines.of("tool_call"), ran), ([], [], []))
                lines.raw().encode("utf-8")
                for message in session.messages:
                    for part in message.parts:
                        self.assertFalse(b.holds_unencodable([getattr(part, "text", ""), getattr(part, "data", "")]))

    def test_from_the_vendors_stream_too(self) -> None:
        wire = _wire(_start(), *_text_block(0, "x"), *_stop())
        reply = _read(wire.replace(b'"x"', b'"' + LONE_ESCAPE.encode() + b'"'))
        self.assertEqual(reply.text, LONE)
        session, provider, lines = _session(self, [reply])
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "failed")
        self.assertEqual(lines.of("turn_ended")[0]["result"]["code"], "provider_protocol")

    def test_values_json_cannot_write_from_a_provider_that_sidesteps_the_decoder(self) -> None:
        # The same class as finding 3 (DeepSeek's "huge integer" case and its round-5 F3c): a provider that builds
        # its parts itself. A tool input becomes input that did not parse; a vendor block refuses the reply.
        tool, ran = _inert("read")
        call = b.ToolCall("toolu_1", "read", {"n": 10 ** 5000}, "raw")
        session, provider, lines = _session(self, [b.TurnResponse((call,), b.TOOL_USE, b.Usage()), {"text": "done"}])
        session.registry = r.ToolRegistry([tool])
        self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "candidate_done")
        self.assertEqual((lines.of("tool_call")[0]["input"], ran), (None, []))
        self.assertEqual(json.loads(lines.of("tool_result")[0]["text"]),
                         {"INVALID_JSON": "", "error": f"{b.INPUT_NOT_FINITE}; the tool did not run"})
        for value in (float("nan"), float("inf"), 10 ** 5000, {1, 2}):
            with self.subTest(value=type(value).__name__):
                block = b.OpaquePart("anthropic", {"type": "redacted_thinking", "data": "x", "n": value})
                session, provider, lines = _session(self, [b.TurnResponse((block,), b.END_TURN, b.Usage())])
                self.assertEqual(session.run_turn("turn-1", "go", b.CancelToken()), "failed")
                self.assertEqual(lines.of("turn_ended")[0]["result"]["code"], "provider_protocol")
                for line in lines.raw().splitlines():
                    _strict(line)

    def test_holds_unencodable(self) -> None:
        self.assertTrue(b.holds_unencodable({"a": [1, {"b": LONE}]}))
        self.assertTrue(b.holds_unencodable({LONE: 1}))
        self.assertFalse(b.holds_unencodable({"a": ["caf" + chr(0xE9), "\U0001f680", 5, None]}))


class RegistryRefusals(unittest.TestCase):
    """DeepSeek, round 6, findings 5 to 7 (the strict tool permission path): ``needs_approval`` is True or False
    and nothing else; ``dispatch`` answers anything that is not a tool call with an error result; ``get`` gives a
    tool's spec, never its ``run``."""

    def test_needs_approval_must_be_a_bool(self) -> None:
        spec = b.ToolSpec("w", "a write tool", {"type": "object"})
        for flag in (0, 1, "", "yes", None, [], 0.0):
            with self.subTest(flag=repr(flag)), self.assertRaises(ValueError):
                r.Tool(spec, lambda args, ctx: r.ToolOutput("ran"), flag)
        self.assertTrue(r.Tool(spec, lambda args, ctx: r.ToolOutput("ran"), True).needs_approval)

    def test_a_registry_holds_tools_only(self) -> None:
        class Lookalike:
            spec = b.ToolSpec("w", "a write tool", {"type": "object"})
            needs_approval = 0

            @staticmethod
            def run(args, ctx):
                return r.ToolOutput("ran")

        with self.assertRaises(ValueError):
            r.ToolRegistry([Lookalike()])

    def test_dispatch_answers_what_is_not_a_tool_call(self) -> None:
        tool, ran = _inert("read")
        registry = r.ToolRegistry([tool])
        session, provider, lines = _session(self, [])
        ctx = r.ToolContext(session.workspace, b.CancelToken(), lambda s: s)
        for call in (None, "read", {"call_id": "toolu_1", "name": "read", "input": {}}, 5):
            with self.subTest(call=repr(call)):
                outcome = registry.dispatch(call, ctx)
                self.assertEqual((outcome.status, outcome.call_id, outcome.name), (r.ERROR, "", ""))
                self.assertIn("is not a tool call", outcome.model_text)
        self.assertEqual(ran, [])

    def test_get_gives_the_spec_never_the_run(self) -> None:
        tool, ran = _inert("inspect", needs_approval=True)
        registry = r.ToolRegistry([tool])
        self.assertEqual(registry.get("inspect"), tool.spec)
        self.assertFalse(hasattr(registry.get("inspect"), "run"))
        self.assertIsNone(registry.get("nope"))
        self.assertIsNone(registry.get(None))
        self.assertIsNone(registry.get(["inspect"]))


class WriterRefusals(unittest.TestCase):
    """DeepSeek, round 6, finding 10: a line's type is text, and so is the type a writer seals after."""

    def test_a_line_type_that_is_not_text_is_refused_and_nothing_is_written(self) -> None:
        lines = Lines()
        for line_type in (5, "", None, b"text"):
            with self.subTest(line_type=repr(line_type)), self.assertRaises(ValueError):
                lines.writer.write(line_type, turn_id="turn-1")
        self.assertEqual(lines.raw(), "")

    def test_seal_after_is_a_line_type_or_none(self) -> None:
        lines = Lines()
        for seal in (5, "", b"closed", ["closed"]):
            with self.subTest(seal=repr(seal)), self.assertRaises(ValueError):
                p.Writer(lines.buffer, lines.redactor, seal_after=seal)
        p.Writer(lines.buffer, lines.redactor, seal_after=None)
        p.Writer(lines.buffer, lines.redactor, seal_after="closed")


class EngineThroughTheProcessShape(unittest.TestCase):
    """The answers above through an Engine, as the process drives it: every line strict JSON and UTF-8."""

    def test_an_engine_run_with_each_answered_case_writes_only_strict_lines(self) -> None:
        replies = [b.LimitError("synthetic limit", retry_after_s=float("nan")),
                   b.TurnResponse((b.TextPart(LONE),), b.END_TURN, b.Usage()),
                   {"text": "done"}]
        session, provider, lines = _session(self, replies)
        engine = Engine(session, lines.writer, log=lambda m: None)
        for n in (1, 2, 3):
            engine.feed_line(_message(n))
        engine.input_ended()
        self.assertEqual(engine.serve(), 0)
        raw = lines.raw()
        raw.encode("utf-8")
        for line in raw.splitlines():
            _strict(line)
        self.assertEqual([d["outcome"] for d in lines.of("turn_ended")], ["paused", "failed", "candidate_done"])


if __name__ == "__main__":
    unittest.main()
