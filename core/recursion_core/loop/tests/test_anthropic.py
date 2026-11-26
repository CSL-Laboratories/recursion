"""The Anthropic client against recorded streams served by a loopback HTTP server. No live call, no real key.

The streams in fixtures/anthropic/ are synthesized from the documented wire format (they say so in their first
lines); they are cut into 37-byte HTTP chunks so lines, events and UTF-8 characters arrive split.
"""

from __future__ import annotations

import json
import threading
import unittest

from recursion_core.loop.providers import anthropic as a
from recursion_core.loop.providers import base as b
from recursion_core.loop.tests.support import FAKE_KEY, FIXTURES, Canned, Loopback, sse

CLOCK = 1_763_100_000.0  # a fixed "now" for retry times


def request(**kw) -> b.TurnRequest:
    tools = (b.ToolSpec("search", "find", {"type": "object", "properties": {}}),
             b.ToolSpec("read", "read a file", {"type": "object", "properties": {"path": {"type": "string"}}}))
    base = dict(model="claude-opus-5-5", system="You are a test.", tools=b.tool_specs(tools),
                messages=(b.Message("user", (b.TextPart("hi"),)),), max_output_tokens=64000)
    base.update(kw)
    return b.TurnRequest(**base)


def provider(lb: Loopback, **kw) -> a.AnthropicProvider:
    return a.AnthropicProvider(FAKE_KEY, base_url=lb.url, clock=lambda: CLOCK, **kw)


def error_answer(status: int, vendor_type: str, message: str = "nope", headers: dict | None = None) -> Canned:
    body = json.dumps({"type": "error", "error": {"type": vendor_type, "message": message},
                       "request_id": "req_err"}).encode()
    return Canned(status, body, {"content-type": "application/json", "request-id": "req_err", **(headers or {})})


class TextStream(unittest.TestCase):
    def test_text_usage_served_model_and_deltas(self) -> None:
        lb = Loopback(self, [sse("text_only.sse")])
        deltas: list[str] = []
        resp = provider(lb).stream_turn(request(), on_event=lambda e: deltas.append(e.text))
        self.assertEqual(resp.text, "Hello, the tests pass: \u2713 \u201cdone\u201d")
        self.assertEqual("".join(deltas), resp.text)
        self.assertEqual(resp.stop_reason, b.END_TURN)
        self.assertEqual(resp.vendor_stop_reason, "end_turn")
        self.assertEqual(resp.served_model, "claude-opus-5-5")
        self.assertEqual(resp.request_id, "req_fixture")
        u = resp.usage
        self.assertEqual(u.input_tokens, b.observed(25))
        self.assertEqual(u.output_tokens, b.observed(12), "message_delta's cumulative count wins")
        self.assertEqual(u.cache_read_tokens, b.observed(2048))
        self.assertEqual(u.cache_write_tokens, b.observed(0), "a reported 0 is a known 0")
        self.assertEqual(u.reasoning_tokens, b.unknown())
        self.assertEqual(u.cost_usd, b.unknown(), "the API reports no dollars: unknown, never 0")

    def test_request_follows_the_documented_shape(self) -> None:
        lb = Loopback(self, [sse("text_only.sse")])
        provider(lb).stream_turn(request(effort="high"))
        got = lb.requests[0]
        self.assertEqual(got["path"], "/v1/messages")
        h = got["headers"]
        self.assertEqual(h["x-api-key"], FAKE_KEY)
        self.assertEqual(h["anthropic-version"], "2026-10-21")
        self.assertEqual(h["content-type"], "application/json")
        self.assertNotIn("anthropic-beta", h)
        body = got["body"]
        self.assertIs(body["stream"], True)
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["max_tokens"], 64000)
        self.assertEqual(body["output_config"], {"effort": "high"})
        self.assertEqual(body["system"], [{"type": "text", "text": "You are a test."}])
        for refused in ("temperature", "top_p", "top_k", "thinking", "fallbacks"):
            self.assertNotIn(refused, body)
        self.assertEqual([t["name"] for t in body["tools"]], ["read", "search"], "tools are sorted for the cache")
        for tool in body["tools"]:
            self.assertNotIn("eager_input_streaming", tool, "not sent to a host that is not Anthropic's own")

    def test_eager_input_streaming_only_to_the_first_party_host(self) -> None:
        first = a.AnthropicProvider(FAKE_KEY).build_body(request())
        self.assertTrue(all(t.get("eager_input_streaming") is True for t in first["tools"]))
        proxy = a.AnthropicProvider(FAKE_KEY, base_url="https://gateway.example/anthropic").build_body(request())
        self.assertTrue(all("eager_input_streaming" not in t for t in proxy["tools"]))
        self.assertNotIn("output_config", first, "no effort unless the route sets one")


class ToolStream(unittest.TestCase):
    def test_tool_call_parsed_and_thinking_sent_back_unchanged(self) -> None:
        lb = Loopback(self, [sse("tool_use.sse"), sse("text_only.sse")])
        p = provider(lb)
        first = p.stream_turn(request())
        self.assertEqual(first.stop_reason, b.TOOL_USE)
        kinds = [type(part).__name__ for part in first.parts]
        self.assertEqual(kinds, ["OpaquePart", "TextPart", "ToolCall"])
        thinking = first.parts[0]
        self.assertEqual(dict(thinking.data), {"type": "thinking", "thinking": "",
                                               "signature": "c2lnbmF0dXJlLWZpeHR1cmUtMDE="})
        call = first.tool_calls[0]
        self.assertEqual((call.call_id, call.name, dict(call.input)),
                         ("toolu_fixture_01", "read", {"path": "tests/test_io.py", "limit": 50}))
        self.assertEqual(first.usage.cache_write_tokens, b.observed(1500))
        messages = (b.Message("user", (b.TextPart("hi"),)), b.Message("assistant", first.parts),
                    b.Message("user", (b.ToolResult("toolu_fixture_01", "     1\timport io\n"),)))
        p.stream_turn(request(messages=messages))
        sent = lb.requests[1]["body"]["messages"]
        self.assertEqual(sent[1]["role"], "assistant")
        self.assertEqual(sent[1]["content"][0], {"type": "thinking", "thinking": "",
                                                 "signature": "c2lnbmF0dXJlLWZpeHR1cmUtMDE="})
        self.assertEqual(sent[1]["content"][1], {"type": "text", "text": "I will read the test file first."})
        self.assertEqual(sent[1]["content"][2], {"type": "tool_use", "id": "toolu_fixture_01", "name": "read",
                                                 "input": {"path": "tests/test_io.py", "limit": 50}})
        self.assertEqual(sent[2], {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_fixture_01", "content": "     1\timport io\n"}]})

    def test_tool_input_that_is_not_json_is_kept_raw(self) -> None:
        lb = Loopback(self, [sse("bad_tool_json.sse")])
        call = provider(lb).stream_turn(request()).tool_calls[0]
        self.assertIsNone(call.input)
        self.assertEqual(call.raw_input, '{"path": "a.py"')
        self.assertIn("not valid JSON", call.input_error)


class Failures(unittest.TestCase):
    def test_429_is_a_typed_limit_with_the_vendors_retry_time(self) -> None:
        lb = Loopback(self, [error_answer(429, "rate_limit_error", headers={"retry-after": "30"})])
        with self.assertRaises(b.LimitError) as cm:
            provider(lb).stream_turn(request())
        err = cm.exception
        self.assertEqual(err.retry_after_s, 30.0)
        self.assertEqual(err.reset_at, a._timestamp(CLOCK + 30))
        self.assertRegex(err.reset_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual((err.status, err.vendor_type, err.request_id), (429, "rate_limit_error", "req_err"))

    def test_429_without_retry_after_has_no_reset_time(self) -> None:
        lb = Loopback(self, [error_answer(429, "rate_limit_error")])
        with self.assertRaises(b.LimitError) as cm:
            provider(lb).stream_turn(request())
        self.assertIsNone(cm.exception.retry_after_s)
        self.assertIsNone(cm.exception.reset_at)

    def test_each_status_has_its_type(self) -> None:
        table = [(529, "overloaded_error", b.OverloadError), (401, "authentication_error", b.AuthError),
                 (403, "permission_error", b.AuthError), (402, "billing_error", b.BillingError),
                 (400, "invalid_request_error", b.RequestError), (404, "not_found_error", b.RequestError),
                 (413, "request_too_large", b.RequestError), (500, "api_error", b.ServerError)]
        lb = Loopback(self, [error_answer(s, t) for s, t, _ in table])
        p = provider(lb)
        for status, vendor_type, cls in table:
            with self.subTest(status=status), self.assertRaises(cls) as cm:
                p.stream_turn(request())
            self.assertIs(type(cm.exception), cls)
            self.assertIn(str(status), cm.exception.detail)

    def test_an_error_event_in_the_stream_is_typed(self) -> None:
        lb = Loopback(self, [sse("overloaded_mid_stream.sse")])
        with self.assertRaises(b.OverloadError) as cm:
            provider(lb).stream_turn(request())
        self.assertIn("overloaded_error", cm.exception.detail)

    def test_a_stream_cut_off_before_message_stop_is_a_transport_fault(self) -> None:
        lb = Loopback(self, [sse("cut_off.sse")])
        with self.assertRaises(b.TransportError):
            provider(lb).stream_turn(request())

    def test_a_200_that_is_not_an_event_stream_is_refused(self) -> None:
        lb = Loopback(self, [Canned(200, b'{"type":"message"}', {"content-type": "application/json"})])
        with self.assertRaises(b.ProtocolError):
            provider(lb).stream_turn(request())

    def test_the_key_never_leaves_in_an_error(self) -> None:
        lb = Loopback(self, [error_answer(401, "authentication_error", f"invalid x-api-key {FAKE_KEY}")])
        p = provider(lb)
        with self.assertRaises(b.AuthError) as cm:
            p.stream_turn(request())
        self.assertNotIn(FAKE_KEY, cm.exception.detail)
        self.assertNotIn(FAKE_KEY, str(cm.exception))
        self.assertIn("[redacted]", cm.exception.detail)
        self.assertNotIn(FAKE_KEY, repr(p))

    def test_a_refused_connection_is_a_transport_fault(self) -> None:
        lb = Loopback(self, [])
        url = lb.url
        lb.close()
        with self.assertRaises(b.TransportError) as cm:
            a.AnthropicProvider(FAKE_KEY, base_url=url).stream_turn(request())
        self.assertNotIn(FAKE_KEY, cm.exception.detail)


class Cancel(unittest.TestCase):
    def test_an_interrupt_closes_the_stream(self) -> None:
        data = (FIXTURES / "anthropic" / "text_only.sse").read_bytes()
        cut = data.index(b"Hello, ") + data[data.index(b"Hello, "):].index(b"\n\n") + 2
        answer = Canned(200, [data[:cut], data[cut:]], {"content-type": "text/event-stream"}, hold_after=1)
        lb = Loopback(self, [answer])
        self.addCleanup(answer.release.set)
        cancel = b.CancelToken()
        first_delta = threading.Event()

        def on_event(event: b.TextDelta) -> None:
            first_delta.set()

        result: dict = {}

        def call() -> None:
            try:
                result["response"] = provider(lb).stream_turn(request(), on_event=on_event, cancel=cancel)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                result["error"] = exc

        worker = threading.Thread(target=call)
        worker.start()
        first_delta.wait()
        answer.held.wait()
        cancel.cancel()
        worker.join()
        self.assertIsInstance(result.get("error"), b.Cancelled, result)

    def test_a_cancelled_token_sends_nothing(self) -> None:
        lb = Loopback(self, [sse("text_only.sse")])
        cancel = b.CancelToken()
        cancel.cancel()
        with self.assertRaises(b.Cancelled):
            provider(lb).stream_turn(request(), cancel=cancel)
        self.assertEqual(lb.requests, [])


class Configuration(unittest.TestCase):
    def test_base_url_rules(self) -> None:
        for bad in ("http://api.example.com", "https://user:pw@api.example.com", "ftp://x", "https://",
                    "https://api.example.com/?q=1", " https://api.example.com"):
            with self.subTest(url=bad), self.assertRaises(a.ConfigError):
                a.AnthropicProvider(FAKE_KEY, base_url=bad)
        for good in ("http://127.0.0.1:9", "http://[::1]:9", "http://localhost:9", "https://api.anthropic.com/"):
            with self.subTest(url=good):
                a.AnthropicProvider(FAKE_KEY, base_url=good)
        self.assertEqual(a.AnthropicProvider(FAKE_KEY, base_url="https://gw.example/anthropic/")._path,
                         "/anthropic/v1/messages")

    def test_a_key_must_be_one_printable_token(self) -> None:
        for bad in ("", "short", "sk-ant has space 1234", "sk-ant-\nnewline-1234", "sk-ant-\u00e9-12345678"):
            with self.subTest(key=bad), self.assertRaises(a.ConfigError):
                a.AnthropicProvider(bad)

    def test_from_env_reads_only_the_two_names(self) -> None:
        with self.assertRaises(a.ConfigError):
            a.AnthropicProvider.from_env({"OPENAI_API_KEY": FAKE_KEY})
        p = a.AnthropicProvider.from_env({a.ENV_KEY: FAKE_KEY, a.ENV_BASE_URL: "http://127.0.0.1:9"})
        self.assertEqual((p._scheme, p._host, p._port), ("http", "127.0.0.1", 9))
        self.assertEqual(p.secrets, (FAKE_KEY,))

    def test_retry_after_forms(self) -> None:
        self.assertEqual(a.parse_retry_after("12", CLOCK), 12.0)
        self.assertEqual(a.parse_retry_after("-3", CLOCK), 0.0)
        self.assertIsNone(a.parse_retry_after(None, CLOCK))
        self.assertIsNone(a.parse_retry_after("soon", CLOCK))
        self.assertIsNone(a.parse_retry_after("nan", CLOCK))
        import email.utils
        later = email.utils.formatdate(CLOCK + 90, usegmt=True)
        self.assertEqual(a.parse_retry_after(later, CLOCK), 90.0)


if __name__ == "__main__":
    unittest.main()
