"""Boundaries the first reads of draft #379 found the package did not yet hold, kept as tests. Several were
first written by the GPT-6 Astra Codex read (review-1-379) and are extended here:

- no secret the loop holds reaches the model's conversation, a provider's callback or return, or a diagnostic;
- every model call has one usage line, a failed or interrupted call included (what it measured; unknown,
  never 0, where nothing was);
- a malformed input line is refused with a typed error and reading goes on;
- the one output cap holds for what is finally shown, marker included, for every accepted configuration;
- values keep the wire's limits (whole, finite token counts; call ids and tool names within event.v1's);
- search and read agree on line numbers, and a read page is never cut twice.

Only made-up secrets, private temporary folders and in-memory streams are used.
"""

from __future__ import annotations

import io
import json
import math
import unittest
from unittest.mock import patch

from recursion_core.loop import protocol as p
from recursion_core.loop.engine import Config, Engine, Session
from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.providers.fake import FakeProvider, request_to_json
from recursion_core.loop.tests.support import FIXTURES, POSIX, Lines, context, private_dir, registry, workspace
from recursion_core.loop.tests.test_process import run_engine
from recursion_core.loop.tools import read
from recursion_core.loop.tools import registry as r
from recursion_core.loop.tools.workspace import ToolError

SECRET = "synthetic-canary-0000-not-a-real-key-0000"
LS = chr(0x2028)  # LINE SEPARATOR: text inside a line, not a line break
# The secret as JSON may spell it: its first character written as a unicode escape.
ESCAPED = chr(92) + "u" + format(ord(SECRET[0]), "04x") + SECRET[1:]
CR = chr(13)
MSG = b'{"kind":"user_message","turn_id":"turn-1","delivery_id":"dlv-1","text":"go"}'


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read1(self, n: int) -> bytes:
        return self._buf.read(n)


def _events(*docs: dict) -> bytes:
    return "".join("data: " + json.dumps(d) + "\n\n" for d in docs).encode()


def _text_stream(*deltas: str) -> bytes:
    return _events(
        {"type": "message_start", "message": {"model": "synthetic-model", "usage": {"input_tokens": 25}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        *({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}} for t in deltas),
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"})


def _block_stream(block: dict, deltas: tuple = ()) -> bytes:
    return _events(
        {"type": "message_start", "message": {"model": "synthetic-model", "usage": {}}},
        {"type": "content_block_start", "index": 0, "content_block": block},
        *({"type": "content_block_delta", "index": 0, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"})


def _read(data: bytes, on_event=None, cancel: b.CancelToken | None = None) -> b.TurnResponse:
    return a.AnthropicProvider(SECRET)._read_stream(_Bytes(data), "req-test", on_event, cancel or b.CancelToken())


class _Replay:
    """A provider that replays bytes through the real Anthropic stream reader."""

    name = "replay"

    def __init__(self, data: bytes) -> None:
        self.data = data

    def stream_turn(self, request, *, on_event=None, cancel=None):
        return _read(self.data, on_event, cancel)


def _session(case, provider, *, files=None, system=None) -> tuple[Session, Lines]:
    lines = Lines([SECRET])
    session = Session(provider, registry(), workspace(case, files), lines.writer, lines.redactor,
                      Config("synthetic-model", system=system,
                             retry_delays=()))  # one call per failure (PR 3 retries)
    return session, lines


def _serve(session: Session, lines: Lines, *inputs: bytes, log=None) -> list[dict]:
    engine = Engine(session, lines.writer, log=log or (lambda m: None))
    for line in inputs:
        engine.feed_line(line)
    engine.input_ended()
    engine.serve()
    return lines.all()


class StreamedSecrets(unittest.TestCase):
    def test_streamed_text_never_shows_the_key_even_split_across_fragments(self) -> None:
        chunks: list[str] = []
        response = _read(_text_stream("before " + SECRET[:10], SECRET[10:] + " and after"),
                         lambda e: chunks.append(e.text))
        self.assertNotIn(SECRET, "".join(chunks))
        self.assertEqual("".join(chunks), "before [redacted] and after")
        self.assertEqual(response.text, "before [redacted] and after")

    def test_a_held_fragment_that_is_not_the_key_goes_out_with_the_next_one_or_at_the_block_end(self) -> None:
        chunks: list[str] = []
        _read(_text_stream("x" + SECRET[:6], "zzz"), lambda e: chunks.append(e.text))
        self.assertEqual(chunks, ["x", SECRET[:6] + "zzz"])
        chunks.clear()
        _read(_text_stream("tail " + SECRET[:6]), lambda e: chunks.append(e.text))
        self.assertEqual("".join(chunks), "tail " + SECRET[:6])

    def test_returned_text_and_tool_inputs_are_scrubbed(self) -> None:
        response = _read(_text_stream(SECRET))
        self.assertEqual(response.text, "[redacted]")
        call = _read(_block_stream({"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}},
                                   ({"type": "input_json_delta", "partial_json": json.dumps({"path": SECRET})},)))
        self.assertEqual(call.tool_calls[0].input, {"path": "[redacted]"})
        self.assertNotIn(SECRET, call.tool_calls[0].raw_input)

    def test_a_tool_input_spelling_the_key_with_escapes_is_scrubbed_raw_text_included(self) -> None:
        call = _read(_block_stream({"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}},
                                   ({"type": "input_json_delta", "partial_json": '{"path": "' + ESCAPED + '"}'},)))
        self.assertEqual(call.tool_calls[0].input, {"path": "[redacted]"})
        self.assertEqual(json.loads(call.tool_calls[0].raw_input), {"path": "[redacted]"})

    def test_a_thinking_block_holding_the_key_is_refused_not_changed(self) -> None:
        block = {"type": "thinking", "thinking": "", "signature": ""}
        with self.assertRaises(b.ProtocolError):
            _read(_block_stream(block, ({"type": "thinking_delta", "thinking": "the key is " + SECRET},
                                        {"type": "signature_delta", "signature": "sig-1"})))
        kept = _read(_block_stream(block, ({"type": "thinking_delta", "thinking": "plain"},
                                           {"type": "signature_delta", "signature": "sig-1"})))
        self.assertEqual(kept.parts[0], b.OpaquePart("anthropic", {"type": "thinking", "thinking": "plain",
                                                                   "signature": "sig-1"}))


class ConversationSecrets(unittest.TestCase):
    def test_the_system_prompt_and_the_users_text_are_scrubbed(self) -> None:
        provider = FakeProvider([{"text": "ok"}])
        session, _ = _session(self, provider, system="the key is " + SECRET)
        session.run_turn("turn-1", "mine is " + SECRET, b.CancelToken())
        sent = json.dumps(request_to_json(provider.requests[0]))
        self.assertNotIn(SECRET, sent)
        self.assertIn("[redacted]", provider.requests[0].system)

    def test_the_models_text_and_tool_inputs_are_scrubbed_before_they_are_sent_again(self) -> None:
        provider = FakeProvider([
            {"text": "I saw " + SECRET, "tool_calls": [{"id": "toolu_1", "name": "read", "input": {"path": SECRET}}]},
            {"text": "done"}, {"text": "ok"}])
        session, _ = _session(self, provider, files={"a.txt": "x\n"})
        session.run_turn("turn-1", "first", b.CancelToken())
        session.run_turn("turn-2", "second", b.CancelToken())
        for request in provider.requests[1:]:
            self.assertNotIn(SECRET, json.dumps(request_to_json(request)))

    def test_a_tool_input_spelling_a_secret_with_escapes_is_kept_scrubbed_raw_text_included(self) -> None:
        reply = b.TurnResponse((b.ToolCall("toolu_1", "read", {"path": SECRET}, '{"path": "' + ESCAPED + '"}'),),
                               b.TOOL_USE, b.Usage())
        session, _ = _session(self, FakeProvider([reply, {"text": "done"}]))
        session.run_turn("turn-1", "go", b.CancelToken())
        kept = session.messages[1].parts[0]
        self.assertEqual(kept.input, {"path": "[redacted]"})
        self.assertEqual(json.loads(kept.raw_input), {"path": "[redacted]"})

    def test_a_reply_with_the_key_inside_a_vendor_block_is_refused_and_not_kept(self) -> None:
        reply = b.TurnResponse((b.OpaquePart("anthropic", {"type": "thinking", "thinking": SECRET, "signature": "s"}),
                                b.TextPart("hello")), b.END_TURN, b.Usage())
        session, lines = _session(self, FakeProvider([reply]))
        session.run_turn("turn-1", "go", b.CancelToken())
        end = lines.of("turn_ended")[0]
        self.assertEqual((end["outcome"], end["result"]["code"]), ("failed", "provider_protocol"))
        self.assertEqual([m.role for m in session.messages], ["user", "assistant"])
        (note,) = session.messages[1].parts  # only the harness note: nothing of the refused reply
        self.assertTrue(note.text.startswith("[harness] The turn ended before a reply was kept"))
        self.assertEqual(lines.of("text"), [])
        self.assertEqual(len([u for u in lines.of("usage") if u["span"] == "call"]), 1)

    def test_a_loop_error_diagnostic_is_scrubbed(self) -> None:
        def broken(request):
            raise RuntimeError("it failed near " + SECRET)

        session, lines = _session(self, FakeProvider([broken]))
        logs: list[str] = []
        docs = _serve(session, lines, MSG, log=logs.append)
        self.assertTrue(logs)
        self.assertNotIn(SECRET, "\n".join(logs))
        self.assertEqual([d["reason"] for d in docs if d["type"] == "turn_ended"], ["loop_error"])

    def test_a_spill_failure_note_is_scrubbed(self) -> None:
        out = _emit(self, "x" * 3000, max_lines=20, spill_error="could not save near " + SECRET)
        self.assertTrue(out.truncated)
        self.assertNotIn(SECRET, out.model_text)
        self.assertIn("could not be saved", out.model_text)


class FailedCallUsage(unittest.TestCase):
    def test_a_cut_off_stream_reports_what_it_had_measured(self) -> None:
        session, lines = _session(self, _Replay((FIXTURES / "anthropic" / "cut_off.sse").read_bytes()))
        session.run_turn("turn-1", "go", b.CancelToken())
        (usage,) = [u for u in lines.of("usage") if u["span"] == "call"]
        self.assertEqual(usage["model"], "claude-opus-5-5")
        self.assertEqual(usage["input_tokens"], b.observed(30).to_wire())
        self.assertEqual(usage["output_tokens"], b.at_least(1).to_wire())
        self.assertEqual(usage["cost_usd"], b.unknown().to_wire())
        self.assertEqual(lines.of("turn_ended")[0]["result"]["code"], "provider_transport")

    def test_a_call_refused_before_it_streamed_has_one_unknown_usage_line(self) -> None:
        session, lines = _session(self, FakeProvider([b.AuthError("HTTP 401")]))
        session.run_turn("turn-1", "go", b.CancelToken())
        (usage,) = [u for u in lines.of("usage") if u["span"] == "call"]
        self.assertEqual({k: usage[k] for k in b.USAGE_FIELDS}, b.Usage().to_wire())
        self.assertIsNone(usage["model"])

    def test_an_interrupted_stream_carries_what_it_had_measured(self) -> None:
        cancel = b.CancelToken()
        with self.assertRaises(b.Cancelled) as caught:
            _read(_text_stream("one", "two"), lambda e: cancel.cancel(), cancel)
        self.assertEqual(caught.exception.usage.input_tokens, b.observed(25))
        self.assertEqual(caught.exception.usage.output_tokens, b.unknown())
        self.assertEqual(caught.exception.served_model, "synthetic-model")

    def test_the_engine_writes_the_usage_of_an_interrupted_call(self) -> None:
        stopped = b.Cancelled()
        stopped.usage = b.Usage(input_tokens=b.observed(7))
        session, lines = _session(self, FakeProvider([stopped]))
        session.run_turn("turn-1", "go", b.CancelToken())
        calls = [u for u in lines.of("usage") if u["span"] == "call"]
        self.assertEqual([u["input_tokens"]["value"] for u in calls], [7])
        self.assertEqual(lines.of("turn_ended")[0]["outcome"], "interrupted")

    def test_a_provider_bug_still_leaves_one_usage_line(self) -> None:
        session, lines = _session(self, FakeProvider([RuntimeError("a bug")]))
        docs = _serve(session, lines, MSG)
        self.assertEqual([d["type"] for d in docs], ["input_accepted", "usage", "turn_ended", "closed"])

    def test_a_scripted_reply_the_wire_refuses_fails_as_a_protocol_error(self) -> None:
        provider = FakeProvider([{"tool_calls": [{"id": "c" * 129, "name": "read", "input": {"path": "a"}}]}])
        session, lines = _session(self, provider)
        session.run_turn("turn-1", "go", b.CancelToken())
        self.assertEqual(lines.of("turn_ended")[0]["result"]["code"], "provider_protocol")
        self.assertEqual(lines.of("tool_call"), [])


class Inputs(unittest.TestCase):
    def test_a_kind_that_is_not_a_string_is_a_typed_refusal(self) -> None:
        for value in ([], {}, 1, None):
            with self.subTest(value=value), self.assertRaises(p.InputError) as caught:
                p.parse_input(json.dumps({"kind": value}))
            self.assertEqual(caught.exception.code, "unknown_kind")

    def test_reading_goes_on_after_a_bad_kind(self) -> None:
        session, lines = _session(self, FakeProvider([{"text": "ok"}]))
        docs = _serve(session, lines, b'{"kind":[]}', MSG)
        self.assertEqual([d.get("code") for d in docs if d["type"] == "protocol_error"], ["unknown_kind"])
        self.assertEqual(len([d for d in docs if d["type"] == "input_accepted"]), 1)

    def test_a_line_the_parser_did_not_foresee_is_refused_and_reading_goes_on(self) -> None:
        real = p.parse_input
        calls: list[bytes] = []

        def flaky(line):
            calls.append(line)
            if len(calls) == 1:
                raise TypeError("not foreseen")
            return real(line)

        session, lines = _session(self, FakeProvider([{"text": "ok"}]))
        with patch.object(p, "parse_input", side_effect=flaky):
            docs = _serve(session, lines, b'{"kind":"close"}', MSG)
        self.assertEqual([d.get("code") for d in docs if d["type"] == "protocol_error"], ["bad_input"])
        self.assertEqual(len([d for d in docs if d["type"] == "input_accepted"]), 1)

    @unittest.skipUnless(POSIX, "the direct-process tests run on POSIX")
    def test_the_process_recovers_after_a_bad_kind(self) -> None:
        folder = private_dir(self)
        (folder / "fake.json").write_text('[{"text": "ok"}]', encoding="utf-8")
        docs, _, code = run_engine(self, ["--provider", "fake", "--fake-script", str(folder / "fake.json"),
                                          "--home", str(folder / "home")],
                                   [{"kind": []}, json.loads(MSG), {"kind": "close"}], folder)
        self.assertEqual(code, 0)
        self.assertEqual([d["type"] for d in docs],
                         ["ready", "protocol_error", "input_accepted", "usage", "text", "usage", "usage",
                          "turn_ended", "closed"])


def _emit(case, text: str, max_bytes: int = 1201, max_lines: int = 3, spill_error: str | None = None) -> r.Outcome:
    ws = workspace(case)
    spec = b.ToolSpec("synthetic", "test-only output", {"type": "object"})
    reg = r.ToolRegistry([r.Tool(spec, lambda args, ctx: r.ToolOutput(text))], max_bytes=max_bytes,
                         max_lines=max_lines)
    ctx = context(ws, scrub=p.Redactor([SECRET]).scrub)
    if spill_error is None:
        return reg.dispatch(b.ToolCall("c1", "synthetic", {}), ctx)
    with patch.object(ws.spill, "put", side_effect=ToolError(spill_error)):
        return reg.dispatch(b.ToolCall("c1", "synthetic", {}), ctx)


class Cap(unittest.TestCase):
    def test_the_cap_holds_marker_included_when_the_spill_fails(self) -> None:
        for cap in (1201, r.DEFAULT_MAX_BYTES):
            with self.subTest(cap=cap):
                out = _emit(self, "x" * (2 * cap), max_bytes=cap, max_lines=20,
                            spill_error="a long detail\nover lines " * 100)
                self.assertLessEqual(len(out.model_text.encode("utf-8")), cap)
                self.assertLessEqual(r.count_lines(out.model_text), 20)

    def test_the_marker_stays_on_one_line_when_the_spill_error_has_several(self) -> None:
        out = _emit(self, "".join(f"line {i}\n" for i in range(1, 11)), spill_error="first\nsecond\nthird")
        self.assertEqual(r.count_lines(out.model_text), 3)
        self.assertIn("first second third", out.model_text.split("\n")[1])

    def test_the_smallest_line_cap_holds_a_head_line_the_marker_and_a_tail_line(self) -> None:
        out = _emit(self, "".join(f"line {i}\n" for i in range(1, 11)))
        shown = out.model_text.split("\n")
        self.assertEqual(r.count_lines(out.model_text), 3)
        self.assertEqual((shown[0], shown[2]), ("line 1", "line 10"))
        self.assertIn("output truncated", shown[1])

    def test_a_line_cap_below_three_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            r.ToolRegistry([], max_lines=2)
        r.ToolRegistry([], max_lines=3)

    def test_the_cap_holds_even_if_the_marker_outgrew_its_reserve(self) -> None:
        with patch.object(r, "MARKER_RESERVE", 0):
            out = _emit(self, "x" * 5000, max_lines=20)
        self.assertLessEqual(len(out.model_text.encode("utf-8")), 1201)
        self.assertTrue(out.model_text.startswith("[output truncated"))

    def test_lines_are_counted_at_lf_only(self) -> None:
        rows = [f"{i}a{LS}b\n" for i in range(30)]
        out = _emit(self, "".join(rows), max_lines=10)
        self.assertLessEqual(r.count_lines(out.model_text), 10)
        self.assertTrue(out.model_text.startswith(rows[0] + rows[1] + rows[2]), "head lines are whole LF lines")
        self.assertFalse(_emit(self, "".join(rows[:8]), max_lines=10).truncated)


class WireValues(unittest.TestCase):
    def test_measurements_are_finite(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                b.observed(value)

    def test_token_counts_are_whole_numbers_and_dollars_need_not_be(self) -> None:
        for name in b.TOKEN_FIELDS:
            with self.subTest(name=name), self.assertRaises(ValueError):
                b.Usage(**{name: b.observed(1.5)})
        self.assertEqual(b.Usage(cost_usd=b.observed(0.0125)).cost_usd.value, 0.0125)

    def test_call_ids_and_tool_names_keep_event_v1s_limits(self) -> None:
        b.ToolCall("c" * 128, "t" * 256, {})
        for call_id, name in (("c" * 129, "read"), ("call-1", "t" * 257), ("", "read"), ("call-1", ""),
                              ("call" + CR, "read"), ("call-1", "re\nad")):
            with self.subTest(call_id=call_id[:8], name=name[:8]), self.assertRaises(ValueError):
                b.ToolCall(call_id, name, {})

    def test_the_anthropic_reader_refuses_an_id_or_a_name_the_wire_refuses(self) -> None:
        for call_id, name in (("c" * 129, "read"), ("call-1", "t" * 257), ("call" + CR, "read")):
            with self.subTest(call_id=call_id[:8], name=name[:8]), self.assertRaises(b.ProtocolError):
                _read(_block_stream({"type": "tool_use", "id": call_id, "name": name, "input": {}}))


class LineNumbers(unittest.TestCase):
    def _found(self, ws, pattern: str) -> list[int]:
        out = registry().dispatch(b.ToolCall("c1", "search", {"pattern": pattern}), context(ws))
        self.assertEqual(out.status, r.OK)
        return [int(row.split(":", 2)[1]) for row in out.model_text.splitlines() if row.startswith("source.txt:")]

    def test_search_and_read_agree_on_line_numbers(self) -> None:
        for content in ("prefix" + LS + "needle\n", "first" + CR + "\nneedle" + CR + "\n", "a\x0cb\nneedle"):
            with self.subTest(content=content):
                ws = workspace(self, {"source.txt": content})
                (number,) = self._found(ws, "needle")
                page = registry().dispatch(b.ToolCall("c2", "read", {"path": "source.txt", "offset": number,
                                                                     "limit": 1}), context(ws))
                self.assertEqual(page.status, r.OK)
                self.assertIn("needle", page.model_text)

    def test_a_pattern_anchored_at_the_line_end_matches_before_a_crlf(self) -> None:
        ws = workspace(self, {"source.txt": "first" + CR + "\nneedle" + CR + "\n"})
        self.assertEqual(self._found(ws, "needle$"), [2])

    def test_a_final_line_break_is_not_one_more_line(self) -> None:
        ws = workspace(self, {"source.txt": "x\n"})
        self.assertEqual(self._found(ws, "^$"), [])


class ReadPages(unittest.TestCase):
    def test_a_long_line_is_shown_cut_at_its_limit(self) -> None:
        ws = workspace(self, {"long.txt": "z" * 2500 + "\n"})
        out = registry().dispatch(b.ToolCall("c1", "read", {"path": "long.txt"}), context(ws))
        self.assertEqual(out.model_text.count("z"), read.MAX_LINE_CHARS)
        self.assertIn("[line cut: 2500 characters]", out.model_text)

    def test_a_page_is_never_cut_twice_even_under_a_small_byte_cap(self) -> None:
        for content, width in (("a" * 2200 + "\n", 2200), ("\u00e9" * 1500 + "\nsecond\n", 1500)):
            with self.subTest(width=width):
                ws = workspace(self, {"wide.txt": content})
                out = registry(max_bytes=1201).dispatch(b.ToolCall("c1", "read", {"path": "wide.txt"}), context(ws))
                self.assertEqual(out.status, r.OK)
                self.assertFalse(out.truncated)
                self.assertIn(f"[line cut: {width} characters]", out.model_text)
                self.assertLessEqual(len(out.model_text.encode("utf-8")), 1201)


if __name__ == "__main__":
    unittest.main()
